"""Synthetic Wix markup: the patterns the GRC parsers depend on, with made-up content.

Real pages are not copied; each helper reproduces one habit of Wix's rich text: text split
over many <span>s, zero-width and no-break spaces, <br> line breaks, repeater item IDs, and
legacy links with a `dataquery` instead of an href.
"""

ZWSP = "​"
NBSP = "\xa0"
HOST = "https://www.grc.yzu.edu.tw"
# Text elements every page shares: what the source removes as chrome.
CHROME = '<div id="comp-footer" data-testid="richTextElement"><p>地址：範例路 1 號</p></div>'


def rich(element_id: str, *nodes: str) -> str:
    return f'<div id="{element_id}" data-testid="richTextElement">{"".join(nodes)}</div>'


def spans(*parts: str) -> str:
    """Text split the way Wix splits it, one <span> per part."""
    return "".join(f'<span class="wixui-rich-text__text">{part}</span>' for part in parts)


def p(*parts: str) -> str:
    return f"<p>{spans(*parts)}</p>"


def br_p(*lines: str) -> str:
    """One paragraph holding several lines, separated by <br>."""
    return "<p>" + "<br>".join(spans(line) for line in lines) + "</p>"


def a(text: str, href: str | None = None, query: str = "#textLink_x") -> str:
    target = f'href="{href}"' if href else f'dataquery="{query}"'
    return f"<a {target}>{spans(text)}</a>"


def link_p(text: str, href: str | None = None) -> str:
    return f"<p>{a(text, href)}</p>"


def h(level: int, text: str) -> str:
    return f"<h{level}>{spans(text)}</h{level}>"


def ol(*items: str) -> str:
    return "<ol>" + "".join(f"<li><p>{item}</p></li>" for item in items) + "</ol>"


BLANK = p(ZWSP)


def page(*elements: str) -> bytes:
    return f"<html><body>{CHROME}{''.join(elements)}</body></html>".encode()
