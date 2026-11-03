"""Turn a URL into an archived Telegraph page. Independent of EFB."""

import datetime
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cache import Cache
from .extractors import Article, extract
from .http import make_session
from .images import GitHubStore, ImageProcessor
from .nodes import Node, link
from .telegraph import Telegraph
from .urls import host_of, normalize, url_matches

logger = logging.getLogger(__name__)


class Archiver:
    def __init__(self, config: Dict[str, Any], data_path: Path):
        self.config = config
        self.data_path = Path(data_path)
        self.cache = Cache(self.data_path / "cache.sqlite3", config.get("cache_days", 30))
        self.session = make_session(config.get("proxy") or None)
        fallback = config.get("fallback_proxy") or ""
        self.fallback_session = make_session(fallback) if fallback and fallback != config.get("proxy") else None
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telegraph-images")
        self.telegraph = Telegraph(
            token=config.get("telegraph_token") or "",
            short_name=config.get("telegraph_short_name") or "EFB",
            author_name=config.get("author_name") or "",
            proxy=config.get("telegraph_proxy") or None,
            token_file=self.data_path / "telegraph_token",
        )
        self.images = self._build_image_processor(config.get("image_host") or {})
        self._inflight: Dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()

    def _build_image_processor(self, cfg: Dict[str, Any]) -> ImageProcessor:
        kind = (cfg.get("type") or "none").lower()
        rehost = (cfg.get("rehost") or "protected").lower()
        store = None
        if kind == "github":
            if cfg.get("repo") and cfg.get("token"):
                store = GitHubStore(
                    repo=cfg["repo"], token=cfg["token"], branch=cfg.get("branch") or "main",
                    path_prefix=cfg.get("path_prefix") or "", url_template=cfg.get("url_template")
                    or "https://raw.githubusercontent.com/{repo}/{branch}/{path}",
                    proxy=cfg.get("api_proxy") or None, private=bool(cfg.get("private")))
            else:
                logger.warning("image_host.type=github but repo/token missing; images are not re-hosted")
                rehost = "none"
        elif kind == "proxy":
            if not cfg.get("proxy_template"):
                rehost = "none"
        else:
            rehost = "none"
        return ImageProcessor(
            mode=rehost, store=store, proxy_template=cfg.get("proxy_template") or "",
            proxy=self.config.get("proxy") or None,
            extra_protected=cfg.get("extra_protected_domains") or [],
            max_side=int(cfg.get("max_side") or 1600), quality=int(cfg.get("jpeg_quality") or 85),
            cache=self.cache)

    # ------------------------------------------------------------------
    def should_archive(self, url: str) -> bool:
        if not url or not url.startswith(("http://", "https://")):
            return False
        only = self.config.get("only_domains") or []
        if only and not url_matches(url, only):
            return False
        return not url_matches(url, self.config.get("skip_domains") or [])

    def archive(self, url: str) -> Optional[str]:
        """Return the Telegraph URL for ``url`` or None if it cannot be archived."""
        if not self.should_archive(url):
            return None
        key = normalize(url)
        cached = self.cache.get_page(key)
        if cached:
            return cached
        # The same link is often forwarded to several chats at once.
        with self._inflight_lock:
            event = self._inflight.get(key)
            owner = event is None
            if owner:
                event = self._inflight[key] = threading.Event()
        if not owner:
            event.wait(self.config.get("timeout", 25) + 20)
            return self.cache.get_page(key)
        try:
            page_url = self._archive(url)
            if page_url:
                self.cache.put_page(key, page_url, "")
            return page_url
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            event.set()

    def _archive(self, url: str) -> Optional[str]:
        started = time.time()
        budget = float(self.config.get("timeout", 25))
        article = extract(self.session, url, timeout=min(15.0, budget),
                          min_text=int(self.config.get("min_text_length", 80)),
                          fallback_session=self.fallback_session,
                          parsehub_api=self.config.get("parsehub_api") or "")
        if article is None:
            return None
        nodes = list(article.nodes)
        if self.config.get("footer", True):
            nodes += self.footer(article, url)
        # Publish right away with the original image URLs: Telegram fetches
        # Instant View images server-side without a telegra.ph Referer, so the
        # preview already works. Re-hosting (for browsers and long-term
        # archiving) happens in the background and edits the same page.
        pages = self.telegraph.create_pages(article.title, nodes,
                                            author_name=article.author or article.site, author_url=url)
        logger.info("Archived %s -> %s (%d images, %.1fs)", url, pages[0].url,
                    len(article.images), time.time() - started)
        if article.images and self.images.mode != "none":
            self.executor.submit(self._rehost_images, article, pages)
        return pages[0].url

    def _rehost_images(self, article: Article, pages) -> None:
        started = time.time()
        try:
            before = [img["attrs"]["src"] for img in article.images]
            deadline = started + float(self.config.get("image_timeout", 300))
            count = self.images.process(article.images, article.referer, deadline, article.title)
            after = [img["attrs"]["src"] for img in article.images]
            if before != after:
                self.telegraph.edit_pages(pages)
            logger.info("Re-hosted %d/%d images of %s (%.1fs)", count, len(article.images),
                        pages[0].url, time.time() - started)
        except Exception:
            logger.warning("Re-hosting images of %s failed", pages[0].url, exc_info=True)

    @staticmethod
    def footer(article: Article, original_url: str) -> List[Node]:
        meta = [x for x in (article.site, article.author) if x]
        if article.published:
            meta.append(article.published.strftime("%Y-%m-%d %H:%M"))
        nodes: List[Node] = [{"tag": "hr"}]
        if meta:
            nodes.append({"tag": "p", "children": [{"tag": "i", "children": [" · ".join(meta)]}]})
        nodes.append({"tag": "p", "children": ["原文：", link(original_url, host_of(original_url) + " ↗")]})
        nodes.append({"tag": "p", "children": [{"tag": "i", "children": [
            "存档于 " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")]}]})
        return nodes
