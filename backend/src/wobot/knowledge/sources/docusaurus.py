"""Docusaurus pages → the blocks Wix pages yield: headings, paragraphs, list items, tables
and code, in page order, from the article's Markdown body only.

Navigation, the table of contents and the footer sit outside that body and are never
read. Images are yielded where they stand, for the visual step; an embedded video's
address is kept as a link.
"""

from bs4 import BeautifulSoup, NavigableString, Tag

from wobot.knowledge.sources.wix import Block, Image, Link, clean_block_text

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# Wrappers whose children are read as if they stood in the body: callouts, folded
# sections, tabs.
_CONTAINERS = ("div", "section", "blockquote", "details")


def article_blocks(html: str) -> list[Block]:
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one("article .theme-doc-markdown")
    if body is None:
        return []
    blocks: list[Block] = []
    _read(body, blocks)
    return blocks


def _read(node: Tag, blocks: list[Block]) -> None:
    for child in node.children:
        if not isinstance(child, Tag):
            continue
        if child.name == "header":
            _read(child, blocks)
        elif child.name in _HEADINGS:
            text = _text(child)
            if text:
                blocks.append(Block("", "heading", text, _links(child), level=int(child.name[1])))
        elif child.name == "p":
            _paragraph(child, blocks)
        elif child.name in ("ul", "ol"):
            _list(child, blocks, depth=0)
        elif child.name == "table":
            _table(child, blocks)
        elif child.name == "pre":
            _code(child, blocks)
        elif child.name == "summary":
            _paragraph(child, blocks)
        elif child.name == "iframe" and child.get("src"):
            blocks.append(Block("", "button", "", (Link("video", child["src"]),)))
        elif child.name == "img":
            _image(child, blocks)
        elif child.name in _CONTAINERS:
            _read(child, blocks)


def _paragraph(node: Tag, blocks: list[Block]) -> None:
    text = _text(node)
    if text:
        blocks.append(Block("", "paragraph", text, _links(node)))
    for frame in node.find_all("iframe"):
        if frame.get("src"):
            blocks.append(Block("", "button", "", (Link("video", frame["src"]),)))
    for img in node.find_all("img"):
        _image(img, blocks)


def _image(img: Tag, blocks: list[Block]) -> None:
    if src := img.get("src"):
        alt = clean_block_text(img.get("alt") or "")
        width, height = (_number(img.get(name)) for name in ("width", "height"))
        blocks.append(Block("", "image", alt, image=Image(src, alt, width, height)))


def _number(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _list(node: Tag, blocks: list[Block], depth: int) -> None:
    """Each item on its own, nested items indented under theirs."""
    for item in node.find_all("li", recursive=False):
        nested = [child for child in item.children if isinstance(child, Tag)]
        own = [child for child in nested if child.name not in ("ul", "ol")]
        text = clean_block_text(
            "".join(
                part.get_text("") if isinstance(part, Tag) else str(part)
                for part in item.children
                if not (isinstance(part, Tag) and part.name in ("ul", "ol"))
            )
        )
        if text:
            links = tuple(link for part in own for link in _links(part))
            blocks.append(Block("", "item", "  " * depth + text, links))
        for sublist in (child for child in nested if child.name in ("ul", "ol")):
            _list(sublist, blocks, depth + 1)


def _table(node: Tag, blocks: list[Block]) -> None:
    rows = tuple(
        tuple(clean_block_text(cell.get_text("")) for cell in row.find_all(["th", "td"]))
        for row in node.find_all("tr")
    )
    if rows:
        blocks.append(Block("", "table", "", _links(node), rows=rows))


def _code(pre: Tag, blocks: list[Block]) -> None:
    """Code keeps its lines and their indentation; only trailing spaces go."""
    lines = pre.select(".token-line")
    if lines:
        text = "\n".join(line.get_text("").rstrip() for line in lines)
    else:
        text = pre.get_text("")
    text = text.strip("\n")
    if text.strip():
        blocks.append(Block("", "code", text))


def _text(node: Tag) -> str:
    """The node's text without heading anchors, which Docusaurus fills with a zero-width
    space."""
    parts = []
    for part in node.descendants:
        if isinstance(part, NavigableString) and not any(
            "hash-link" in parent.get("class", []) for parent in part.parents if parent is not node
        ):
            parts.append(str(part))
    return clean_block_text("".join(parts))


def _links(node: Tag) -> tuple[Link, ...]:
    return tuple(
        Link(clean_block_text(anchor.get_text("")), anchor["href"])
        for anchor in node.find_all("a", href=True)
        if "hash-link" not in anchor.get("class", [])
    )
