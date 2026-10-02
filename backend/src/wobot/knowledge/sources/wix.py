"""Wix pages → text blocks in page order, the structure GRC and G-Tech parsers read.

The Docusaurus reader yields the same blocks, so one section parser serves both sites.
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

# Wix sprinkles zero-width spaces through its text, even inside dates ("201​3/12/31").
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
IMAGE_HOST = "static.wixstatic.com"
# A resized copy's size, "/v1/fill/w_774,h_531,...": the size shown when the page states none.
_SHOWN_SIZE = re.compile(r"/w_(\d+),h_(\d+)")


@dataclass(frozen=True)
class Link:
    text: str
    # None for an anchor that leads nowhere: legacy Wix links carry only a `dataquery`
    # whose target is missing from the site too, so the live page cannot follow them.
    url: str | None


@dataclass(frozen=True)
class Image:
    url: str  # the file itself, which a page may show resized; relative on Docusaurus pages
    alt: str
    # The size the page shows it at, in CSS pixels, when it says; icons are small.
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class Block:
    # The Wix text element holding the block. Repeater items share a suffix after "__",
    # which pairs, say, a year with the list beside it.
    element_id: str
    # A button is a link outside the text, such as a catalog to download; a table and a
    # code block come from Docusaurus pages only. An image's text is its alt text.
    kind: Literal["heading", "paragraph", "item", "blank", "button", "table", "code", "image"]
    text: str
    links: tuple[Link, ...] = ()
    level: int | None = None  # headings only
    rows: tuple[tuple[str, ...], ...] = ()  # tables only, the header row first
    image: Image | None = None  # images only

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


def page_blocks(html: str, *, buttons: bool = False, images: bool = False) -> list[Block]:
    """Every heading, paragraph and list item of the page's text elements, in page order,
    with the link buttons among them when `buttons` is set and the images when `images` is."""
    soup = BeautifulSoup(html, "lxml")
    selector = '[data-testid="richTextElement"]'
    if buttons:
        selector += ', a[data-testid="linkElement"]'
    if images:
        selector += ", img"
    blocks = []
    for element in soup.select(selector):
        if element.name == "img":
            if (block := _image(element)) is not None:
                blocks.append(block)
            continue
        if element.name == "a":
            if element.find_parent(attrs={"data-testid": "richTextElement"}) is None:
                blocks.append(_button(element))
            continue
        element_id = element.get("id", "")
        for node in element.find_all([*_HEADINGS, "p", "li"]):
            if node.name == "p" and node.find_parent("li") is not None:
                continue  # read as part of its list item
            blocks.append(_block(element_id, node))
    return blocks


def _button(anchor: Tag) -> Block:
    """A link button, named after the nearest element with an ID: the same button on every
    page, such as a menu entry, then shares its ID like any other chrome."""
    owner = anchor if anchor.get("id") else anchor.find_parent(id=True)
    label = clean_block_text(anchor.get_text(" "))
    link = Link(text=label, url=anchor.get("href") or None)
    return Block(owner.get("id", "") if owner else "", "button", label, (link,))


def _image(img: Tag) -> Block | None:
    """An image the site stores, as its original file rather than the resized copy shown.

    Wix shows a picture, a section's background or a gallery item through the same host;
    a background's first source is a blurred placeholder, but its path still names the file.
    """
    src = img.get("src") or ""
    parts = urlsplit(src)
    if parts.hostname != IMAGE_HOST or not parts.path.startswith("/media/"):
        return None  # a tracking pixel, or an image from elsewhere
    name = parts.path.removeprefix("/media/").split("/")[0]
    width, height = _number(img.get("width")), _number(img.get("height"))
    if (width is None or height is None) and (shown := _SHOWN_SIZE.search(parts.path)):
        width, height = int(shown[1]), int(shown[2])
    owner = img.find_parent(id=True)
    alt = clean_block_text(img.get("alt") or "")
    image = Image(f"https://{IMAGE_HOST}/media/{name}", alt, width, height)
    return Block(owner.get("id", "") if owner else "", "image", alt, image=image)


def _number(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.isdigit() else None


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
