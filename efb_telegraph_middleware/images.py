"""Image download, hotlink detection, compression and re-hosting.

Telegraph pages load images straight from their source. Many Chinese CDNs
(WeChat, Weibo, Xiaohongshu, Bilibili ...) reject requests whose Referer is
``telegra.ph`` and serve a placeholder or 403 instead, so those images are
downloaded here and re-hosted on a GitHub repository.
"""

import base64
import hashlib
import io
import logging
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from .http import DESKTOP_UA
from .urls import domain_matches, host_of

logger = logging.getLogger(__name__)

# CDNs known to block foreign Referers; skip the probe request for them.
PROTECTED_DOMAINS = [
    "qpic.cn", "qlogo.cn", "gtimg.com", "sinaimg.cn", "sinajs.cn", "xhscdn.com", "xhscdn.net",
    "hdslb.com", "biliimg.com", "douyinpic.com", "byteimg.com", "pstatp.com", "toutiaoimg.com",
    "zhimg.com", "csdnimg.cn", "jianshu.io", "126.net", "127.net", "baidu.com", "bdimg.com",
    "bdstatic.com", "kuaishou.com", "yximgs.com", "smzdmimg.com", "ithome.com",
]
MAX_IMAGE_BYTES = 15 * 1024 * 1024
TELEGRAPH_REFERER = "https://telegra.ph/"


@dataclass
class Downloaded:
    data: bytes
    content_type: str


class GitHubStore:
    """Commit images to a GitHub repository, one commit per batch."""

    def __init__(self, repo: str, token: str, branch: str = "main", path_prefix: str = "",
                 url_template: str = "https://raw.githubusercontent.com/{repo}/{branch}/{path}",
                 proxy: Optional[str] = None, auto_create: bool = True, private: bool = False):
        self.repo = repo
        self.branch = branch
        self.prefix = path_prefix.strip("/")
        self.url_template = url_template
        self.auto_create = auto_create
        self.private = private
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "efb-telegraph-middleware",
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.lock = threading.Lock()
        self._ready = False

    def url_for(self, path: str) -> str:
        return self.url_template.format(repo=self.repo, branch=self.branch, path=path)

    def _api(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        return self.session.request(method, f"https://api.github.com/repos/{self.repo}{path}", **kwargs)

    def ensure_repo(self) -> None:
        if self._ready:
            return
        r = self._api("GET", "")
        if r.status_code == 404 and self.auto_create:
            owner, name = self.repo.split("/", 1)
            me = self.session.get("https://api.github.com/user", timeout=30).json().get("login")
            endpoint = "https://api.github.com/user/repos" if me == owner else \
                f"https://api.github.com/orgs/{owner}/repos"
            created = self.session.post(endpoint, json={
                "name": name, "private": self.private, "auto_init": True,
                "description": "Images re-hosted by efb-telegraph-middleware"}, timeout=30)
            if created.status_code not in (201, 422):
                raise RuntimeError(f"cannot create {self.repo}: {created.status_code} {created.text[:200]}")
            time.sleep(2)
        elif r.status_code >= 400:
            raise RuntimeError(f"cannot access {self.repo}: {r.status_code} {r.text[:200]}")
        self._ready = True

    def upload(self, files: Dict[str, bytes], message: str) -> Dict[str, str]:
        """Upload ``{path: data}`` in a single commit; return ``{path: url}``."""
        if not files:
            return {}
        self.ensure_repo()
        blobs: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {path: pool.submit(self._blob, data) for path, data in files.items()}
            for path, future in futures.items():
                blobs[path] = future.result()
        with self.lock:
            for attempt in range(4):
                ref = self._api("GET", f"/git/ref/heads/{self.branch}")
                ref.raise_for_status()
                head = ref.json()["object"]["sha"]
                base_tree = self._api("GET", f"/git/commits/{head}").json()["tree"]["sha"]
                tree = self._api("POST", "/git/trees", json={
                    "base_tree": base_tree,
                    "tree": [{"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in blobs.items()],
                })
                tree.raise_for_status()
                commit = self._api("POST", "/git/commits", json={
                    "message": message, "tree": tree.json()["sha"], "parents": [head]})
                commit.raise_for_status()
                update = self._api("PATCH", f"/git/refs/heads/{self.branch}",
                                   json={"sha": commit.json()["sha"], "force": False})
                if update.status_code < 400:
                    break
                if update.status_code != 422 or attempt == 3:
                    update.raise_for_status()
                time.sleep(1 + attempt)  # branch moved underneath us: retry on new head
        return {path: self.url_for(path) for path in files}

    def _blob(self, data: bytes) -> str:
        r = self._api("POST", "/git/blobs", json={
            "content": base64.b64encode(data).decode(), "encoding": "base64"}, timeout=60)
        r.raise_for_status()
        return r.json()["sha"]


def compress(data: bytes, max_side: int, quality: int) -> Tuple[bytes, str]:
    """Shrink large still images; return ``(bytes, extension)``."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return data, _sniff_ext(data)
    try:
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        if fmt == "GIF" or getattr(img, "is_animated", False):
            return data, "gif" if fmt == "GIF" else _sniff_ext(data)
        img.load()
    except Exception:
        return data, _sniff_ext(data)
    resized = False
    if max_side and max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
        resized = True
    if not resized and len(data) <= 400 * 1024 and fmt in ("JPEG", "PNG", "WEBP"):
        return data, {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[fmt]
    out = io.BytesIO()
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        img.save(out, "PNG", optimize=True)
        ext = "png"
    else:
        img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        ext = "jpg"
    result = out.getvalue()
    if not resized and len(result) >= len(data):
        return data, _sniff_ext(data)
    return result, ext


def _sniff_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _is_image(data: bytes, content_type: str) -> bool:
    if content_type.startswith("image/"):
        return True
    return data.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8")) or \
        (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


class ImageProcessor:
    def __init__(self, mode: str = "protected", store: Optional[GitHubStore] = None,
                 proxy_template: str = "", proxy: Optional[str] = None,
                 extra_protected: Optional[List[str]] = None, max_side: int = 1600,
                 quality: int = 85, workers: int = 8, cache=None):
        self.mode = mode
        self.store = store
        self.proxy_template = proxy_template
        self.protected = PROTECTED_DOMAINS + list(extra_protected or [])
        self.max_side = max_side
        self.quality = quality
        self.workers = workers
        self.cache = cache
        self.session = requests.Session()
        self.session.headers["User-Agent"] = DESKTOP_UA
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _get(self, url: str, referer: Optional[str], timeout: float) -> Optional[Downloaded]:
        headers = {"Referer": referer} if referer else {}
        try:
            r = self.session.get(url, headers=headers, timeout=timeout, stream=True)
            if r.status_code >= 400:
                r.close()
                return None
            body = bytearray()
            for chunk in r.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES:
                    r.close()
                    return None
            ctype = (r.headers.get("Content-Type") or "").lower()
            data = bytes(body)
            return Downloaded(data, ctype) if _is_image(data, ctype) else None
        except requests.RequestException:
            return None

    def _needs_rehost(self, url: str, original: Downloaded, timeout: float) -> bool:
        if self.mode == "all":
            return True
        if domain_matches(host_of(url), self.protected):
            return True
        probe = self._get(url, TELEGRAPH_REFERER, timeout)
        return probe is None or hashlib.sha1(probe.data).digest() != hashlib.sha1(original.data).digest()

    def _prepare(self, url: str, referer: Optional[str], timeout: float):
        """Return (url, (path, data, digest) | None, new_url | None)."""
        original = self._get(url, referer, timeout) or self._get(url, None, timeout)
        if original is None:
            return url, None, None
        if not self._needs_rehost(url, original, timeout):
            return url, None, None
        if self.store is None:
            if self.proxy_template:
                return url, None, self.proxy_template.format(
                    url=urllib.parse.quote(url, safe=""), raw_url=url)
            return url, None, None
        data, ext = compress(original.data, self.max_side, self.quality)
        digest = hashlib.sha256(data).hexdigest()
        if self.cache is not None:
            cached = self.cache.get_image(digest)
            if cached:
                return url, None, cached
        stamp = time.strftime("%Y/%m")
        prefix = (self.store.prefix + "/") if self.store.prefix else ""
        return url, (f"{prefix}{stamp}/{digest[:24]}.{ext}", data, digest), None

    def process(self, images: List[Dict[str, Any]], referer: Optional[str], deadline: float,
                title: str = "") -> int:
        """Rewrite ``src`` of image nodes in place; return how many were re-hosted."""
        if self.mode == "none" or not images:
            return 0
        urls = list(dict.fromkeys(node["attrs"]["src"] for node in images if node.get("attrs", {}).get("src")))
        replacements: Dict[str, str] = {}
        uploads: Dict[str, Tuple[List[str], bytes, str]] = {}
        per_request = 12
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._prepare, u, referer, per_request) for u in urls]
            done, pending = wait(futures, timeout=max(1.0, deadline - time.time()))
            for future in pending:
                future.cancel()
            for future in done:
                try:
                    url, upload, new_url = future.result()
                except Exception:
                    logger.debug("image prepare failed", exc_info=True)
                    continue
                if new_url:
                    replacements[url] = new_url
                elif upload:
                    path, data, digest = upload
                    if path in uploads:
                        uploads[path][0].append(url)
                    else:
                        uploads[path] = ([url], data, digest)
        if uploads and self.store is not None and time.time() < deadline + 15:
            try:
                urls_by_path = self.store.upload({p: d for p, (_, d, _) in uploads.items()},
                                                 f"archive: {title[:60] or 'images'}")
                for path, new_url in urls_by_path.items():
                    original_urls, _, digest = uploads[path]
                    for original_url in original_urls:
                        replacements[original_url] = new_url
                    if self.cache is not None:
                        self.cache.put_image(digest, new_url)
            except Exception:
                logger.warning("GitHub image upload failed", exc_info=True)
        for node in images:
            src = node.get("attrs", {}).get("src")
            if src in replacements:
                node["attrs"]["src"] = replacements[src]
        return sum(1 for node in images if node.get("attrs", {}).get("src") in replacements.values())
