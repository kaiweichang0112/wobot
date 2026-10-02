"""Synthetic G-Tech sites behind a mock transport: the Wix website and the Docusaurus docs.

The content is made up; the structure follows the real sites: chrome and a menu on every
page, a contact form closing each page, catalogs behind buttons, and docs found through
a sitemap that also lists tag and category pages.
"""

from urllib.parse import quote

import httpx2

from tests.knowledge.docs_pages import doc_page, heading, table
from tests.knowledge.wix_pages import h, p, rich
from wobot.knowledge.gtech import GtechDocsSource, GtechWebsiteSource
from wobot.knowledge.profiles import DOCS_HOST, GTECH_HOST
from wobot.knowledge.sources.http import PageFetcher

SITE = f"https://{GTECH_HOST}"
DOCS = f"https://{DOCS_HOST}"
DRIVE_CATALOG = "https://drive.google.com/file/d/catalog/view"
MANUAL = f"{SITE}/_files/ugd/manual.pdf"
LINE = "https://line.me/R/ti/p/@example"


def button(element_id: str, label: str, href: str) -> str:
    return (
        f'<div id="{element_id}"><a data-testid="linkElement" href="{href}">'
        f'<span data-testid="stylablebutton-label">{label}</span></a></div>'
    )


def wix_page(*elements: str, contact: str) -> bytes:
    """A page between the shared header and footer, its contact form last."""
    header = rich("comp-brand", p("範例智科")) + button("comp-menu", "首頁", SITE)
    form = rich(f"comp-{contact}-h", h(2, "聯絡我們")) + rich(
        f"comp-{contact}", p("歡迎加入 Line 官方帳號")
    )
    footer = rich("comp-address", p("範例市範例路 1 號"))
    return f"<html><body>{header}{''.join(elements)}{form}{footer}</body></html>".encode()


def website_pages() -> dict[str, bytes]:
    return {
        "/": wix_page(
            rich("comp-h", h(2, "智慧床墊")),
            rich("comp-p", p("不需插電，安全方便。")),
            button("comp-line", "", LINE),
            contact="c1",
        ),
        "/whizpad": wix_page(
            rich("comp-lead", h(1, "智慧床墊")),  # a heading over a long section
            rich("comp-title", h(1, "安心臥床墊")),
            rich("comp-text", p("離床前提醒照護者。" * 60)),
            button("comp-catalog", "線上型錄", DRIVE_CATALOG),
            contact="c2",
        ),
        "/whiztoys": wix_page(
            rich("comp-t1", h(1, "運動地墊")),
            rich("comp-t2", h(2, "運動地墊")),
            rich("comp-t3", p("把運動變成遊戲。")),
            button("comp-manual", "操作說明書下載", MANUAL),
            button("comp-contact-us", "Contact us >", f"{SITE}/whiztoys#toys"),
            contact="c3",
        ),
    }


def website_sitemap(extra: tuple[str, ...] = ()) -> bytes:
    paths = [
        "",
        "/whizpad",
        "/whiztoys",
        "/" + quote("資深的新創公司"),
        "/" + quote("資訊安全政策"),
        "/" + quote("whiztoys運動地墊遊戲平台app隱私權聲明"),
        *extra,
    ]
    return (
        "<urlset>" + "".join(f"<url><loc>{SITE}{x}</loc></url>" for x in paths) + "</urlset>"
    ).encode()


def docs_pages() -> dict[str, bytes]:
    return {
        "/docs/whiztoys/intro/": doc_page(
            "WhizToys 簡介",
            "<p>一套地墊遊戲。</p>",
            heading(2, "相關文件"),
            '<p>見 <a href="../hardware/">硬體介紹</a>。</p>',
        ),
        "/docs/whiztoys/hardware/": doc_page(
            "硬體介紹",
            heading(2, "規格"),
            table(("項目", "數值"), ("地墊尺寸", "30 cm"), ("防水", "IP44")),
        ),
    }


def docs_sitemap() -> bytes:
    paths = [
        "/",
        "/docs/tags/app",
        "/docs/category/硬體介紹",
        "/en/docs/whiztoys/intro",
        "/docs/whiztoys/intro",
        "/docs/whiztoys/hardware/",
    ]
    return (
        "<urlset>" + "".join(f"<url><loc>{DOCS}{x}</loc></url>" for x in paths) + "</urlset>"
    ).encode()


def _fetcher(host: str, routes: dict[str, bytes], requests: list[str]) -> PageFetcher:
    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        path = request.url.raw_path.decode()
        if request.url.host != host:
            return httpx2.Response(404)
        if path in routes:
            return httpx2.Response(200, content=routes[path])
        if f"{path}/" in routes:  # Docusaurus serves each page under a trailing slash
            return httpx2.Response(301, headers={"location": f"{path}/"})
        return httpx2.Response(404)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return PageFetcher(client, {host}, min_interval=0)


def website_source(requests: list[str], *, extra_pages: tuple[str, ...] = ()) -> GtechWebsiteSource:
    routes = {
        "/robots.txt": b"User-agent: *\nAllow: /\n",
        "/sitemap.xml": website_sitemap(extra_pages),
        **website_pages(),
    }
    return GtechWebsiteSource(_fetcher(GTECH_HOST, routes, requests))


def docs_source(requests: list[str], *, sitemap: bytes | None = None) -> GtechDocsSource:
    routes = {
        "/robots.txt": b"",
        "/sitemap.xml": docs_sitemap() if sitemap is None else sitemap,
        **docs_pages(),
    }
    return GtechDocsSource(_fetcher(DOCS_HOST, routes, requests))
