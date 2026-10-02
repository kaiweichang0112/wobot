from urllib.parse import unquote

import httpx2

from tests.knowledge.fakes import FakeVisionReader, fake_vision
from tests.knowledge.gtech_site import (
    ABOUT,
    DOCS,
    DRIVE_CATALOG,
    LINE,
    MANUAL,
    MEDIA,
    SITE,
    docs_source,
    documents_source,
    pdfs,
    website_source,
)
from tests.knowledge.pdf_files import text_pdf
from wobot.knowledge.extraction import Answer
from wobot.knowledge.profiles import DOCS_HOST, GTECH_DOCUMENTS, GTECH_HOST, GTECH_PAGES
from wobot.knowledge.sources.wix import IMAGE_HOST


def sections_by_page(extraction):
    return {
        snapshot.locator: [(d.raw["heading_path"][2:], d.raw["paragraphs"]) for d in drafts]
        for snapshot, drafts in extraction.pages
        if all(d.record_type == "section" for d in drafts) and snapshot.locator.startswith(SITE)
    }


def images_of(extraction):
    return {
        draft.raw["image_url"]: (draft.raw["heading_path"][1:], draft.raw["alts"])
        for draft in extraction.records
        if draft.record_type == "image"
    }


def paths(requests, host):
    return {unquote(httpx2.URL(url).path) for url in requests if httpx2.URL(url).host == host}


async def test_reads_the_listed_pages_by_heading():
    extraction = await website_source([]).extract()

    assert extraction.report.passed, extraction.report.blocking
    assert sections_by_page(extraction) == {
        # The home page keeps the header, the contact form and the footer.
        SITE: [
            ([], ["範例智科"]),
            (["智慧床墊"], ["不需插電，安全方便。"]),
            (["聯絡我們"], ["歡迎加入 Line 官方帳號", "範例市範例路 1 號"]),
        ],
        f"{SITE}/whizpad": [(["智慧床墊"], []), (["安心臥床墊"], ["離床前提醒照護者。" * 60])],
        f"{SITE}/whiztoys": [(["運動地墊"], ["把運動變成遊戲。"])],
        f"{SITE}{ABOUT}": [],  # images only
    }


async def test_reads_each_image_a_page_shows_under_its_heading_but_not_logos():
    extraction = await website_source([]).extract()

    assert images_of(extraction) == {
        f"{MEDIA}/feature~mv2.png": (["首頁", "智慧床墊"], []),  # a file name is no alt text
        f"{MEDIA}/spec~mv2.png": (["WhizPad 安心臥智慧床墊", "安心臥床墊"], ["體壓分佈測定"]),
        f"{MEDIA}/about~mv2.png": (["資深的新創公司"], []),  # a gallery: size in its address
    }
    spec = next(
        d for d in extraction.records if d.raw.get("image_url", "").endswith("spec~mv2.png")
    )
    assert spec.raw["reading"]["verbatim_text"] == ["spec~mv2.png"]  # the fake reads the bytes
    assert spec.raw["read_by"]["model"] == "fake-vision"


async def test_a_page_of_images_only_counts_as_read():
    report = (await website_source([]).extract()).report

    assert report.passed
    assert report.counts["images"] == 3
    assert f"{SITE}{ABOUT}: no records read" not in report.blocking


async def test_an_image_the_host_cannot_serve_is_reported_and_left_out():
    extraction = await website_source([]).extract()

    assert any("gone~mv2.png answered 404" in w for w in extraction.report.warnings)
    assert all("gone" not in url for url in images_of(extraction))


async def test_an_image_is_read_once_however_often_it_is_asked_for():
    reader = FakeVisionReader()
    vision = fake_vision(reader)

    await website_source([], vision=vision).extract()
    await website_source([], vision=vision).extract()

    assert len(reader.calls) == 3


async def test_an_image_without_a_reading_is_reported_not_guessed():
    refusal = Answer(None, "refused: no", "fake-vision-2026-01-01", 800, 5)
    vision = fake_vision(FakeVisionReader(lambda item: refusal))

    extraction = await website_source([], vision=vision).extract()

    assert images_of(extraction) == {}
    assert sum("has no reading: refused" in w for w in extraction.report.warnings) == 3


async def test_keeps_catalog_links_and_fetches_only_listed_pages_and_their_images():
    requests = []

    extraction = await website_source(requests).extract()

    links = {link["url"] for d in extraction.records for link in d.raw.get("links", [])}
    assert {DRIVE_CATALOG, MANUAL, LINE} <= links
    assert f"{SITE}/whiztoys#toys" not in links  # a button back to the page itself
    assert paths(requests, GTECH_HOST) == {
        "/",
        "/whizpad",
        "/whiztoys",
        unquote(ABOUT),
        "/robots.txt",
        "/sitemap.xml",
    }
    # The originals, not the resized copies; the logo is too small to be asked for.
    assert paths(requests, IMAGE_HOST) == {
        "/robots.txt",
        "/media/feature~mv2.png",
        "/media/spec~mv2.png",
        "/media/about~mv2.png",
        "/media/gone~mv2.png",
    }


async def test_reports_a_new_page_and_a_document_no_profile_lists():
    report = (await website_source([], extra_pages=("/news",)).extract()).report

    assert [w for w in report.warnings if w.startswith(("page not in", "document not in"))] == [
        f"page not in any profile: {SITE}/news",
        f"document not in any profile: {DRIVE_CATALOG}",  # the manual is listed
    ]


async def test_chunks_each_page_under_site_and_page():
    extraction = await website_source([]).extract()

    assert {chunk.heading_path[:2] for chunk in extraction.chunks} == {
        ("世大智科 G-Tech", page.title) for page in GTECH_PAGES
    }
    chunked = {revision for chunk in extraction.chunks for revision in chunk.record_revisions}
    assert chunked == {draft.revision for draft in extraction.records}


async def test_finds_the_docs_in_their_sitemap_and_nothing_else():
    requests = []

    extraction = await docs_source(requests).extract()

    assert extraction.report.passed, extraction.report.blocking
    assert [snapshot.locator for snapshot, _ in extraction.pages] == [
        f"{DOCS}/docs/whiztoys/intro",
        f"{DOCS}/docs/whiztoys/hardware/",
        f"{DOCS}/assets/images/kit.png",
    ]
    fetched = paths(requests, DOCS_HOST)
    assert not fetched & {"/docs/tags/app", "/docs/category/硬體介紹", "/en/docs/whiztoys/intro"}
    assert "/assets/images/ic.svg" not in fetched  # a drawn icon, which no model reads
    assert {httpx2.URL(url).host for url in requests} == {DOCS_HOST}


async def test_docs_sections_take_the_page_title_and_resolve_links_where_served():
    extraction = await docs_source([]).extract()

    intro = extraction.pages[0][1]
    assert [d.raw["heading_path"] for d in intro] == [
        ["WhizToys 技術文件", "WhizToys 簡介"],
        ["WhizToys 技術文件", "WhizToys 簡介", "相關文件"],
    ]
    assert intro[1].raw["links"] == [{"text": "硬體介紹", "url": f"{DOCS}/docs/whiztoys/hardware/"}]
    assert intro[0].logical_key == "section:whiztoys_docs:intro"
    table_chunk = next(c for c in extraction.chunks if "地墊尺寸" in c.body)
    assert "項目：地墊尺寸｜數值：30 cm" in table_chunk.body
    [kit] = images_of(extraction).values()
    assert kit == (["硬體介紹", "規格"], ["產品配備圖"])


async def test_a_docs_sitemap_without_whiztoys_pages_blocks():
    sitemap = f"<urlset><url><loc>{DOCS}/docs/tags/app</loc></url></urlset>".encode()

    report = (await docs_source([], sitemap=sitemap).extract()).report

    assert not report.passed
    assert report.blocking == [f"{DOCS}/sitemap.xml: no page under /docs/whiztoys/"]


# --- Documents --------------------------------------------------------------------------


async def test_reads_every_listed_document_a_record_per_page():
    extraction = await documents_source().extract()

    assert extraction.report.passed, extraction.report.blocking
    assert extraction.kind == "pdf"
    assert [snapshot.locator for snapshot, _ in extraction.pages] == [
        d.url for d in GTECH_DOCUMENTS
    ]
    manual = next(drafts for snapshot, drafts in extraction.pages if snapshot.locator == MANUAL)
    assert [(d.logical_key, d.raw["text"]) for d in manual] == [
        ("document_page:whiztoys_manual:1", ""),  # a cover drawn only, read by the model
        ("document_page:whiztoys_manual:2", "whiztoys_manual contents\nSize: 30 cm"),
        ("document_page:whiztoys_manual:3", "Safety"),
    ]
    assert manual[0].raw["reading"]["verbatim_text"] == ["page 1"]
    assert extraction.report.counts["document_pages"] == 9


async def test_a_page_chunk_cites_its_page_and_says_the_layer_once():
    reading = {
        "contains_information": True,
        "verbatim_text": ["Size: 30 cm", "Only in the picture"],
        "values": [{"label": "Size", "value": "30", "unit": "cm"}],
        "relationships": [],
        "description": "規格頁。",
        "unreadable": [],
    }
    vision = fake_vision(FakeVisionReader(lambda item: reading))

    extraction = await documents_source(vision=vision).extract()

    chunk = next(c for c in extraction.chunks if c.context_header.endswith("操作說明書 › 第 2 頁"))
    assert chunk.body.startswith("頁面文字（原文）：\nwhiztoys_manual contents\nSize: 30 cm")
    assert "圖中其他文字（模型轉錄）：\nOnly in the picture" in chunk.body
    assert "數值" not in chunk.body  # 30 cm is in the layer already
    assert chunk.links == ({"text": "WhizToys 操作說明書 第 2 頁", "url": f"{MANUAL}#page=2"},)
    assert MANUAL not in chunk.embedding_input


async def test_a_file_that_is_no_pdf_blocks_and_a_drawn_page_without_reading_is_dropped():
    files = pdfs() | {"whiztoys_manual": b"<html>Not found</html>"}
    refusal = Answer(None, "refused: no", "fake-vision-2026-01-01", 800, 5)
    vision = fake_vision(FakeVisionReader(lambda item: refusal))

    report = (await documents_source(files, vision=vision).extract()).report

    assert any(problem.startswith(f"{MANUAL}: not a readable PDF") for problem in report.blocking)
    # The covers had no text and got no reading: nothing of them is kept.
    assert report.counts["records"] == 4


async def test_pages_are_drawn_and_read_once_per_file():
    reader = FakeVisionReader()
    vision = fake_vision(reader)
    same = {document.key: text_pdf("One page") for document in GTECH_DOCUMENTS}

    await documents_source(same, vision=vision).extract()

    assert len(reader.calls) == 1  # three listings of the same bytes
