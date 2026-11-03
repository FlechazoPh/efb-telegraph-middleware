"""EFB middleware: archive every shared link to Telegraph for Instant View."""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from ehforwarderbot import Message, MsgType, Middleware, coordinator
from ehforwarderbot.message import LinkAttribute
from ehforwarderbot.utils import get_config_path, get_data_path

from .archiver import Archiver
from .config import load_config
from .urls import extract_urls

__version__ = "1.0.0"
__all__ = ["TelegraphMiddleware", "__version__"]


class TelegraphMiddleware(Middleware):
    """Archive links (WeChat share cards and URLs in text) to Telegraph.

    Share cards get the Telegraph page as their preview link, so Telegram shows
    an Instant View; URLs in text messages get the Telegraph link appended.
    Images that block hotlinking are re-hosted so the pages keep working.
    """

    middleware_id = "flechazo.telegraph"
    middleware_name = "Telegraph Archive Middleware"
    __version__ = __version__

    def __init__(self, instance_id: Optional[str] = None):
        super().__init__(instance_id)
        self.logger = logging.getLogger(f"plugins.{self.middleware_id}")
        self.config = load_config(get_config_path(self.middleware_id))
        self.archiver = Archiver(self.config, get_data_path(self.middleware_id))

    def process_message(self, message: Message) -> Optional[Message]:
        try:
            if message.deliver_to != coordinator.master or getattr(message, "edit", False):
                return message
            if message.type == MsgType.Link and isinstance(message.attributes, LinkAttribute):
                if self.config.get("process_links", True):
                    self._process_link(message)
            elif message.type == MsgType.Text and message.text:
                if self.config.get("process_text_urls", True):
                    self._process_text(message)
        except Exception:
            self.logger.exception("Failed to archive links of message %s", getattr(message, "uid", "?"))
        return message

    def _safe_archive(self, url: str) -> Optional[str]:
        try:
            return self.archiver.archive(url)
        except Exception:
            self.logger.exception("Failed to archive %s", url)
            return None

    def _process_link(self, message: Message) -> None:
        attributes: LinkAttribute = message.attributes
        page_url = self.archiver.archive(attributes.url or "")
        if page_url:
            # ETM renders `image` as the first link of the message, which makes
            # Telegram build the preview (Instant View) from the Telegraph page.
            attributes.image = page_url

    def _process_text(self, message: Message) -> None:
        limit = int(self.config.get("max_text_urls", 3) or 0)
        urls = [u for u in extract_urls(message.text) if self.archiver.should_archive(u)][:limit]
        if not urls:
            return
        with ThreadPoolExecutor(max_workers=len(urls)) as pool:
            results = list(pool.map(self._safe_archive, urls))
        pages = []
        for page_url in results:
            if page_url and page_url not in pages:
                pages.append(page_url)
        if pages:
            prefix = self.config.get("text_prefix", "📰 ")
            message.text = message.text.rstrip() + "\n\n" + "\n".join(prefix + p for p in pages)
