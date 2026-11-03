"""HTTP session helpers."""

from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
WECHAT_UA = MOBILE_UA + " MicroMessenger/8.0.50(0x1800322d) NetType/WIFI Language/zh_CN"

MAX_HTML_BYTES = 12 * 1024 * 1024


class FetchError(RuntimeError):
    pass


@dataclass
class Page:
    url: str
    html: str
    content_type: str


def make_session(proxy: Optional[str] = None, user_agent: str = DESKTOP_UA,
                 pool: int = 8) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _sniff_encoding(body: bytes) -> Optional[str]:
    """Guess the charset of already-downloaded bytes (no extra HTTP reads)."""
    if not body:
        return None
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body[:65536]).best()
        if best and best.encoding:
            return best.encoding
    except Exception:
        pass
    head = body[:4096].lower()
    if b"charset=utf-8" in head:
        return "utf-8"
    if b"charset=gb" in head:
        return "gb18030"
    return None


def fetch_html(session: requests.Session, url: str, timeout: float = 12,
               headers: Optional[dict] = None) -> Page:
    response = session.get(url, timeout=(min(6.0, timeout), timeout), headers=headers or {},
                           stream=True, allow_redirects=True)
    try:
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code}")
        ctype = (response.headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype:
            raise FetchError(f"not a web page: {ctype.split(';')[0]}")
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_HTML_BYTES:
                raise FetchError("page too large")
        encoding = response.encoding
        if not encoding or encoding.lower() in ("iso-8859-1", "ascii"):
            head = bytes(body[:4096]).lower()
            if b"charset=gb" in head or b"charset=\"gb" in head:
                encoding = "gb18030"
            else:
                # Do NOT use response.apparent_encoding here: it re-reads the
                # response, which is already consumed (requests raises
                # "The content for this response was already consumed").
                encoding = _sniff_encoding(bytes(body)) or "utf-8"
        if encoding.lower() in ("gb2312", "gbk"):
            encoding = "gb18030"
        return Page(url=response.url, html=bytes(body).decode(encoding, errors="replace"),
                    content_type=ctype)
    finally:
        response.close()
