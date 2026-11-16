"""Archero2 Hub 菜单标题检索；仅允许固定站点中文页面。"""
from urllib.parse import urljoin, urlsplit
import requests
from bs4 import BeautifulSoup

BASE = 'https://archero2hub.com'


class HubError(RuntimeError):
    pass


class ArcheroHub:
    def _get(self, url):
        try:
            # 禁止重定向至未知站点；限制页面体积及请求等待时间。
            with requests.get(url, timeout=(5, 10), allow_redirects=False, stream=True) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise HubError('页面返回异常状态')
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise HubError('页面过大')
                    chunks.append(chunk)
                return b''.join(chunks).decode('utf-8')
        except (requests.RequestException, UnicodeError) as exc:
            raise HubError('攻略站读取失败') from exc

    def lookup(self, keyword):
        keyword = keyword.strip().casefold()
        if not keyword:
            return []
        soup = BeautifulSoup(self._get(BASE + '/zh'), 'html.parser')
        if not soup.select('nav a[href]'):
            raise HubError('攻略目录不可用')
        candidates = {}
        for a in soup.select('nav a[href]'):
            title_node = a.select_one('span.truncate')
            # 简介仅描述附带内容，不应作为搜索主题，避免弱匹配挤掉相关标题。
            title = (title_node or a).get_text(' ', strip=True)
            if keyword not in title.casefold():
                continue
            url = urljoin(BASE, a['href'])
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.netloc != 'archero2hub.com' or not parsed.path.startswith('/zh/'):
                continue
            if parsed.query or parsed.fragment:
                continue
            candidates.setdefault(url, title)
        def relevance(item):
            title = item[1].casefold()
            return 0 if title == keyword else 1 if title.startswith(keyword) else 2

        ranked = sorted(candidates.items(), key=relevance)
        return [{'title': title, 'url': url} for url, title in ranked[:5]]
