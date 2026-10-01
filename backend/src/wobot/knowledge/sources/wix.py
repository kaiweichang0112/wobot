"""Wix pages → text blocks in page order, the structure GRC and G-Tech parsers read."""

from dataclasses import dataclass
from typing import Literal

from bs4 import BeautifulSoup, Tag

# Wix sprinkles zero-width spaces through its text, even inside dates ("201​3/12/31").
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


@dataclass(frozen=True)
class Link:
    text: str
    # None for an anchor that leads nowhere: legacy Wix links carry only a `dataquery`
    # whose target is missing from the site too, so the live page cannot follow them.
    url: str | None


@dataclass(frozen=True)
class Block:
    # The Wix text element holding the block. Repeater items share a suffix after "__",
    # which pairs, say, a year with the list beside it.
    element_id: str
    kind: Literal["heading", "paragraph", "item", "blank"]
    text: str
    links: tuple[Link, ...] = ()
    level: int | None = None  # headings only

    @property
    def repeater_item(self) -> str | None:
        _, separator, item = self.element_id.partition("__")
        return item if separator else None


def clean_block_text(text: str) -> str:
    """Zero-width characters out, no-break spaces in as spaces, runs of spaces collapsed.

    Line breaks stay, each line trimmed.
    """
    lines = text.translate(_ZERO_WIDTH).replace("\xa0", " ").split("\n")
    return "\n".join(" ".join(line.split()) for line in lines).strip()


def page_blocks(html: str) -> list[Block]:
    """Every heading, paragraph and list item of the page's text elements, in page order."""
    soup = BeautifulSoup(html, "lxml")
    blocks = []
    for element in soup.select('[data-testid="richTextElement"]'):
        element_id = element.get("id", "")
        for node in element.find_all([*_HEADINGS, "p", "li"]):
            if node.name == "p" and node.find_parent("li") is not None:
                continue  # read as part of its list item
            blocks.append(_block(element_id, node))
    return blocks


def _block(element_id: str, node: Tag) -> Block:
    for line_break in node.find_all("br"):
        line_break.replace_with("\n")
    # Text nodes join with no separator: Wix splits one word over many <span>s
    # ("N" + "TD1,900,000"), and a space between them would corrupt amounts and dates.
    text = clean_block_text(node.get_text(""))
    links = tuple(
        Link(text=clean_block_text(anchor.get_text("")), url=anchor.get("href") or None)
        for anchor in node.find_all("a")
    )
    if node.name in _HEADINGS:
        return Block(element_id, "heading", text, links, level=int(node.name[1]))
    if node.name == "li":
        return Block(element_id, "item", text, links)
    return Block(element_id, "paragraph" if text else "blank", text, links)
