"""Convert arbitrary article HTML into Telegraph's restricted node format.

Telegraph only understands a small set of tags and renders top-level inline
content poorly, so the converter flattens the source tree into a list of
block nodes (``p``, ``h3``, ``figure`` ...) whose children are text or inline
nodes (``a``, ``b``, ``i`` ...). Images are always lifted to their own
``figure`` block, wherever they appear in the source.
"""

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

Node = Union[str, Dict[str, Any]]

INLINE_MAP = {
    "a": "a", "b": "b", "strong": "strong", "i": "i", "em": "em", "u": "u",
    "ins": "u", "s": "s", "del": "s", "strike": "s", "code": "code",
    "kbd": "code", "samp": "code", "tt": "code", "mark": "b", "cite": "i",
}
HEADINGS = {"h1": "h3", "h2": "h3", "h3": "h3", "h4": "h4", "h5": "h4", "h6": "h4"}
SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "button", "input",
        "select", "textarea", "form", "head", "title", "meta", "link", "nav", "audio",
        "map", "object", "embed", "mpvoice", "mp-common-profile", "mp-style-type"}
BLOCKS = {"p", "div", "section", "article", "main", "header", "footer", "aside",
          "center", "body", "html", "figure", "dl", "dd", "dt", "address", "details",
          "summary", "table", "tbody", "thead", "tfoot", "tr", "td", "th", "caption",
          "picture", "fieldset", "li", "ul", "ol", "blockquote", "pre", "hr"}
LAZY_SRC = ("data-src", "data-original", "data-lazy-src", "data-actualsrc", "data-url",
            "data-original-src", "data-echo", "data-croporisrc", "src")
_WS = re.compile(r"[ \t\r\n\f\v\u00a0\u200b\u3000]+")


@dataclass
class Converted:
    nodes: List[Node] = field(default_factory=list)
    images: List[Dict[str, Any]] = field(default_factory=list)
    text_len: int = 0


def image_src(tag: Tag, base_url: str) -> Optional[str]:
    """Best real image URL of an <img>, resolving lazy-loading attributes."""
    for attr in LAZY_SRC:
        value = (tag.get(attr) or "").strip()
        if not value or value.startswith("data:") or value.endswith(".svg"):
            continue
        if value.startswith("//"):
            value = "https:" + value
        url = urllib.parse.urljoin(base_url, value)
        if url.startswith(("http://", "https://")):
            return url
    srcset = tag.get("srcset") or tag.get("data-srcset") or ""
    if srcset:
        last = srcset.split(",")[-1].strip().split(" ")[0]
        if last:
            return urllib.parse.urljoin(base_url, "https:" + last if last.startswith("//") else last)
    return None


def _is_hidden(tag: Tag) -> bool:
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or tag.has_attr("hidden") or \
        (tag.get("aria-hidden") == "true" and tag.name not in ("img",))


def _tiny(tag: Tag) -> bool:
    for attr in ("width", "height"):
        value = str(tag.get(attr) or "").strip().rstrip("px")
        if value.isdigit() and int(value) <= 2:
            return True
    return False


class _Builder:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.blocks: List[Node] = []
        self.inline: List[Node] = []
        self.images: List[Dict[str, Any]] = []
        self.seen_images = set()

    # -- paragraph handling ------------------------------------------------
    def flush(self) -> None:
        children = _trim(self.inline)
        self.inline = []
        if children:
            self.blocks.append({"tag": "p", "children": children})

    def add_block(self, node: Dict[str, Any]) -> None:
        self.flush()
        self.blocks.append(node)

    def add_image(self, tag: Tag, caption: Optional[str] = None) -> None:
        if _tiny(tag):
            return
        src = image_src(tag, self.base_url)
        if not src or src in self.seen_images:
            return
        self.seen_images.add(src)
        img = {"tag": "img", "attrs": {"src": src}}
        self.images.append(img)
        children: List[Node] = [img]
        if caption:
            children.append({"tag": "figcaption", "children": [caption]})
        self.add_block({"tag": "figure", "children": children})

    # -- traversal ---------------------------------------------------------
    def walk(self, element: Tag) -> None:
        for child in list(element.children):
            self.visit(child)

    def visit(self, child) -> None:
        if isinstance(child, Comment):
            return
        if isinstance(child, NavigableString):
            text = _WS.sub(" ", str(child))
            if text:
                self.inline.append(text)
            return
        if not isinstance(child, Tag):
            return
        name = (child.name or "").lower()
        if name in SKIP or _is_hidden(child):
            return
        if name == "img":
            self.add_image(child, (child.get("alt") or "").strip() or None
                           if child.parent is not None and child.parent.name == "figure" else None)
            return
        if name == "br":
            self.inline.append("\n")
            return
        if name == "hr":
            self.add_block({"tag": "hr"})
            return
        if name in ("iframe", "video"):
            self._media_link(child)
            return
        if name in HEADINGS:
            content = _trim(self.inline_children(child))
            if content:
                self.add_block({"tag": HEADINGS[name], "children": content})
            for img in child.find_all("img"):
                self.add_image(img)
            return
        if name == "pre":
            text = child.get_text()
            if text.strip():
                self.add_block({"tag": "pre", "children": [text.strip("\n")]})
            return
        if name in ("ul", "ol"):
            self._list(child, name)
            return
        if name == "blockquote":
            if child.find(["img", "p", "div", "section", "ul", "ol"]):
                self.flush()
                inner = _Builder(self.base_url)
                inner.seen_images = self.seen_images
                inner.walk(child)
                inner.flush()
                for block in inner.blocks:
                    if isinstance(block, dict) and block.get("tag") == "p":
                        self.add_block({"tag": "blockquote", "children": block["children"]})
                    else:
                        self.add_block(block)
                self.images.extend(inner.images)
            else:
                content = _trim(self.inline_children(child))
                if content:
                    self.add_block({"tag": "blockquote", "children": content})
            return
        if name == "figure":
            self.flush()
            caption_tag = child.find("figcaption")
            caption = _WS.sub(" ", caption_tag.get_text()).strip() if caption_tag else None
            imgs = child.find_all("img")
            if imgs:
                for i, img in enumerate(imgs):
                    self.add_image(img, caption if i == len(imgs) - 1 else None)
                return
            self.walk(child)
            self.flush()
            return
        if name in ("tr",):
            cells = [_WS.sub(" ", c.get_text()).strip() for c in child.find_all(["td", "th"], recursive=False)]
            cells = [c for c in cells if c]
            if cells:
                self.add_block({"tag": "p", "children": [" | ".join(cells)]})
            for img in child.find_all("img"):
                self.add_image(img)
            return
        if name in INLINE_MAP and not child.find(["img", "p", "div", "section", "table", "ul", "ol",
                                                  "h1", "h2", "h3", "h4", "figure", "blockquote"]):
            node = self.inline_node(child)
            if isinstance(node, list):
                self.inline.extend(node)
            elif node is not None:
                self.inline.append(node)
            return
        if name in BLOCKS:
            self.flush()
            self.walk(child)
            self.flush()
            return
        # span, font, label and unknown tags: transparent inline containers
        self.walk(child)

    def inline_children(self, element: Tag) -> List[Node]:
        out: List[Node] = []
        for child in element.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                text = _WS.sub(" ", str(child))
                if text:
                    out.append(text)
            elif isinstance(child, Tag):
                name = (child.name or "").lower()
                if name in SKIP or name == "img" or _is_hidden(child):
                    continue
                if name == "br":
                    out.append("\n")
                    continue
                node = self.inline_node(child)
                if node is None:
                    continue
                if isinstance(node, list):
                    out.extend(node)
                else:
                    out.append(node)
        return out

    def inline_node(self, tag: Tag):
        name = (tag.name or "").lower()
        children = self.inline_children(tag)
        if not any(isinstance(c, dict) or c.strip() for c in children):
            return None
        mapped = INLINE_MAP.get(name)
        if mapped is None:
            return children
        node: Dict[str, Any] = {"tag": mapped, "children": children}
        if mapped == "a":
            href = (tag.get("href") or "").strip()
            if href.startswith("//"):
                href = "https:" + href
            href = urllib.parse.urljoin(self.base_url, href) if href else ""
            if not href.startswith(("http://", "https://", "mailto:")):
                return children
            node["attrs"] = {"href": href}
        return node

    def _list(self, tag: Tag, name: str) -> None:
        items = []
        for li in tag.find_all("li", recursive=False):
            content = _trim(self.inline_children(li))
            if content:
                items.append({"tag": "li", "children": content})
        if items:
            self.add_block({"tag": name, "children": items})
        for img in tag.find_all("img"):
            self.add_image(img)

    def _media_link(self, tag: Tag) -> None:
        src = ""
        for attr in ("data-src", "src", "data-mpvid"):
            src = (tag.get(attr) or "").strip()
            if src:
                break
        if not src:
            source = tag.find("source")
            src = (source.get("src") or "").strip() if source else ""
        poster = tag.get("poster") or tag.get("data-cover")
        if poster:
            fake = BeautifulSoup("<img>", "html.parser").img
            fake["src"] = urllib.parse.unquote(poster) if "%3A" in poster else poster
            self.add_image(fake)
        if src and not src.startswith("data:"):
            href = urllib.parse.urljoin(self.base_url, "https:" + src if src.startswith("//") else src)
            if href.startswith(("http://", "https://")):
                self.add_block({"tag": "p", "children": [
                    "▶ ", {"tag": "a", "attrs": {"href": href}, "children": ["视频"]}]})


def _trim(children: List[Node]) -> List[Node]:
    """Merge adjacent strings, collapse spaces around newlines, strip ends."""
    merged: List[Node] = []
    for child in children:
        if isinstance(child, str) and merged and isinstance(merged[-1], str):
            merged[-1] += child
        else:
            merged.append(child)
    out: List[Node] = []
    for child in merged:
        if isinstance(child, str):
            child = re.sub(r" *\n+ *", "\n", child)
            child = re.sub(r" {2,}", " ", child)
        out.append(child)
    while out and isinstance(out[0], str) and not out[0].strip():
        out.pop(0)
    while out and isinstance(out[-1], str) and not out[-1].strip():
        out.pop()
    if out and isinstance(out[0], str):
        out[0] = out[0].lstrip()
    if out and isinstance(out[-1], str):
        out[-1] = out[-1].rstrip()
    if not any(isinstance(c, dict) or c.strip() for c in out):
        return []
    return out


def text_length(nodes: List[Node]) -> int:
    total = 0
    for node in nodes:
        if isinstance(node, str):
            total += len(node.strip())
        elif isinstance(node, dict):
            total += text_length(node.get("children") or [])
    return total


def html_to_nodes(html_or_tag: Union[str, Tag], base_url: str) -> Converted:
    if isinstance(html_or_tag, Tag):
        root = html_or_tag
    else:
        root = BeautifulSoup(html_or_tag or "", "lxml")
        root = root.body or root
    builder = _Builder(base_url)
    builder.walk(root)
    builder.flush()
    blocks = [b for b in builder.blocks if not (isinstance(b, dict) and b.get("tag") == "p"
                                                 and not text_length(b.get("children") or [])
                                                 and not _has_tag(b, "a"))]
    return Converted(nodes=blocks, images=builder.images, text_len=text_length(blocks))


def _has_tag(node: Node, tag: str) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("tag") == tag:
        return True
    return any(_has_tag(c, tag) for c in node.get("children") or [])


def paragraph(text: str) -> Dict[str, Any]:
    return {"tag": "p", "children": [text]}


def link(href: str, text: Optional[str] = None) -> Dict[str, Any]:
    return {"tag": "a", "attrs": {"href": href}, "children": [text or href]}
