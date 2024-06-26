import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Any, List, Tuple
from urllib.parse import urljoin

import aiohttp
import inquirer
import tabulate
from DrissionPage import ChromiumPage, ChromiumOptions, WebPage
from bs4 import BeautifulSoup
from lxml import html
from rich.prompt import Confirm

from linovelib2epub.models import LightNovel, LightNovelImage, CatalogMasiroChapter, CatalogMasiroVolume
from linovelib2epub.spider import BaseNovelWebsiteSpider
from linovelib2epub.utils import aiohttp_get_with_retry, aiohttp_post_with_retry, requests_get_with_retry
from .config import env_settings
from ..exceptions import LinovelibException


@dataclass
class MasiroLoginInfo:
    login_url: str = 'https://masiro.me/admin/auth/login'
    # don't print secrets
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    token: str = ''


@dataclass()
class LoginSessionState:
    page: WebPage
    csrf_token: str


class MasiroSpider(BaseNovelWebsiteSpider):

    def __init__(self, spider_settings: Dict[str, Any]):
        super().__init__(spider_settings)

        # read user secrets
        self._masiro_username = env_settings.get("MASIRO_LOGIN_USERNAME")
        self._masiro_password = env_settings.get("MASIRO_LOGIN_PASSWORD")
        if (not self._masiro_username) or (not self._masiro_password):
            raise LinovelibException("Masiro account is not found. About configuration, check the documentation.")

        self.FETCH_CHAPTER_CONCURRENCY_LEVEL = 1

    def fetch(self) -> LightNovel:
        novel = asyncio.run(self._fetch())
        return novel

    async def _fetch(self) -> LightNovel:
        login_state = self._login_by_browser()
        page = login_state.page
        csrf_token = login_state.csrf_token
        login_info = MasiroLoginInfo(token=csrf_token)

        page.cookies_to_session(copy_user_agent=True)
        # rprint(page.session.cookies)

        book_url = f"https://masiro.me/admin/novelView?novel_id={self.spider_settings['book_id']}"
        novel = await self._crawl_book_by_browser(book_url, page, login_info)
        return novel

    async def _crawl_book_by_browser(self, url: str, session: WebPage, login_info: MasiroLoginInfo):

        url_is_ok = session.get(url, show_errmsg=True, retry=5, interval=2, timeout=10)
        session.wait.doc_loaded()
        html_text = session.html

        self._check_user_level_limit(html_text, url)

        new_novel, points_balance = self._extract_basic_info(html_text, url)
        self.logger.debug(f'{new_novel=}; {points_balance=}')

        catalog_list: List[CatalogMasiroVolume] = self._convert_to_catalog_list(html_text)

        # select_volume_mode
        if self.spider_settings['select_volume_mode']:
            catalog_list = self._handle_select_volume(catalog_list)

        # resolve final catalog
        final_catalog_list: List[CatalogMasiroVolume] = catalog_list
        chapter_to_pay: Dict[str, int] = self._get_unpayed_chapter(final_catalog_list)
        self.logger.debug(f'{final_catalog_list=}')

        # 计算所需的积分价格，必须排除用户已经购买过的章节
        quote = sum([volume.volume_cost for volume in final_catalog_list])

        # 如果本书所有章节都是不需要积分查看的 => 直接起飞
        if quote == 0:
            # 1
            self.logger.info("当前所有卷都是免费积分或你已经购买，直接执行下载。")
            await self.fetch_chapters(session, final_catalog_list, new_novel)
            return new_novel
        else:
            # 2
            # [可选]显示当前挑选卷的积分消耗预计值和用户当前积分余额
            table_header = [
                ['vid', 'volume title', 'volume cost(G)']
            ]
            table_body = [[volume.vid, volume.volume_title, volume.volume_cost] for volume in
                          final_catalog_list]
            table_data = table_header + table_body
            table_view = tabulate.tabulate(table_data)
            self.logger.info(table_view)

            if quote > points_balance:
                # 2.1
                self.logger.warning(f"Need {quote} and your balance is {points_balance}, exit.")
                sys.exit()
            else:
                # 2.2
                if Confirm.ask(f"Need {quote} and your balance is {points_balance}, buy and continue?"):
                    # 2.2.1
                    self.logger.info("用户积分余额足够，决定购买。")
                    await self._pay_chapters_by_browser(session, login_info, chapter_to_pay)
                    await self.fetch_chapters(session, final_catalog_list, new_novel)
                    return new_novel
                else:
                    # 2.2.2
                    self.logger.info("用户积分余额足够，但是决定不购买，程序退出。")
                    sys.exit()

    async def download_pages(self, session: WebPage, page_url_set: set) -> Dict[str, str]:
        self.logger.info(f'page url set = {len(page_url_set)}')

        url_to_page = {url: 'NOT_DOWNLOAD_READY' for url in page_url_set}

        # use semaphore to control concurrency
        max_concurrency = self.FETCH_CHAPTER_CONCURRENCY_LEVEL
        semaphore = asyncio.Semaphore(max_concurrency)

        self.logger.info(f'DOWNLOAD_PAGES concurrency level: {max_concurrency}.')

        tasks = {asyncio.create_task(self._download_page(session, semaphore, url), name=url)
                 for url in page_url_set}
        pending: set = tasks
        succeed_count = 0

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)
            # Note: This does not raise TimeoutError! Futures that aren't done when the timeout occurs
            # are returned in the second set

            # 1. succeed => normal result in done(# HAPPY CASE)
            # 2. Timeout => No TimeoutError, put timeout tasks in pending(SAD CASE(need retry))
            # 3  Other Exception before timeout => (SAD CASE(need retry)

            for done_task in done:
                exception = done_task.exception()
                task_url = done_task.get_name()

                if exception is None:
                    url_to_page[task_url] = done_task.result()
                    succeed_count += 1
                else:
                    # [TEST]make connect=.1 to reach this branch, should retry all the urls that entered this case
                    self.logger.error(
                        f'{exception.__class__.__name__}: {task_url} should retry.')
                    pending.add(
                        asyncio.create_task(self._download_page(session, semaphore, task_url), name=task_url))

            self.logger.info(f'SUCCEED_COUNT: {succeed_count}')
            self.logger.info(f'[NEXT TURN]Pending task count: {len(pending)}')

        # ASSERTION: make sure data is ok.
        for page_content in url_to_page.values():
            if page_content == "NOT_DOWNLOAD_READY":
                raise LinovelibException('天啊，发生什么事了。如果这个断言被触发，那么证明代码逻辑有问题。')

        return url_to_page

    async def _download_page(self, session: WebPage, semaphore, url) -> str | None:
        async with semaphore:
            is_url_available = session.get(url, headers=self.request_headers(), retry=10, interval=3, timeout=10)
            loaded = session.wait.doc_loaded()
            # 多等待，少被ban
            await asyncio.sleep(3)

            html = session.html
            if html:
                if "访问频繁" in html:
                    # <title>429 Too Many Requests</title>
                    # <h3 style="font-size: 32px;">
                    # 访问频繁，歇会吧您内。
                    # </h3>
                    raise LinovelibException(f'429 Too Many Requests when downloading {url}')
                self.logger.info(f'page {url} => ok.')
                return html
            else:
                # ...... => should retry
                self.logger.error(f'page {url} => should retry.')
                raise LinovelibException(f'fetch page url {url} failed with error status ?.')

    def _login_by_browser(self) -> LoginSessionState:
        # see https://g1879.gitee.io/drissionpagedocs/get_start/before_start
        co = ChromiumOptions()
        if self.spider_settings['browser_path']:
            # 似乎这个set_browser_path不 work。暂时没有解决方案。
            # path = "/usr/bin/google-chrome"
            # path = r'D:\Chrome\Chrome.exe'
            path = self.spider_settings['browser_path']
            co.set_browser_path(path).save()
        arguments = [
            "-no-first-run",
            "-force-color-profile=srgb",
            "-metrics-recording-only",
            # "-password-store=basic",
            "-use-mock-keychain",
            "-export-tagged-pdf",
            "-no-default-browser-check",
            "-disable-background-mode",
            "-enable-features=NetworkService,NetworkServiceInProcess,LoadCryptoTokenExtension,PermuteTLSExtensions",
            "-disable-features=FlashDeprecationWarning,EnablePasswordsAccountStorage",
            "-deny-permission-prompts",
            "-disable-gpu"
            # "-headless=new"
            # "-incognito"
        ]
        for argument in arguments:
            co.set_argument(argument)

        page = WebPage(chromium_options=co)
        login_url = MasiroLoginInfo.login_url
        # <input type="hidden" name="_token" value="???">
        page.get(login_url)

        # 定义下面几种状态，使用有限状态机理论进行分析。
        # A.遇到已登录后的页面。发生可能的理由：由于上一次cookie没有过期，而且之前选择了记住登录。
        # B1.遇到 cloudflare turnstile。发生的理由：触发 cloudflare turnstile风控，要求挑战。
        # B2.遇到登录表单界面。发生可能的理由：没有记住登录，或者cookie已经过期。

        # 列举状态迁移图：
        #  -> A
        #  -> B1 -> A。触发风控，上次登录没有过期。
        #  -> B1 -> B2 -> A。触发风控，上次登录过期或者没有记住登录。=> 需要重新登录
        #  -> B2 -> A。没有触发风控，上次登录过期或者没有记住登录。=> 需要重新登录

        # //li[@class='user-footer']//a[contains(text(), '登出')]
        already_logged_in_xpath = 't:a@tx():登出'

        logout_flag = page(already_logged_in_xpath)
        if logout_flag:
            self.logger.info('-> 已登录')
        else:
            self.logger.info('未登录，正在尝试挑战或登录……')
            while True:
                # 这个方法有点耗时，可能需要留意
                self._pass_cycle(page)
                logout_flag = page(already_logged_in_xpath)
                if logout_flag:
                    self.logger.info('-> 挑战? -> 已登录')
                    # now can jump to next task
                    break
                else:
                    self.logger.info('-> 挑战? -> 未登录')
                    try:
                        # what is s_ele? https://g1879.gitee.io/drissionpagedocs/get_start/concept#-sessionelement
                        ele = page.s_ele('xpath://form[@id="loginForm"]/h1')
                        if ele and ele.text == "登录":
                            # 使用【浏览器模式】填表登录。
                            # refer https://g1879.gitee.io/drissionpagedocs/get_start/examples/control_browser
                            page.ele('#username').input(self._masiro_username)
                            page.ele('#password').input(self._masiro_password)
                            # page.ele('#remember').input(True)

                            page.ele('#login-btn').click()
                            page.wait.load_start()
                            self.logger.info('-> 挑战? -> 未登录 -> 登录成功')

                            # now can jump to next task
                            break

                        self.logger.info('-> 挑战? -> 未登录: 等待下一次重试')
                    except:
                        time.sleep(.2)

        #  after https://masiro.me/admin
        # head tag <meta name="csrf-token" content="???">
        csrf_token = page.ele('xpath://meta[@name="csrf-token"]/@content').attr('content')
        self.logger.debug(f'csrf token: {csrf_token}')

        return LoginSessionState(page, csrf_token)

    @staticmethod
    def _pass_cycle(_driver: ChromiumPage):
        # cycle to bypass cloudflare turnstile
        try:
            # css selector: label.ctp-checkbox-label
            if _driver('xpath://div/iframe').s_ele(".ctp-checkbox-label") is not None:
                # timeout: 查找元素超时时间（秒），默认与元素所在页面等待时间一致
                btn = _driver('xpath://div/iframe').ele(".ctp-checkbox-label", timeout=0.2)
                # 注意：这里可能需要等几秒。参阅：https://www.youtube.com/watch?v=TCRcRXszhhU
                btn.click()
        except:
            pass

    async def _fetch_legacy(self) -> LightNovel:
        # can share
        trust_env = False if self.spider_settings["disable_proxy"] else True
        timeout = aiohttp.ClientTimeout(total=30, connect=15)

        # don't share tcp connection and cookie objects
        jar = aiohttp.CookieJar(unsafe=True)
        conn = aiohttp.TCPConnector(ssl=False)

        continue_flag = None
        partial_novel = None
        final_catalog_list = None

        async with aiohttp.ClientSession(connector=conn, trust_env=trust_env, cookie_jar=jar,
                                         timeout=timeout) as session:
            login_info = await self._login_by_api(session)

            # get basic info and catalog
            book_url = f"https://masiro.me/admin/novelView?novel_id={self.spider_settings['book_id']}"
            new_novel, final_catalog_list, flag = await self._crawl_book_basic_info_and_catalog_by_api(book_url,
                                                                                                       session,
                                                                                                       login_info)
            if flag == 'NOT_NEED_RESET_SESSION':
                await self.fetch_chapters(session, final_catalog_list, new_novel)
                return new_novel
            else:
                continue_flag = flag
                partial_novel = new_novel
                final_catalog_list = final_catalog_list

        if continue_flag == 'NEED_RESET_SESSION':
            self.logger.info('Recreate session and login, then continue crawling.')
            # continue fetch_chapters by new session
            jar2 = aiohttp.CookieJar(unsafe=True)
            conn2 = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=conn2, trust_env=trust_env, cookie_jar=jar2,
                                             timeout=timeout) as session:
                login_info = await self._login_by_api(session)
                await self.fetch_chapters(session, final_catalog_list, partial_novel)
                return partial_novel

    async def _login_by_api(self, session):
        login_info = MasiroLoginInfo()
        login_info.username = self._masiro_username
        login_info.password = self._masiro_password

        # 1. get csrf token (HTTP GET)
        await self._masiro_get_token(login_info, session)
        login_param = self._build_login_param(login_info)
        login_headers = self._build_login_headers(login_info)

        # 2. do login (HTTP POST)
        result = await aiohttp_post_with_retry(session, login_info.login_url, params=login_param,
                                               headers=login_headers)
        # {"code":1,"msg":"\u767b\u5f55\u6210\u529f!","url":"https:\/\/masiro.me"}

        # now this session is already logged.
        return login_info

    async def _crawl_book_basic_info_and_catalog_by_api(self,
                                                        url: str,
                                                        session: aiohttp.ClientSession,
                                                        login_info):

        html_text = await aiohttp_get_with_retry(session, url, self.request_headers())

        self._check_user_level_limit(html_text, url)

        new_novel, points_balance = self._extract_basic_info(html_text, url)

        catalog_list: List[CatalogMasiroVolume] = self._convert_to_catalog_list(html_text)

        # select_volume_mode
        if self.spider_settings['select_volume_mode']:
            catalog_list = self._handle_select_volume(catalog_list)

        # resolve final catalog
        final_catalog_list: List[CatalogMasiroVolume] = catalog_list
        chapter_to_pay: Dict[str, int] = self._get_unpayed_chapter(final_catalog_list)

        # 计算所需的积分价格，必须排除用户已经购买过的章节
        quote = sum([volume.volume_cost for volume in final_catalog_list])

        # 如果本书所有章节都是不需要积分查看的 => 直接起飞
        if quote == 0:
            # 1
            self.logger.info("当前所有卷都是免费积分或你已经购买，直接执行下载。")
            return new_novel, final_catalog_list, 'NOT_NEED_RESET_SESSION'
        else:
            # 2
            # [可选]显示当前挑选卷的积分消耗预计值和用户当前积分余额
            table_header = [
                ['vid', 'volume title', 'volume cost(G)']
            ]
            table_body = [[volume.vid, volume.volume_title, volume.volume_cost] for volume in
                          final_catalog_list]
            table_data = table_header + table_body
            table_view = tabulate.tabulate(table_data)
            self.logger.info(table_view)

            if quote > points_balance:
                # 2.1
                self.logger.warning(f"Need {quote} and your balance is {points_balance}, exit.")
                sys.exit()
            else:
                # 2.2
                if Confirm.ask(f"Need {quote} and your balance is {points_balance}, buy and continue?"):
                    # 2.2.1
                    self.logger.info("用户积分余额足够，决定购买。")

                    # batch payments
                    # warning: session pollution: session will redirect to chapter detail pages.
                    await self._pay_chapters(session, login_info, chapter_to_pay)

                    self.logger.info(f"MUST reset/recover session state.")

                    # FLAGS: MUST re-login after payment to fetch chapter text content
                    return new_novel, final_catalog_list, 'NEED_RESET_SESSION'
                else:
                    # 2.2.2
                    self.logger.info("用户积分余额足够，但是决定不购买，程序退出。")
                    sys.exit()

    def _check_user_level_limit(self, html_text, url):
        index = html_text.find("小孩子不能看")
        if index != -1:
            self.logger.error(f"[等级限制]: 你在真白萌的用户等级不足以查看链接: {url}")
            sys.exit()

    def _extract_basic_info(self, html_text, url):
        soup = BeautifulSoup(html_text, 'lxml')

        # title √
        # author √
        # cover √
        # translator
        # status
        # tags √
        # recent_update
        # popularity
        # word_count_text
        # original
        # brief_introduction √

        # get user point balance
        # .user-header small text 金币:91 粉丝:
        text = soup.find('li', {'class': 'user-header'}).find('small').text
        match = re.match(r'金币:(\d+)\s*', text)
        if match:
            points_balance = int(match.group(1))
            self.logger.info(f'User points balance is {points_balance}.')

        title = soup.find('div', {'class': 'novel-title'}).text
        author = soup.find('div', {'class': 'author'}).find('a').text
        _tag_spans = soup.find('div', class_='tags').find_all('a')
        tags = [tag.find('span').text for tag in _tag_spans]
        brief_introduction = soup.find('div', {'class': 'brief'}).text
        cover_src = soup.find('img', {'class': 'img img-thumbnail'})['src'].split("?")[0]
        new_novel = LightNovel()
        new_novel.book_id = self.spider_settings['book_id']
        new_novel.book_title = title
        new_novel.author = author
        new_novel.description = brief_introduction
        new_novel.book_cover = LightNovelImage(related_page_url=url,
                                               remote_src=cover_src,
                                               book_id=self.spider_settings['book_id'],
                                               is_book_cover=True)
        new_novel.mark_basic_info_ready()
        return new_novel, points_balance

    def _get_unpayed_chapter(self, catalog_list: List[CatalogMasiroVolume]) -> Dict[str, int]:
        unpayed_chapter_dict = {}

        for volume in catalog_list:
            volume_unpayed_chapter_dict = {
                chapter.remote_chapter_id: int(chapter.chapter_cost)
                for chapter in volume.chapters
                if int(chapter.chapter_payed) == 0 and int(chapter.chapter_cost) > 0
            }
            unpayed_chapter_dict.update(volume_unpayed_chapter_dict)

        return unpayed_chapter_dict

    async def _pay_chapters_by_browser(self, session, login_info, chapter_to_pay: Dict[str, int]):
        self.logger.info(f'len of chapter_to_pay = {len(chapter_to_pay)}')

        _pay_chapter_func = self._pay_chapter_by_browser

        max_concurrency = 2
        semaphore = asyncio.Semaphore(max_concurrency)

        tasks = {
            asyncio.create_task(_pay_chapter_func(session, semaphore, login_info, chapter_id, chapter_cost),
                                name=chapter_id)
            for chapter_id, chapter_cost in chapter_to_pay.items()
        }
        pending: set = tasks
        succeed_count = 0

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)
            # Note: This does not raise TimeoutError! Futures that aren't done when the timeout occurs
            # are returned in the second set

            # 1. succeed => normal result in done(# HAPPY CASE)
            # 2. Timeout => No TimeoutError, put timeout tasks in pending(SAD CASE(need retry))
            # 3  Other Exception before timeout => (SAD CASE(need retry)

            for done_task in done:
                exception = done_task.exception()
                chapter_id = done_task.get_name()

                if exception is None:
                    # done_task.result()
                    succeed_count += 1
                else:
                    # [TEST]make connect=.1 to reach this branch, should retry all the tasks that entered this case
                    self.logger.info(f'Exception: {type(exception)}')
                    self.logger.info(f'FAIL: {chapter_id}; should retry paying this chapter_id.')
                    pending.add(
                        asyncio.create_task(
                            _pay_chapter_func(session, semaphore, login_info, chapter_id, chapter_to_pay[chapter_id]),
                            name=chapter_id)
                    )

            self.logger.info(f'SUCCEED_COUNT: {succeed_count}')
            self.logger.info(f'[NEXT TURN]Pending task count: {len(pending)}')

        self.logger.info(f'All payment of chapters were successful.')

    async def _pay_chapters(self, session, login_info, chapter_to_pay: Dict[str, int]):
        self.logger.info(f'len of chapter_to_pay = {len(chapter_to_pay)}')

        _pay_chapter_func = self._pay_chapter

        # payments concurrency level can be hardcoded
        max_concurrency = 2
        semaphore = asyncio.Semaphore(max_concurrency)

        async with session:
            tasks = {asyncio.create_task(_pay_chapter_func(session, semaphore, login_info, chapter_id, chapter_cost),
                                         name=chapter_id)
                     for chapter_id, chapter_cost in chapter_to_pay.items()}
            pending: set = tasks
            succeed_count = 0

            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)
                # Note: This does not raise TimeoutError! Futures that aren't done when the timeout occurs
                # are returned in the second set

                # 1. succeed => normal result in done(# HAPPY CASE)
                # 2. Timeout => No TimeoutError, put timeout tasks in pending(SAD CASE(need retry))
                # 3  Other Exception before timeout => (SAD CASE(need retry)

                for done_task in done:
                    exception = done_task.exception()
                    chapter_id = done_task.get_name()

                    if exception is None:
                        # done_task.result()
                        succeed_count += 1
                    else:
                        # [TEST]make connect=.1 to reach this branch, should retry all the tasks that entered this case
                        self.logger.info(f'Exception: {type(exception)}')
                        self.logger.info(f'FAIL: {chapter_id}; should retry paying this chapter_id.')
                        pending.add(
                            asyncio.create_task(_pay_chapter_func(session, semaphore, login_info, chapter_id,
                                                                  chapter_to_pay[chapter_id]),
                                                name=chapter_id)
                        )

                self.logger.info(f'SUCCEED_COUNT: {succeed_count}')
                self.logger.info(f'[NEXT TURN]Pending task count: {len(pending)}')

        self.logger.info(f'All payment of chapters were successful.')

    async def _pay_chapter_by_browser(self, session: WebPage, semaphore, login_info, chapter_id, chapter_cost):
        async with semaphore:
            pay_url = 'https://masiro.me/admin/pay'
            pay_params = {'type': '2', 'object_id': chapter_id, 'cost': chapter_cost}
            pay_headers = self._build_login_headers(login_info=login_info)
            try:
                # session.post() is not work,emm... workaround is using internal requests.session
                requests_session = session.session
                # add a retry mechanism for self retry
                resp = requests_session.post(url=pay_url, data=pay_params, headers=pay_headers)
                self.logger.debug(f'chapter payment {resp.text=}')
                if resp and json.loads(resp.text)['code'] == 1:
                    self.logger.info(f'[SUCCESS] pay for chapter {chapter_id} with cost {chapter_cost}.')
                else:
                    # resp None or
                    # resp is not None but is not a json string
                    raise LinovelibException(f"[FAIL] pay for chapter {chapter_id} with cost {chapter_cost}.")
            except Exception as e:
                self.logger.debug(f'chapter payment ({chapter_id}) failed. {e=}')
                raise LinovelibException(f"[FAIL] pay for chapter {chapter_id} with cost {chapter_cost}.")

    async def _pay_chapter(self, session, semaphore, login_info, chapter_id, chapter_cost):
        async with semaphore:
            pay_url = 'https://masiro.me/admin/pay'
            pay_params = {'type': '2', 'object_id': chapter_id, 'cost': chapter_cost}
            pay_headers = self._build_login_headers(login_info=login_info)
            try:
                resp = await aiohttp_post_with_retry(session, url=pay_url, params=pay_params, headers=pay_headers)
                if resp and json.loads(resp)['code'] == 1:
                    self.logger.info(f'[SUCCESS] pay for chapter {chapter_id} with cost {chapter_cost}.')
                else:
                    # resp None or
                    # resp is not None but is not a json string
                    raise LinovelibException(f"[FAIL] pay for chapter {chapter_id} with cost {chapter_cost}.")
            except Exception as e:
                raise LinovelibException(f"[FAIL] pay for chapter {chapter_id} with cost {chapter_cost}.")

    def _convert_to_catalog_list(self, html_text) -> List[CatalogMasiroVolume]:
        """
        input example:

        <ul class="chapter-ul">

            <li id="1" class="chapter-box"><span class="sign minus">-</span><b>杂项1</b></li>
            <li>
              <ul data-enum="21" class="episode-ul">
                <a href="/admin/novelReading?cid=71343" data-id="71343"
                   data-cost="0" data-payed="0" data-uid="61162" class="to-read">

                    <li class="episode-box ">
                        <span>第1话 章节标题&nbsp;</span>
                        <small></small>
                        <span>
                            <span title="创建时间：2023-07-10 12:01:42；更新时间：2023-07-13 06:34:43">23-07-10 12:01</span>
                            &nbsp;
                            <small title="更新时间：2023-07-13 06:34:43">(<u>更新</u>)</small>
                        </span>
                    </li>
                </a>
                <a>...</a>
              </ul>
            </li>

            <li id="2" class="chapter-box"><span class="sign minus">-</span><b>杂项2</b></li>
            <li>
              <ul class=“episode-ul”>...</ul>
            </li>

            ...
        <li>
        """

        # [{vid:1, volume_title: "XX", chapters:[{dict},{dict},{...}]
        catalog_list: List[CatalogMasiroVolume] = []

        soup = BeautifulSoup(html_text, 'html.parser')
        ul_element = soup.find('ul', {'class': 'chapter-ul'})

        if ul_element:
            # 使用.find_all()方法并传递recursive=False参数，获取<ul>元素的直接子代<li>
            li_elements = ul_element.find_all('li', recursive=False)

            _current_chapters: List[CatalogMasiroChapter] = []
            _current_volume_text = ''
            _volume_index = 0

            for idx, li in enumerate(li_elements):

                class_value = li.get('class')
                if class_value and 'chapter-box' in class_value:
                    volume_name = li.find("b").text

                    _volume_index += 1
                    # reset current_* variables
                    _current_volume_text = volume_name
                    _current_chapters: List[CatalogMasiroChapter] = []

                    new_volume = CatalogMasiroVolume(
                        vid=_volume_index,
                        volume_title=_current_volume_text,
                        chapters=_current_chapters
                    )
                    catalog_list.append(new_volume)
                else:
                    chapter_link_items = li.select('a.to-read')

                    for idx, chapter_a_item in enumerate(chapter_link_items):
                        #  <a href="/admin/novelReading?cid=71343" data-id="71343"
                        #     data-cost="0" data-payed="0" data-uid="61162" class="to-read">

                        data_cost = chapter_a_item['data-cost']
                        # 0 => unpayed; 1 => payed
                        data_payed = chapter_a_item['data-payed']
                        # remote server chapter_id
                        remote_chapter_id = chapter_a_item['data-id']

                        a_href = chapter_a_item['href']
                        chapter_url = urljoin('https://masiro.me', a_href)

                        chapter_title = chapter_a_item.find('li').find('span').text
                        # remove `&nbsp;` and `\r\n`.
                        chapter_title = chapter_title.strip()
                        # todo fix remove \xa0 and &zwj;
                        chapter_title = re.sub(r'&nbsp;', '', chapter_title)

                        new_chapter: CatalogMasiroChapter = CatalogMasiroChapter(
                            chapter_title=chapter_title,
                            chapter_url=chapter_url,
                            chapter_cost=data_cost,
                            chapter_payed=data_payed,
                            remote_chapter_id=remote_chapter_id,
                        )
                        _current_chapters.append(new_chapter)

        return catalog_list

    @staticmethod
    def _handle_select_volume(catalog_list: List[CatalogMasiroVolume]):
        def _reduce_catalog_by_selection(catalog_list: List[CatalogMasiroVolume], selection_array):
            return [volume for volume in catalog_list if volume.vid in selection_array]

        def _get_volume_choices(catalog_list: List[CatalogMasiroVolume]) -> List[Tuple[str, int]]:
            choice_view = []
            for volume in catalog_list:
                item_text = f"{volume.volume_title} | chapter nums: {len(volume.chapters)} " \
                            f"| volume cost: {volume.volume_cost}"
                choice_view.append((item_text, volume.vid))
            return choice_view

        # step 1: need to show UI for user to select one or more volumes,
        # step 2: then reduce the whole catalog_list to a reduced_catalog_list based on user selection
        # UI show
        question_name = 'Selecting volumes'
        question_description = "Which volumes you want to download?(use SPACE to select one or multiple volumes)"
        volume_choices = _get_volume_choices(catalog_list)
        questions = [
            inquirer.Checkbox(question_name,
                              message=question_description,
                              choices=volume_choices, ),
        ]
        # user input
        # answers: {'Selecting volumes': [3, 6]}
        answers = inquirer.prompt(questions)
        catalog_list = _reduce_catalog_by_selection(catalog_list, answers[question_name])
        return catalog_list

    @staticmethod
    def _build_login_param(login_info: MasiroLoginInfo):
        # network tab 负载区域可以找到表单字段列表
        return {
            'username': login_info.username,
            'password': login_info.password,
            'remember': '1',
            '_token': login_info.token
        }

    def _build_login_headers(self, login_info: MasiroLoginInfo):
        # network tab 标头区域可以审查
        headers = self.request_headers()
        headers['x-csrf-token'] = login_info.token
        headers['x-requested-with'] = 'XMLHttpRequest'
        return headers

    def request_headers(self) -> Dict[str, Any]:
        return {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.2088.46'
        }

    async def _masiro_get_token(self, login_info: MasiroLoginInfo, session):
        res = await aiohttp_get_with_retry(session, login_info.login_url, self.request_headers(), logger=self.logger)

        page_body = html.fromstring(res)
        token = str(page_body.xpath('//input[@class=\'csrf\']/@value')[0])
        self.logger.debug(f'token: {token}')

        login_info.token = token

    def extract_body_content(self, page: str):
        """
        :param page:
        :return:
        """
        html_content = BeautifulSoup(page, 'lxml')
        body_content = html_content.find('div', {'class': 'nvl-content'}).prettify()
        return body_content
