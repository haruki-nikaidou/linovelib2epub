import random
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urljoin

import demjson3
import inquirer
import requests
from bs4 import (BeautifulSoup)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from . import BaseNovelWebsiteSpider
from .linovelib_mobile_rules import generate_mapping_result
from ..exceptions import LinovelibException, PageContentIllegalException
from ..models import LightNovel, LightNovelChapter, LightNovelVolume, LightNovelImage, CatalogLinovelibMobileChapter, \
    CatalogLinovelibMobileVolume
from ..utils import (cookiedict_from_str, create_folder_if_not_exists,
                     requests_get_with_retry)


class LinovelibMobileSpider(BaseNovelWebsiteSpider):

    def __init__(self, spider_settings: Optional[Dict] = None):
        super().__init__(spider_settings)
        self._init_http_client()

        # it might be better to refactor to asyncio mode
        self._mapping_result = generate_mapping_result()
        self._html_content_id = self._mapping_result.content_id
        self._mapping_dict = self._mapping_result.mapping_dict

        self.FETCH_CHAPTER_CONCURRENCY_LEVEL = 1

        self._driver = None

    def request_headers(self, referer: str = '', random_ua: bool = True):
        default_mobile_ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1 Edg/120.0.0.0'
        default_referer = 'https://www.bilinovel.com'
        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'Referer': referer if referer else default_referer,
            # use random mobile phone header later
            # 'User-Agent': self.spider_settings['random_useragent'] if random_ua else default_ua
            'User-Agent': default_mobile_ua
        }
        return headers

    def fetch(self) -> LightNovel:
        start = time.perf_counter()
        novel_whole = self._fetch()
        self.logger.info('(Perf metrics) Fetch Book took: {} seconds'.format(time.perf_counter() - start))

        return novel_whole

    def _init_http_client(self):
        """
        Tunes http session as needed.

        Guideline: Don't move many concrete init logics to super class __init__()
        """
        self.session = requests.Session()

        if self.spider_settings["disable_proxy"]:
            self.session.trust_env = False

        # cookie example: PHPSESSID=...; night=0; jieqiUserInfo=...; jieqiVisitInfo=...
        if self.spider_settings["http_cookie"]:
            cookie_dict = cookiedict_from_str(self.spider_settings["http_cookie"])
            cookiejar = requests.utils.cookiejar_from_dict(cookie_dict)
            self.session.cookies = cookiejar

    def _crawl_book_basic_info(self, url):
        result = requests_get_with_retry(self.session,
                                         url,
                                         headers=self.request_headers(),
                                         retry_max=self.spider_settings['http_retries'],
                                         timeout=self.spider_settings["http_timeout"],
                                         logger=self.logger)

        if result and result.status_code == 200:
            self.logger.info(f'Succeed to get the novel of book_id: {self.spider_settings["book_id"]}')
            soup = BeautifulSoup(result.text, 'lxml')

            try:
                book_title = soup.find('h1', {'class': 'book-title'}).text
                author = soup.find('div', {'class': 'book-rand-a'}).text[:-2]
                book_summary = soup.find('section', id="bookSummary").text
                # see issue #10, strip invalid suffix characters after ? from cover url
                book_cover_url = soup.find('img', {'class': 'book-cover'})['src'].split("?")[0]

                self.logger.info(f'book name:《{book_title}》')
                return book_title, author, book_summary, book_cover_url
            except (Exception,):
                self.logger.error(f'Failed to parse basic info of book_id: {self.spider_settings["book_id"]}')

        return None

    def _crawl_book_content(self, catalog_url):
        def _anti_js_obfuscation(html):
            """
            recover original text of the novel content.

            :param html:
            :return: html after anti-js obfuscation
            """
            table = str.maketrans(self._mapping_dict)
            res = html.translate(table)
            return res

        def _sanitize_html(html: BeautifulSoup) -> str:
            """
            Strip useless script on body tag by reg or soup library method.
            e.g. <script>zation();</script>

            And remove all the content not needed.

            :param html:
            :return:
            """
            html_copy = BeautifulSoup(str(html), 'lxml')

            # remove <p class="ca1"> 去掉一些公告声明
            anouncements = html_copy.select(".ca1")
            for anouncement in anouncements:
                anouncement.decompose()

            return re.sub(r'<script.+?</script>', '', str(html_copy), flags=re.DOTALL)

        book_catalog_rs = None
        try:
            book_catalog_rs = requests_get_with_retry(self.session,
                                                      catalog_url,
                                                      headers=self.request_headers(),
                                                      retry_max=self.spider_settings['http_retries'],
                                                      timeout=self.spider_settings["http_timeout"],
                                                      logger=self.logger)
        except (Exception,):
            self.logger.error(f'Failed to get normal response of {catalog_url}. It may be a network issue.')

        if book_catalog_rs and book_catalog_rs.status_code == 200:
            self.logger.info(f'Succeed to get the catalog of book_id: {self.spider_settings["book_id"]}')

            catalog_html = book_catalog_rs.text
            catalog_list: List[CatalogLinovelibMobileVolume] = self._convert_to_catalog_list(catalog_html)
            if self.spider_settings['select_volume_mode']:
                catalog_list = self._handle_select_volume(catalog_list)

            new_novel = LightNovel()
            url_next = ''

            volume_id = -1
            for catalog_volume in catalog_list:
                volume_id += 1

                new_volume = LightNovelVolume(volume_id=volume_id)
                new_volume.title = catalog_volume.volume_title
                self.logger.info(f'volume: {catalog_volume.volume_title}')

                chapter_id = -1
                chapter_list: List[LightNovelChapter] = []  # store all chapters of one volume
                for catalog_chapter in catalog_volume.chapters:
                    self.apply_crawl_delay('chapter_crawl_delay')

                    chapter_content = ''
                    chapter_title = catalog_chapter.chapter_title
                    chapter_id += 1

                    light_novel_chapter = LightNovelChapter(chapter_id=chapter_id)
                    light_novel_chapter.title = chapter_title
                    chapter_illustrations: List[LightNovelImage] = []
                    self.logger.info(f'chapter : {chapter_title}')

                    # 这个函数是含有状态的，必须及时覆盖 url_next 变量，否则状态机会失败。
                    # 注意：由于这里并不关心页面内容是否正常，只收集页面链接，因此这里暂时不需要应用请求间隔延迟。
                    url_next = self._expand_paginated_chapter_links(catalog_chapter, url_next)

                    # for loop [chapter_index_url]+[all paginated chapters] links of one chapter
                    for page_link in catalog_chapter.chapter_urls:
                        self.apply_crawl_delay('page_crawl_delay')

                        # retry until get the correct title
                        while True:
                            # use selenium instead of direct requests
                            try:
                                page_resp = self._fetch_page(page_link, max_retries=self.spider_settings['http_retries'])
                                self.logger.debug(f'{page_resp[:100]=}')
                            except (Exception,):
                                continue

                            if page_resp:
                                soup = BeautifulSoup(page_resp, 'lxml')
                            else:
                                raise Exception(f'[ERROR]: request {page_link} failed.')

                            new_title = soup.find(id='atitle')
                            if new_title is not None:
                                break

                        # 分页判断过滤
                        if not new_title.text.startswith(light_novel_chapter.title):
                            # 目录：第二章 可爱如花的 N 孩
                            # 文章页：第二章 可爱如花的女孩，第二章 可爱如花的女孩（2/3），......
                            # 目录页部分文字会被隐藏，所以用文章中的标题代替 new_title。由于 new_title 可能带有分页信息，所以不能 ==
                            self.logger.info(f'chapter : [{light_novel_chapter.title}] New Title= [{new_title.text}]')
                            light_novel_chapter.title = new_title.text

                        images = soup.find_all('img')
                        article_soup = soup.find(id=self._html_content_id)
                        article = _sanitize_html(article_soup)
                        for _, image in enumerate(images):
                            # <img class="imagecontent lazyload" data-src="https://img1.readpai.com/0/28/109869/146248.jpg" src="/images/photon.svg"/>
                            # <img border="0" class="imagecontent" src="https://img1.readpai.com/0/28/109869/146254.jpg"/>
                            html_image_src = re.search('(?<= src=").*?(?=")', str(image))
                            image_lazyload_src = image.get("data-src")

                            if image_lazyload_src:
                                remote_src = re.search('(?<= data-src=").*?(?=")', str(image)).group()
                            else:
                                remote_src = image.get("src")

                            light_novel_image = LightNovelImage(related_page_url=page_link, remote_src=remote_src,
                                                                chapter_id=chapter_id, volume_id=volume_id,
                                                                book_id=self.spider_settings["book_id"])

                            image_local_src = f'{self.spider_settings["image_download_folder"]}/{light_novel_image.local_relative_path}'
                            local_image = str(image).replace(str(html_image_src.group()), image_local_src)
                            article = article.replace(str(image), local_image)
                            chapter_illustrations.append(light_novel_image)

                        article = _anti_js_obfuscation(article)
                        chapter_content += article

                        self.logger.info(f'Processing page... {page_link}')

                    light_novel_chapter.content = chapter_content
                    light_novel_chapter.illustrations = chapter_illustrations
                    chapter_list.append(light_novel_chapter)

                self._remove_duplicate_images_in_html(chapter_list)

                for chapter in chapter_list:
                    new_volume.add_chapter(cid=chapter.chapter_id, title=chapter.title, content=chapter.content,
                                           illustrations=chapter.illustrations)

                new_novel.add_volume(vid=new_volume.volume_id, title=new_volume.title, chapters=new_volume.chapters)

            return new_novel

        else:
            self.logger.error(f'Failed to get the catalog of book_id: {self.spider_settings["book_id"]}')

        return None

    def _fetch_page(self, url: str, max_retries: int = 5) -> str | None:
        if not self._driver:
            self._init_browser_driver()

        driver = self._driver

        request_count = 0
        # total requests num = self(1) + max_retries
        # if max_retries= 5, then total is 1+5=6

        while request_count <= max_retries:
            try:
                driver.get(url)
                html = driver.page_source

                # Determine whether the content of the page has the following tags:
                # - You are being rate limited
                # - 抱歉，章节内容不支持该浏览器显示
                failed_patterns = ['You are being rate limited', '抱歉，章节内容不支持该浏览器显示']
                for pattern in failed_patterns:
                    # 使用正则表达式匹配页面内容
                    match = re.search(pattern, html)
                    if match:
                        raise PageContentIllegalException(f'The page content of {url} is not desired.')

                return html
            except PageContentIllegalException as e:
                self.logger.warn(f"{e.message}")
            except Exception as e:
                self.logger.warn(f"{url} encountered {e.__class__.__name__}.")

            request_count += 1
            # 指数退避参考 https://cloud.google.com/memorystore/docs/redis/exponential-backoff?hl=zh-cn#example_algorithm
            # 具体逻辑：
            # 1.向服务器特定API发出请求。
            # 2.如果请求失败，请等待 1 + random_number_milliseconds 秒后再重试请求。
            # 3.如果请求失败，请等待 2 + random_number_milliseconds 秒后再重试请求。
            # 4.如果请求失败，请等待 4 + random_number_milliseconds 秒后再重试请求。
            # 5.依此类推，等待时间上限为 maximum_backoff。
            # 等待时间达到上限后，您可以继续等待并重试，直到达到重试次数上限（但接下来的重试操作不会增加各次重试之间的等待时间）。

            # 等待时间为 min(((2^n)+random_number_seconds), maximum_backoff)，其中，n 会在每次迭代（请求）后增加 1。
            # 其中：
            # - random_number_seconds 是小于1的秒数（随机值）。
            # - maximum_backoff 设置为一个较大的容忍值，这里设置为10s。这是基于经验的估计。
            n = request_count
            random_number_seconds = round(random.uniform(0, 1), 2)  # 0.01-0.99s
            maximum_backoff = 10
            retry_interval = min(round(((2 ** (n - 1)) + random_number_seconds), 2), maximum_backoff)

            self.logger.warning(
                f'Retrying {url}({request_count}/{max_retries})...; retry_interval: {retry_interval}(s)')
            time.sleep(retry_interval)

        return None

    def _init_browser_driver(self):
        chrome_options = Options()
        # 无头模式
        if not self.spider_settings["not_headless"]:
            chrome_options.add_argument("--headless")

        # 添加自定义 User-Agent
        ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
        chrome_options.add_argument(f"user-agent={ua}")

        # [ERROR:ssl_client_socket_impl.cc(970)] handshake failed;
        # => these arguments are NOT WORK
        chrome_options.add_argument('--ignore-certificate-errors')
        chrome_options.add_argument('--ignore-certificate-errors-spki-list')
        chrome_options.add_argument('--ignore-ssl-errors')

        # suppress logging < FATAL
        # chrome_options.add_argument("log-level=3")

        # 创建一个 Chrome 浏览器实例并传入选项
        driver = webdriver.Chrome(options=chrome_options)
        # page timeout
        timeout = self.spider_settings["http_timeout"] or 10
        driver.set_page_load_timeout(timeout)

        # hardcode one url is ok
        url = 'https://www.bilinovel.com/'
        driver.get(url)
        # 这个刷新只需要初始化一次，是因为第一次 get 无法得到正常结果。后续的请求都不再需要刷新。
        driver.refresh()
        self.logger.info(' 初始化 Driver 完毕...')

        self._driver = driver

    def apply_crawl_delay(self, delay_name):
        crawl_delay = self.spider_settings.get(delay_name, None)
        if crawl_delay:
            time.sleep(crawl_delay)
            self.logger.debug(f'Apply {delay_name}(s): {crawl_delay}')

    def _expand_paginated_chapter_links(self, chapter: CatalogLinovelibMobileChapter, url_next):
        # fix broken links in place(catalog_lis) if exits
        # - if chapter[1] is valid link, assign it to url_next
        # - if chapter[1] is not a valid link,e.g. "javascript:cid(0)" etc. use url_next
        if not self._is_valid_chapter_link(chapter.chapter_url):
            # now the url_next value is the correct link of of chapter[1].
            chapter.chapter_url = url_next
        else:
            url_next = chapter.chapter_url

        # goal: solve all page links of a certain chapter
        while True:
            resp = requests_get_with_retry(self.session, url_next,
                                           headers=self.request_headers(),
                                           retry_max=self.spider_settings['http_retries'],
                                           timeout=self.spider_settings["http_timeout"],
                                           logger=self.logger)
            if resp:
                soup = BeautifulSoup(resp.text, 'lxml')
            else:
                raise Exception(f'[ERROR]: request {url_next} failed.')

            first_script = soup.find("body", {"id": "aread"}).find("script")
            first_script_text = first_script.text
            # alternative: use split(':')[-1] to get read_params_text
            read_params_text = first_script_text[len('var ReadParams='):]
            read_params_json = demjson3.decode(read_params_text)
            url_next = urljoin(f'{self.spider_settings["base_url"]}/novel', read_params_json['url_next'])

            if '_' in url_next:
                chapter.add_expand_paginated_chapter_url(url_next)
            else:
                break

        return url_next

    def _remove_duplicate_images_in_html(self, chapter_list):
        # removing duplicate images in the first chapter
        # chapter_list[0] 表示这一卷的第 1 个章节，在 bilinovel 中是插图页，这个页面部分插图会重复，会出现在这一卷的后续章节中。
        # chapter_list[1:] 表示这一卷的第 2 个章节开始的所有章节，也就是正文章节。

        # 这个函数的作用就是将某一卷的第 1 个章节（插图章节）HTML 的所有重复图片 img 元素，全部去掉。

        def _filter_duplicate_images(match, img_src_list):
            img = match.group()
            img_src = re.search('(?<= src=").*?(?=")', img).group()
            if img_src in img_src_list:
                self.logger.info(f'Remove duplicate image in the first chapter... {img_src}')
                return ""
            else:
                return img

        img_src_list = []

        for chapter in chapter_list[1:]:
            img_src_list.extend(
                [re.search('(?<= src=").*?(?=")', i).group() for i in re.findall('<img.*?/>', chapter.content)]
            )
        chapter_list[0].content = re.sub('<img.*?/>',
                                         lambda match: _filter_duplicate_images(match, img_src_list),
                                         chapter_list[0].content)

    @staticmethod
    def _handle_select_volume(catalog_list: List[CatalogLinovelibMobileVolume]):
        def _reduce_catalog_by_selection(catalog_list: List[CatalogLinovelibMobileVolume], selection_array):
            return [volume for volume in catalog_list if volume.vid in selection_array]

        def _get_volume_choices(catalog_list: List[CatalogLinovelibMobileVolume]):
            return [(volume.volume_title, volume.vid) for volume in catalog_list]

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

    def _convert_to_catalog_list(self, catalog_html) -> List[CatalogLinovelibMobileVolume]:
        soup_catalog = BeautifulSoup(catalog_html, 'lxml')
        # chapter_count = soup_catalog.find('h4', {'class': 'chapter-sub-title'}).find('output').text
        catalog_wrapper = soup_catalog.find('div', {'id': 'volumes'})
        catalog_volumes = catalog_wrapper.find_all('div', {'class': 'catalog-volume'})

        # catalog html structure:
        #     <div class="catalog-volume">
        #         <ul class="volume-chapters">
        #             <li class="chapter-bar chapter-li"><h3> 第一章『卡利娅·巴德尼克篇』</h3></li>
        #             <li class="volume-cover chapter-li">...</li>
        #             <li class="chapter-li jsChapter">
        #               <a href="/novel/3087/153701.html" class="chapter-li-a "><span class="chapter-index "> 作品相关 </span></a>
        #             </li>

        catalog_list: List[CatalogLinovelibMobileVolume] = []

        _current_chapters: List[CatalogLinovelibMobileChapter] = []
        _current_volume_title = ""
        _volume_index = 0

        for catalog_volume in catalog_volumes:
            volume_chapters = catalog_volume.find("ul", {'class': 'volume-chapters'})
            volume_chapter_items = volume_chapters.find_all('li')

            for volume_chapter_item in volume_chapter_items:
                # is volume name
                if volume_chapter_item.name == 'li' and 'chapter-bar' in volume_chapter_item['class']:
                    _volume_index += 1
                    _current_volume_title = volume_chapter_item.get_text()
                    _current_chapters: List[CatalogLinovelibMobileChapter] = []
                    new_volume = CatalogLinovelibMobileVolume(
                        vid=_volume_index,
                        volume_title=_current_volume_title,
                        chapters=_current_chapters
                    )
                    catalog_list.append(new_volume)
                # is normal chapter
                elif volume_chapter_item.name == 'li' and 'jsChapter' in volume_chapter_item['class']:
                    href = volume_chapter_item.find("a")["href"]
                    chapter_url = urljoin(f'{self.spider_settings["base_url"]}/novel', href)
                    new_chapter: CatalogLinovelibMobileChapter = CatalogLinovelibMobileChapter(
                        chapter_title=volume_chapter_item.get_text(),
                        chapter_url=chapter_url
                    )
                    _current_chapters.append(new_chapter)

        # sanitize catalog_list => remove volume that has empty chapters
        # https://w.linovelib.com/novel/3847/catalog
        # {'vid': 3, 'volume_title': ' 第四卷 ', 'chapters': []}
        catalog_list = [catalog_volume for catalog_volume in catalog_list if catalog_volume.chapters]
        return catalog_list

    @staticmethod
    def _is_valid_chapter_link(href: str):
        # normal link example: https://w.linovelib.com/novel/682/117077.html
        # broken link example: javascript: cid(0)
        # use https://regex101.com/ to debug regular expression
        reg = r"\S+/novel/\d+/\S+\.html"
        re_match = bool(re.match(reg, href))
        return re_match

    @staticmethod
    def _extract_image_list(image_dict=None):
        image_url_list = []
        for volume_images in image_dict.values():
            for index in range(0, len(volume_images)):
                image_url_list.append(volume_images[index])

        return image_url_list

    def _fetch(self):
        book_url = f'{self.spider_settings["base_url"]}/novel/{self.spider_settings["book_id"]}.html'
        book_catalog_url = f'{self.spider_settings["base_url"]}/novel/{self.spider_settings["book_id"]}/catalog'
        create_folder_if_not_exists(self.spider_settings['pickle_temp_folder'])

        book_basic_info = self._crawl_book_basic_info(book_url)
        if not book_basic_info:
            raise LinovelibException(f'Fetch book_basic_info of {self.spider_settings["book_id"]} failed.')

        new_novel_with_content = self._crawl_book_content(book_catalog_url)
        if not new_novel_with_content:
            raise LinovelibException(f'Fetch book_content of {self.spider_settings["book_id"]} failed.')

        # do better: use named tuple or class like NovelBasicInfoGroup
        book_title, author, book_summary, book_cover = book_basic_info
        novel_whole = new_novel_with_content
        novel_whole.mark_volumes_content_ready()

        # set book basic info
        novel_whole.book_id = self.spider_settings['book_id']
        novel_whole.book_title = book_title
        novel_whole.author = author
        novel_whole.description = book_summary
        novel_whole.book_cover = LightNovelImage(related_page_url=book_url,
                                                 remote_src=book_cover,
                                                 book_id=self.spider_settings["book_id"],
                                                 is_book_cover=True)
        novel_whole.mark_basic_info_ready()

        return novel_whole
