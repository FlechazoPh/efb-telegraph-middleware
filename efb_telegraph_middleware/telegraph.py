"""Minimal Telegraph API client with account bootstrap and page splitting."""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .nodes import Node

logger = logging.getLogger(__name__)

API = "https://api.telegra.ph"
# Telegraph rejects content above 64 KiB; keep a margin for the footer.
MAX_CONTENT_BYTES = 60 * 1024


class TelegraphError(RuntimeError):
    pass


def _size(nodes: List[Node]) -> int:
    return len(json.dumps(nodes, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _shrink(node: Node, limit: int) -> Node:
    """Truncate a single oversized node so it fits into ``limit`` bytes."""
    if isinstance(node, str):
        data = node.encode("utf-8")[: max(0, limit - 16)]
        return data.decode("utf-8", "ignore") + "…"
    children = node.get("children") or []
    kept: List[Node] = []
    for child in children:
        candidate = dict(node, children=kept + [child])
        if _size([candidate]) > limit:
            room = limit - _size([dict(node, children=kept)])
            if room > 32:
                kept.append(_shrink(child, room))
            break
        kept.append(child)
    return dict(node, children=kept)


def split_pages(nodes: List[Node], limit: int = MAX_CONTENT_BYTES) -> List[List[Node]]:
    pages: List[List[Node]] = []
    current: List[Node] = []
    for node in nodes:
        if _size([node]) > limit:
            node = _shrink(node, limit)
        if current and _size(current + [node]) > limit:
            pages.append(current)
            current = []
        current.append(node)
    if current:
        pages.append(current)
    return pages or [[{"tag": "p", "children": [" "]}]]


class Telegraph:
    def __init__(self, token: str = "", short_name: str = "EFB", author_name: str = "",
                 proxy: Optional[str] = None, token_file: Optional[Path] = None, timeout: float = 20):
        self.token = (token or "").strip()
        self.short_name = short_name
        self.author_name = author_name
        self.token_file = Path(token_file) if token_file else None
        self.timeout = timeout
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _call(self, method: str, **params: Any) -> Dict[str, Any]:
        for attempt in range(2):
            r = self.session.post(f"{API}/{method}", data=params, timeout=self.timeout)
            payload = r.json()
            if payload.get("ok"):
                return payload["result"]
            error = str(payload.get("error"))
            m = re.match(r"FLOOD_WAIT_(\d+)", error)
            if m and attempt == 0 and int(m.group(1)) <= 15:
                time.sleep(int(m.group(1)))
                continue
            raise TelegraphError(error)
        raise TelegraphError("retry exhausted")

    def ensure_token(self) -> str:
        if self.token:
            return self.token
        if self.token_file and self.token_file.exists():
            self.token = self.token_file.read_text().strip()
            if self.token:
                return self.token
        result = self._call("createAccount", short_name=self.short_name[:32],
                            author_name=self.author_name[:128])
        self.token = result["access_token"]
        if self.token_file:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(self.token)
            try:
                self.token_file.chmod(0o600)
            except OSError:
                pass
        logger.info("Created a new Telegraph account (token saved to %s)", self.token_file)
        return self.token

    def create_page(self, title: str, nodes: List[Node], author_name: str = "",
                    author_url: str = "", limit: int = MAX_CONTENT_BYTES) -> str:
        """Create one or more chained pages; return the URL of the first page."""
        return self.create_pages(title, nodes, author_name, author_url, limit)[0].url

    def create_pages(self, title: str, nodes: List[Node], author_name: str = "",
                     author_url: str = "", limit: int = MAX_CONTENT_BYTES) -> List["PageRef"]:
        """Create chained pages (first page first in the returned list).

        The ``content`` of each returned :class:`PageRef` shares the node
        objects passed in, so callers can mutate image ``src`` attributes and
        push the changes with :meth:`edit_pages`.
        """
        token = self.ensure_token()
        title = (title or "Untitled").strip()[:200]
        author_name = (author_name or self.author_name)[:128]
        author_url = author_url[:512] if author_url.startswith(("http://", "https://")) else ""
        chunks = split_pages(nodes, limit)
        refs: List[PageRef] = []
        next_url = ""
        for index in range(len(chunks) - 1, -1, -1):
            content = list(chunks[index])
            if next_url:
                content.append({"tag": "p", "children": [
                    {"tag": "a", "attrs": {"href": next_url}, "children": [f"下一页 ({index + 2}/{len(chunks)}) →"]}]})
            page_title = (title if len(chunks) == 1 else f"{title} ({index + 1}/{len(chunks)})")[:256]
            try:
                result = self._call("createPage", access_token=token, title=page_title,
                                    author_name=author_name, author_url=author_url,
                                    content=json.dumps(content, ensure_ascii=False),
                                    return_content="false")
            except TelegraphError as exc:
                # Telegraph measures size differently from us (e.g. escaping);
                # start over with smaller chunks.
                if "CONTENT_TOO_BIG" in str(exc) and limit > 8 * 1024:
                    return self.create_pages(title, nodes, author_name, author_url, limit // 2)
                # The page path is derived from the title plus a "-MM-DD-N"
                # suffix; long titles that already exist overflow it.
                if "PAGE_SAVE_FAILED" in str(exc) and len(title) > 40:
                    return self.create_pages(title[:40].rstrip() + "…", nodes, author_name, author_url, limit)
                raise
            next_url = result["url"]
            refs.insert(0, PageRef(path=result["path"], url=result["url"], title=page_title,
                                   content=content, author_name=author_name, author_url=author_url))
        return refs

    def edit_pages(self, pages: List["PageRef"]) -> None:
        token = self.ensure_token()
        for page in pages:
            self._call("editPage", access_token=token, path=page.path, title=page.title,
                       author_name=page.author_name, author_url=page.author_url,
                       content=json.dumps(page.content, ensure_ascii=False), return_content="false")


class PageRef:
    def __init__(self, path: str, url: str, title: str, content: List[Node],
                 author_name: str = "", author_url: str = ""):
        self.path = path
        self.url = url
        self.title = title
        self.content = content
        self.author_name = author_name
        self.author_url = author_url
