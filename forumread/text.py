"""Text cleanup: HTML bodies (HN, Stack Exchange) to plain text, Reddit markdown unescaping."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "ul", "ol", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr", "hr"}
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\ufeff]")


def clean_markdown(text: str | None) -> str:
    """Unescape HTML entities in Reddit-style markdown and drop invisible characters."""
    if not text:
        return ""
    text = html.unescape(text).replace("&#x200B;", "")
    return _ZERO_WIDTH.sub("", text).replace("\r\n", "\n").strip()


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._pre = 0
        self._quote = 0
        self._links: list[tuple[str, int]] = []  # (href, index in out where link text starts)

    def _break(self) -> None:
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "br":
            self.out.append("\n")
        elif tag == "pre":
            self._break()
            self.out.append("```\n")
            self._pre += 1
        elif tag == "code" and not self._pre:
            self.out.append("`")
        elif tag == "li":
            self._break()
            self.out.append("- ")
        elif tag == "a":
            self._links.append((attr.get("href") or "", len(self.out)))
        elif tag == "img":
            self.out.append(f"[image: {attr['alt']}]" if attr.get("alt") else "[image]")
        elif tag == "blockquote":
            self._break()
            self._quote += 1
        elif tag in _BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._break()
            self.out.append("```\n")
        elif tag == "code" and not self._pre:
            self.out.append("`")
        elif tag == "a" and self._links:
            href, start = self._links.pop()
            label = "".join(self.out[start:])
            if href.startswith("http") and href != label:
                del self.out[start:]
                if label.endswith(("...", "\u2026")) or not label.strip():
                    self.out.append(href)  # HN truncates long URLs in link text
                else:
                    self.out.append(f"[{label}]({href})")
        elif tag == "blockquote":
            self._quote = max(0, self._quote - 1)
            self._break()
        elif tag in _BLOCK_TAGS or tag == "li":
            self._break()

    def handle_data(self, data: str) -> None:
        if self._pre:
            self.out.append(data)
        else:
            data = re.sub(r"\s+", " ", data)
            if not self.out or self.out[-1].endswith("\n"):
                data = data.lstrip()
                if not data:
                    return
                if self._quote:
                    data = "> " + data
            self.out.append(data)


def html_to_text(markup: str | None) -> str:
    """Convert an HTML fragment to compact plain text with fenced code and `- ` list items."""
    if not markup:
        return ""
    converter = _Converter()
    converter.feed(markup)
    converter.close()
    text = "".join(converter.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _ZERO_WIDTH.sub("", text).strip()
