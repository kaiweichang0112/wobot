"""Synthetic G-Tech sites behind a mock transport: the Wix website, its image host, the
Docusaurus docs and the PDFs the website links to.

The content is made up; the structure follows the real sites: chrome and a menu on every
page, a contact form closing each page, catalogs behind buttons, images served from
another host whose robots.txt answers 403, a page of images only, and docs found through
a sitemap that also lists tag and category pages.
"""

from urllib.parse import quote

import httpx2

from tests.knowledge.docs_pages import doc_page, heading, table
from tests.knowledge.fakes import fake_vision
from tests.knowledge.pdf_files import text_pdf
from tests.knowledge.wix_pages import h, p, rich
from wobot.knowledge.extraction import CachedReader
from wobot.knowledge.gtech import GtechDocsSource, GtechDocumentsSource, GtechWebsiteSource
from wobot.knowledge.page_images import IMAGE_MAX_BYTES, ImageReading
from wobot.knowledge.profiles import DOCS_HOST, GTECH_DOCUMENTS, GTECH_HOST, DocumentProfile
from wobot.knowledge.source import Snapshot
from wobot.knowledge.sources.http import FetchedPage, PageFetcher
from wobot.knowledge.sources.wix import IMAGE_HOST
from wobot.knowledge.vision import VisualInput

SITE = f"https://{GTECH_HOST}"
DOCS = f"https://{DOCS_HOST}"
MEDIA = f"https://{IMAGE_HOST}/media"
DRIVE_CATALOG = "https://drive.google.com/file/d/catalog/view"  # in no document profile
MANUAL = next(d.url for d in GTECH_DOCUMENTS if d.key == "whiztoys_manual")
LINE = "https://line.me/R/ti/p/@example"
ABOUT = "/" + quote("資深的新創公司")


def button(element_id: str, label: str, href: str) -> str:
    return (
        f'<div id="{element_id}"><a data-testid="linkElement" href="{href}">'
        f'<span data-testid="stylablebutton-label">{label}</span></a></div>'
    )


def picture(element_id: str, name: str, alt: str, size: tuple[int, int] | None) -> str:
    """A Wix image: a resized copy of the file, with the size shown or, as a gallery
    writes it, the size in the copy's address only."""
    width, height = size or (774, 531)
    src = f"{MEDIA}/{name}/v1/fill/w_{width},h_{height},al_c/{name}"
    shown = f' width="{width}" height="{height}"' if size else ""
    return f'<div id="{element_id}"><img src="{src}" alt="{alt}"{shown}></div>'


def wix_page(*elements: str, contact: str) -> bytes:
    """A page between the shared header and footer, its contact form last."""
    header = (
        picture("comp-logo", "logo~mv2.png", "logo.png", (130, 54))
        + rich("comp-brand", p("範例智科"))
        + button("comp-menu", "首頁", SITE)
    )
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
            picture("comp-feature", "feature~mv2.png", "資產 38_3x.png", (165, 166)),
            button("comp-line", "", LINE),
            contact="c1",
        ),
        "/whizpad": wix_page(
            rich("comp-lead", h(1, "智慧床墊")),  # a heading over a long section
            rich("comp-title", h(1, "安心臥床墊")),
            rich("comp-text", p("離床前提醒照護者。" * 60)),
            picture("comp-spec", "spec~mv2.png", "體壓分佈測定", (539, 245)),
            button("comp-catalog", "線上型錄", DRIVE_CATALOG),
            contact="c2",
        ),
        "/whiztoys": wix_page(
            rich("comp-t1", h(1, "運動地墊")),
            rich("comp-t2", h(2, "運動地墊")),
            rich("comp-t3", p("把運動變成遊戲。")),
            picture("comp-gone", "gone~mv2.png", "", (400, 300)),  # missing from the host
            button("comp-manual", "操作說明書下載", MANUAL),
            button("comp-contact-us", "Contact us >", f"{SITE}/whiztoys#toys"),
            contact="c3",
        ),
        ABOUT: wix_page(picture("item-g1", "about~mv2.png", "懶人包-02.png", None), contact="c4"),
    }


IMAGES = {
    f"/media/{name}": (name.encode(), "image/png")
    for name in ("logo~mv2.png", "feature~mv2.png", "spec~mv2.png", "about~mv2.png")
}


def website_sitemap(extra: tuple[str, ...] = ()) -> bytes:
    paths = [
        "",
        "/whizpad",
        "/whiztoys",
        ABOUT,
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
            '<p><img src="/assets/images/kit.png" alt="產品配備圖" width="4032" height="3024"></p>',
            '<p><img src="/assets/images/ic.svg" alt="測驗圖示" width="1097" height="1097"></p>',
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


Routes = dict[str, bytes | tuple[bytes, str]]  # path → body, or body and content type


def _fetcher(host: str, routes: Routes, requests: list[str]) -> PageFetcher:
    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        path = request.url.raw_path.decode()
        if request.url.host != host:
            return httpx2.Response(404)
        if path in routes:
            body, content_type = (
                routes[path] if isinstance(routes[path], tuple) else (routes[path], "text/html")
            )
            return httpx2.Response(200, content=body, headers={"content-type": content_type})
        if f"{path}/" in routes:  # Docusaurus serves each page under a trailing slash
            return httpx2.Response(301, headers={"location": f"{path}/"})
        return httpx2.Response(404)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return PageFetcher(client, {host}, min_interval=0)


def website_source(
    requests: list[str],
    *,
    extra_pages: tuple[str, ...] = (),
    vision: CachedReader[VisualInput] | None = None,
) -> GtechWebsiteSource:
    routes: Routes = {
        "/robots.txt": b"User-agent: *\nAllow: /\n",
        "/sitemap.xml": website_sitemap(extra_pages),
        **website_pages(),
    }
    # Wix's image host answers 403 for its robots.txt, which sets no rules (RFC 9309).
    images = _fetcher(IMAGE_HOST, {"/robots.txt": (b"Forbidden", "text/plain"), **IMAGES}, requests)
    return GtechWebsiteSource(
        _fetcher(GTECH_HOST, routes, requests),
        ImageReading(images.fetch, vision or fake_vision()),
    )


def docs_source(
    requests: list[str],
    *,
    sitemap: bytes | None = None,
    vision: CachedReader[VisualInput] | None = None,
) -> GtechDocsSource:
    routes: Routes = {
        "/robots.txt": b"",
        "/sitemap.xml": docs_sitemap() if sitemap is None else sitemap,
        "/assets/images/kit.png": (b"kit", "image/png"),
        **docs_pages(),
    }
    fetcher = _fetcher(DOCS_HOST, routes, requests)

    async def fetch_image(url: str) -> FetchedPage:
        return await fetcher.fetch(url, max_bytes=IMAGE_MAX_BYTES)

    return GtechDocsSource(fetcher, ImageReading(fetch_image, vision or fake_vision()))


def pdfs() -> dict[str, bytes]:
    """Each listed document as a PDF: a cover drawn only, then pages of text."""
    return {
        document.key: text_pdf("", f"{document.key} contents\nSize: 30 cm", "Safety")
        for document in GTECH_DOCUMENTS
    }


def documents_source(
    files: dict[str, bytes] | None = None, *, vision: CachedReader[VisualInput] | None = None
) -> GtechDocumentsSource:
    files = pdfs() if files is None else files

    async def fetch(document: DocumentProfile) -> Snapshot:
        return Snapshot(document.url, files[document.key])

    return GtechDocumentsSource(fetch, vision or fake_vision())
