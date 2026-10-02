from urllib.parse import unquote

import httpx2

from tests.knowledge.gtech_site import (
    DOCS,
    DRIVE_CATALOG,
    LINE,
    MANUAL,
    SITE,
    docs_source,
    website_source,
)
from wobot.knowledge.profiles import DOCS_HOST, GTECH_HOST, GTECH_PAGES


def by_page(extraction):
    return {
        snapshot.locator: [(d.raw["heading_path"][2:], d.raw["paragraphs"]) for d in drafts]
        for snapshot, drafts in extraction.pages
    }


async def test_reads_the_listed_pages_by_heading():
    extraction = await website_source([]).extract()

    assert extraction.report.passed, extraction.report.blocking
    assert by_page(extraction) == {
        # The home page keeps the header, the contact form and the footer.
        SITE: [
            ([], ["範例智科"]),
            (["智慧床墊"], ["不需插電，安全方便。"]),
            (["聯絡我們"], ["歡迎加入 Line 官方帳號", "範例市範例路 1 號"]),
        ],
        f"{SITE}/whizpad": [(["智慧床墊"], []), (["安心臥床墊"], ["離床前提醒照護者。" * 60])],
        f"{SITE}/whiztoys": [(["運動地墊"], ["把運動變成遊戲。"])],
    }


async def test_keeps_catalog_links_for_the_visual_step_and_fetches_only_listed_pages():
    requests = []

    extraction = await website_source(requests).extract()

    links = {link["url"] for d in extraction.records for link in d.raw["links"]}
    assert {DRIVE_CATALOG, MANUAL, LINE} <= links
    assert f"{SITE}/whiztoys#toys" not in links  # a button back to the page itself
    fetched = {unquote(httpx2.URL(url).path) for url in requests}
    assert fetched == {"/", "/whizpad", "/whiztoys", "/robots.txt", "/sitemap.xml"}
    assert {httpx2.URL(url).host for url in requests} == {GTECH_HOST}


async def test_reports_a_new_page_but_not_the_excluded_or_postponed_ones():
    report = (await website_source([], extra_pages=("/news",)).extract()).report

    assert [w for w in report.warnings if w.startswith("page not in")] == [
        f"page not in any profile: {SITE}/news"
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
    ]
    fetched = {unquote(httpx2.URL(url).path) for url in requests}
    assert not fetched & {"/docs/tags/app", "/docs/category/硬體介紹", "/en/docs/whiztoys/intro"}
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


async def test_a_docs_sitemap_without_whiztoys_pages_blocks():
    sitemap = f"<urlset><url><loc>{DOCS}/docs/tags/app</loc></url></urlset>".encode()

    report = (await docs_source([], sitemap=sitemap).extract()).report

    assert not report.passed
    assert report.blocking == [f"{DOCS}/sitemap.xml: no page under /docs/whiztoys/"]
