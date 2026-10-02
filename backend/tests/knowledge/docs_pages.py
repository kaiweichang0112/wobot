"""Synthetic Docusaurus markup: the article body the reader depends on, with made-up text.

Each helper reproduces one habit of the generated HTML: a heading's hash-link anchor
holding a zero-width space, code split into token lines, tables with a header row,
folded details, and navigation and a footer outside the article.
"""

ZWSP = "​"
DOCS = "https://docs.example.test"


def heading(level: int, text: str) -> str:
    anchor = f'<a class="hash-link" href="#x" title="link">{ZWSP}</a>'
    return f'<h{level} class="anchor" id="x">{text}{anchor}</h{level}>'


def code(*lines: str) -> str:
    spans = "".join(
        f'<span class="token-line"><span class="token plain">{line}</span><br></span>'
        for line in lines
    )
    return (
        '<div class="language-text codeBlockContainer"><div class="codeBlockContent">'
        f'<pre class="prism-code"><code>{spans}</code></pre>'
        '<button aria-label="複製">複製</button></div></div>'
    )


def table(*rows: tuple[str, ...]) -> str:
    header, *body = rows
    head = "".join(f"<th>{cell}</th>" for cell in header)
    cells = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in body)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{cells}</tbody></table>"


def doc_page(title: str, *body: str) -> bytes:
    return (
        "<html><body><nav>選單 頁面一 頁面二</nav><article>"
        '<div class="theme-doc-markdown markdown">'
        f"<header>{heading(1, title)}</header>{''.join(body)}"
        "</div></article><footer>Copyright 範例公司</footer></body></html>"
    ).encode()
