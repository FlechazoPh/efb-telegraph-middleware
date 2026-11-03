"""URL helpers: discovery in free text, domain matching and normalisation."""

import re
import urllib.parse
from typing import Iterable, List

# Stop at whitespace, quotes, angle brackets and common CJK punctuation that
# people type right after a pasted link.
URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、；：（）【】《》「」]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\"…"

# Query parameters that only identify the sharer / campaign. They make the
# same article look like different URLs, which defeats the page cache.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {
    "share_token", "share_from", "share_source", "share_medium", "sharer", "share_id",
    "shareRedId", "share_from_user_hidden", "apptime", "appuid", "app_platform",
    "app_version", "author_share", "xhsshare", "exSource", "spm", "from", "sourcetype",
    "wm", "rp_trans", "scene", "clicktime", "enterid", "sessionid", "subscene",
    "ascene", "devicetype", "version", "nettype", "lang", "pass_ticket", "wx_header",
    "chksm", "mpshare", "srcid", "sharer_shareinfo", "sharer_shareinfo_first",
}
# WeChat articles: only these identify the article.
_WEIXIN_KEYS = ("__biz", "mid", "idx", "sn")


def extract_urls(text: str) -> List[str]:
    """Return the http(s) URLs in ``text`` in order of appearance, deduplicated."""
    seen, out = set(), []
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(_TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def domain_matches(host: str, patterns: Iterable[str]) -> bool:
    """True if ``host`` equals a pattern or is a subdomain of it.

    A pattern may also carry a path prefix, e.g. ``weixin.qq.com/sph``; it is
    matched against ``host + path`` in :func:`url_matches`.
    """
    host = (host or "").lower()
    for pattern in patterns:
        pattern = pattern.lower().strip()
        if not pattern or "/" in pattern:
            continue
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def url_matches(url: str, patterns: Iterable[str]) -> bool:
    patterns = list(patterns)
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if domain_matches(host, patterns):
        return True
    location = host + (parts.path or "/")
    for pattern in patterns:
        pattern = pattern.lower().strip()
        if "/" not in pattern:
            continue
        p_host, _, p_path = pattern.partition("/")
        if (host == p_host or host.endswith("." + p_host)) and \
                location[len(host):].lower().startswith("/" + p_path):
            return True
    return False


def normalize(url: str) -> str:
    """Canonical form used as the page-cache key."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    host = (parts.hostname or "").lower()
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if host == "mp.weixin.qq.com" and parts.path.rstrip("/") == "/s" and \
            any(k == "__biz" for k, _ in query):
        query = [(k, v) for k, v in query if k in _WEIXIN_KEYS]
    else:
        query = [(k, v) for k, v in query
                 if k not in _TRACKING_KEYS and not k.startswith(_TRACKING_PREFIXES)]
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit(
        (scheme, netloc, parts.path or "/", urllib.parse.urlencode(query), ""))
