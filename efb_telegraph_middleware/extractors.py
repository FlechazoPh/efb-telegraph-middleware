"""Site specific and generic article extractors.

Every extractor returns an :class:`Article` or ``None`` when the URL does not
contain something worth archiving (login wall, deleted post, product page...).
"""

import datetime
import html as html_lib
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from .http import DESKTOP_UA, MOBILE_UA, WECHAT_UA, FetchError, fetch_html
from .nodes import Converted, Node, html_to_nodes, link, paragraph, text_length
from .urls import host_of

logger = logging.getLogger(__name__)


@dataclass
class Article:
    url: str
    title: str
    nodes: List[Node]
    images: List[Dict[str, Any]] = field(default_factory=list)
    author: str = ""
    site: str = ""
    published: Optional[datetime.datetime] = None
    referer: Optional[str] = None  # Referer to use when downloading images

    @property
    def text_len(self) -> int:
        return text_length(self.nodes)


class Unavailable(RuntimeError):
    """The page exists but has no archivable content (deleted, login wall...)."""


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return html_lib.unescape(tag["content"]).strip()
    return ""


def _from_ts(value) -> Optional[datetime.datetime]:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value > 10 ** 12:
        value //= 1000
    try:
        return datetime.datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return None


def _figure(src: str) -> Dict[str, Any]:
    return {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": src}}]}


def _plain_text(nodes: List[Node]) -> str:
    parts = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict) and node.get("tag") not in ("figure", "img"):
            parts.append(_plain_text(node.get("children") or []))
            if node.get("tag") in ("p", "h3", "h4", "blockquote", "li"):
                parts.append("\n")
    return "".join(parts).strip()


def _collect_images(nodes: List[Node]) -> List[Dict[str, Any]]:
    out = []
    for node in nodes:
        if isinstance(node, dict):
            if node.get("tag") == "img":
                out.append(node)
            out.extend(_collect_images(node.get("children") or []))
    return out


# ---------------------------------------------------------------- WeChat ---
_WX_GONE = ("该内容已被发布者删除", "此内容因违规无法查看", "该公众号已迁移", "此内容被投诉且经审核涉嫌",
            "该内容已被删除", "该账号已被屏蔽", "内容无法查看")


def extract_weixin(session: requests.Session, url: str, timeout: float) -> Article:
    page = fetch_html(session, url, timeout, headers={"User-Agent": WECHAT_UA})
    text = page.html
    soup = BeautifulSoup(text, "lxml")
    if "环境异常" in text and "js_content" not in text:
        raise Unavailable("WeChat verification page")
    content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    title = ""
    tag = soup.select_one("#activity-name") or soup.select_one(".rich_media_title")
    if tag:
        title = tag.get_text(" ", strip=True)
    title = title or _meta(soup, "og:title", "twitter:title")
    if not title:
        m = re.search(r"var msg_title = '(.*?)'", text)
        title = html_lib.unescape(m.group(1)) if m else ""
    author = ""
    tag = soup.select_one("#js_name")
    if tag:
        author = tag.get_text(" ", strip=True)
    if not author:
        m = re.search(r'var nickname\s*=\s*(?:htmlDecode\()?"([^"]*)"', text)
        author = html_lib.unescape(m.group(1)) if m else ""
    m = re.search(r'var (?:ct|create_time)\s*=\s*"?(\d{9,})"?', text)
    published = _from_ts(m.group(1)) if m else None

    if content is not None and content.get_text(strip=True) or (content is not None and content.find("img")):
        converted = html_to_nodes(content, page.url)
        nodes, images = converted.nodes, converted.images
    else:
        # "图片消息" / short posts: images live in a JS list, text in description
        if any(flag in text for flag in _WX_GONE):
            raise Unavailable("WeChat article removed")
        nodes, images = [], []
        desc = _meta(soup, "og:description", "description")
        m = re.search(r"content_noencode\s*:\s*JsDecode\('(.*?)'\)", text, re.S)
        if m:
            desc = html_lib.unescape(m.group(1).encode().decode("unicode_escape", "ignore"))
        for line in (desc or "").replace("\\x0a", "\n").splitlines():
            if line.strip():
                nodes.append(paragraph(line.strip()))
        for cdn in re.findall(r"cdn_url\s*:\s*'([^']+)'", text) or \
                re.findall(r'"cdn_url"\s*:\s*"([^"]+)"', text):
            fig = _figure(html_lib.unescape(cdn).replace("\\/", "/"))
            nodes.append(fig)
            images.append(fig["children"][0])
    if not nodes:
        raise Unavailable("empty WeChat article")
    return Article(url=page.url, title=title or "微信文章", nodes=nodes, images=images,
                   author=author, site="微信公众号", published=published,
                   referer="https://mp.weixin.qq.com/")


# ---------------------------------------------------------- Xiaohongshu ---
def _find_note(obj, depth=0):
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if ("imageList" in obj or "video" in obj) and "type" in obj and ("desc" in obj or "title" in obj):
            return obj
        for value in obj.values():
            found = _find_note(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj[:5]:
            found = _find_note(value, depth + 1)
            if found:
                return found
    return None


def _xhs_image_url(item: Dict[str, Any]) -> Optional[str]:
    for key in ("urlDefault", "url", "urlPre"):
        if item.get(key):
            return item[key]
    for info in item.get("infoList") or []:
        if info.get("url"):
            return info["url"]
    return None


def extract_xiaohongshu(session: requests.Session, url: str, timeout: float) -> Article:
    page = fetch_html(session, url, timeout, headers={"User-Agent": MOBILE_UA})
    if "/login" in urllib.parse.urlsplit(page.url).path:
        raise Unavailable("Xiaohongshu login wall")
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", page.html, re.S)
    if not m:
        raise Unavailable("Xiaohongshu state missing")
    state = json.loads(re.sub(r"\bundefined\b", "null", m.group(1)))
    note = _find_note(state)
    if not note:
        raise Unavailable("Xiaohongshu note not found")
    desc = re.sub(r"\[话题\]#", "#", note.get("desc") or "")
    desc = re.sub(r"#([^#\s]+)#", r"#\1", desc)
    nodes: List[Node] = []
    images = []
    if note.get("type") == "video":
        nodes.append(paragraph("（视频笔记，下面是封面，视频请看原文）"))
    for line in desc.splitlines():
        if line.strip():
            nodes.append(paragraph(line.strip()))
    for item in note.get("imageList") or []:
        src = _xhs_image_url(item or {})
        if src:
            src = "https:" + src if src.startswith("//") else src
            fig = _figure(src)
            nodes.append(fig)
            images.append(fig["children"][0])
    tags = [t.get("name") for t in note.get("tagList") or [] if t.get("name")]
    if tags:
        nodes.append(paragraph(" ".join("#" + t for t in tags)))
    if not nodes:
        raise Unavailable("empty Xiaohongshu note")
    user = (note.get("user") or {}).get("nickname") or (note.get("user") or {}).get("nickName") or ""
    title = note.get("title") or (desc.strip().splitlines() or ["小红书笔记"])[0][:60]
    return Article(url=page.url, title=title, nodes=nodes, images=images, author=user,
                   site="小红书", published=_from_ts(note.get("time")),
                   referer="https://www.xiaohongshu.com/")


# ---------------------------------------------------------------- Weibo ----
_WEIBO_ID = re.compile(r"/(?:detail|status|\d+)/([A-Za-z0-9]{9,})")


def extract_weibo(session: requests.Session, url: str, timeout: float) -> Article:
    parts = urllib.parse.urlsplit(url)
    m = _WEIBO_ID.search(parts.path)
    if not m:
        raise Unavailable("not a Weibo status URL")
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://m.weibo.cn/",
               "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
    r = session.get("https://m.weibo.cn/statuses/show", params={"id": m.group(1)},
                    headers=headers, timeout=timeout)
    try:
        data = (r.json() or {}).get("data") or {}
    except ValueError:
        raise Unavailable("Weibo API did not return JSON")
    if not data:
        raise Unavailable("Weibo status unavailable")

    def render(status: Dict[str, Any]) -> Converted:
        text = status.get("text") or ""
        if status.get("isLongText"):
            try:
                ext = session.get("https://m.weibo.cn/statuses/extend", params={"id": status.get("id")},
                                  headers=headers, timeout=timeout).json()
                text = ((ext or {}).get("data") or {}).get("longTextContent") or text
            except Exception:
                pass
        soup = BeautifulSoup(f"<div>{text}</div>", "lxml")
        for icon in soup.select("span.url-icon"):
            alt = icon.find("img")
            icon.replace_with(alt.get("alt", "") if alt else "")
        converted = html_to_nodes(soup.div, "https://m.weibo.cn/")
        for pic in status.get("pics") or []:
            src = ((pic.get("large") or {}).get("url")) or pic.get("url")
            if src:
                fig = _figure(src)
                converted.nodes.append(fig)
                converted.images.append(fig["children"][0])
        page_info = status.get("page_info") or {}
        if page_info.get("type") == "video":
            cover = (page_info.get("page_pic") or {}).get("url")
            if cover:
                fig = _figure(cover)
                converted.nodes.append(fig)
                converted.images.append(fig["children"][0])
            converted.nodes.append(paragraph("（含视频，请看原文）"))
        return converted

    main = render(data)
    nodes, images = main.nodes, main.images
    retweet = data.get("retweeted_status")
    if retweet:
        inner = render(retweet)
        who = (retweet.get("user") or {}).get("screen_name") or "原微博"
        nodes.append({"tag": "blockquote", "children": [f"转发自 @{who}"]})
        for node in inner.nodes:
            if isinstance(node, dict) and node.get("tag") == "p":
                nodes.append({"tag": "blockquote", "children": node["children"]})
            else:
                nodes.append(node)
        images += inner.images
    user = (data.get("user") or {}).get("screen_name") or ""
    plain = _plain_text(main.nodes)
    title = (data.get("status_title") or (plain.splitlines()[0] if plain else "") or "微博")[:60]
    published = None
    try:
        published = datetime.datetime.strptime(data.get("created_at", ""), "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        pass
    return Article(url=f"https://m.weibo.cn/detail/{data.get('id')}", title=title or "微博",
                   nodes=nodes, images=images, author=user, site="微博", published=published,
                   referer="https://m.weibo.cn/")


# --------------------------------------------------------------- generic ---
def _prepare_generic(soup: BeautifulSoup) -> None:
    for img in soup.find_all("img"):
        for attr in ("data-src", "data-original", "data-lazy-src", "data-actualsrc", "data-original-src"):
            if img.get(attr) and not str(img.get(attr)).startswith("data:"):
                img["src"] = img[attr]
                break
    for tag in soup(["script", "style", "noscript", "iframe", "form", "nav", "footer"]):
        tag.decompose()


def extract_generic(session: requests.Session, url: str, timeout: float,
                    min_text: int = 80) -> Article:
    from readability import Document  # imported lazily: optional heavy dependency

    page = fetch_html(session, url, timeout)
    soup = BeautifulSoup(page.html, "lxml")
    title = _meta(soup, "og:title", "twitter:title")
    author = _meta(soup, "author", "article:author", "og:article:author")
    site = _meta(soup, "og:site_name", "application-name") or host_of(page.url)
    published = None
    stamp = _meta(soup, "article:published_time", "og:release_date", "pubdate", "publishdate")
    if stamp:
        try:
            published = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            pass
    description = _meta(soup, "og:description", "description", "twitter:description")
    cover = _meta(soup, "og:image", "twitter:image")

    _prepare_generic(soup)
    document = Document(str(soup), url=page.url)
    title = title or document.short_title() or (soup.title.get_text(strip=True) if soup.title else "")
    converted = html_to_nodes(document.summary(html_partial=True), page.url)
    nodes, images = converted.nodes, converted.images
    if converted.text_len < min_text and not (images and converted.text_len >= 10):
        # Readability found nothing useful; fall back to the page summary.
        if description and len(description) >= 12:
            nodes = [paragraph(description)]
            images = []
            if cover:
                fig = _figure(urllib.parse.urljoin(page.url, cover))
                nodes.insert(0, fig)
                images = [fig["children"][0]]
        else:
            raise Unavailable("no readable content")
    return Article(url=page.url, title=title or host_of(page.url), nodes=nodes, images=images,
                   author=author, site=site, published=published, referer=page.url)


# -------------------------------------------------------------- dispatch ---
Extractor = Callable[[requests.Session, str, float], Article]
SITE_EXTRACTORS: List[tuple] = [
    (("mp.weixin.qq.com",), extract_weixin),
    (("xiaohongshu.com", "xhslink.com"), extract_xiaohongshu),
    (("weibo.com", "weibo.cn", "m.weibo.cn"), extract_weibo),
]


def pick_extractor(url: str) -> Extractor:
    host = host_of(url)
    for hosts, extractor in SITE_EXTRACTORS:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return extractor
    return extract_generic


def _blocked(exc: Exception) -> bool:
    """Errors that a different network path (proxy) may get around."""
    if isinstance(exc, FetchError):
        return any(code in str(exc) for code in ("HTTP 403", "HTTP 451", "HTTP 502", "HTTP 503", "HTTP 504"))
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def extract(session: requests.Session, url: str, timeout: float = 12,
            min_text: int = 80, fallback_session: Optional[requests.Session] = None) -> Optional[Article]:
    """Extract an article, returning None when there is nothing to archive."""
    extractor = pick_extractor(url)

    def run(sess: requests.Session) -> Article:
        if extractor is extract_generic:
            return extract_generic(sess, url, timeout, min_text=min_text)
        return extractor(sess, url, timeout)

    try:
        try:
            article = run(session)
        except (FetchError, requests.RequestException) as exc:
            if fallback_session is None or not _blocked(exc):
                raise
            logger.info("Retry %s through fallback proxy (%s)", url, exc)
            article = run(fallback_session)
    except (Unavailable, FetchError, requests.RequestException, ValueError) as exc:
        logger.info("Skip %s: %s", url, exc)
        return None
    if extractor is not extract_generic and not article.images:
        article.images = _collect_images(article.nodes)
    return article
