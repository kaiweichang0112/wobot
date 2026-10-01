from collections import Counter

import httpx2

from tests.knowledge.grc_site import grc_fetcher, site_pages, sitemaps
from wobot.knowledge.grc import GrcWebsiteSource
from wobot.knowledge.profiles import GRC_HOST, GRC_PAGES
from wobot.knowledge.sources.http import PageFetcher


async def test_reads_every_listed_page_into_records_and_chunks():
    requests = []

    extraction = await GrcWebsiteSource(grc_fetcher(requests)).extract()

    assert extraction.report.passed, extraction.report.blocking
    assert [snapshot.locator for snapshot, _ in extraction.pages] == [p.url for p in GRC_PAGES]
    assert Counter(d.record_type for d in extraction.records) == {
        "list_item": len(extraction.records) - 5,
        "section": 2,
        "project": 1,
        "student": 2,
    }
    # The footer and the navigation link are chrome, not content.
    assert not any("範例路" in str(d.raw) for d in extraction.records)
    assert not any(d.raw.get("paragraphs") == ["Publications"] for d in extraction.records)


async def test_links_are_stored_but_never_requested():
    requests = []

    extraction = await GrcWebsiteSource(grc_fetcher(requests)).extract()

    stored = {link["url"] for d in extraction.records for link in d.fields.get("links", [])}
    assert {
        "https://doi.org/10.4017/gt.2026.25.1.1257.03",
        "https://drive.google.com/file/d/example/view",
    } <= stored
    assert {httpx2.URL(url).host for url in requests} == {GRC_HOST}


async def test_reports_a_sitemap_page_no_profile_covers():
    routes = {"/robots.txt": b"", **site_pages(), **sitemaps(extra=("/new-page",))}

    def handler(request):
        return httpx2.Response(200, content=routes[request.url.raw_path.decode()])

    fetcher = PageFetcher(
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), {GRC_HOST}, min_interval=0
    )
    report = (await GrcWebsiteSource(fetcher).extract()).report

    assert report.passed
    assert [w for w in report.warnings if w.startswith("page not in")] == [
        "page not in any profile: https://www.grc.yzu.edu.tw/new-page"
    ]


async def test_a_page_whose_amount_is_unreadable_blocks():
    report = (await GrcWebsiteSource(grc_fetcher([], project_amount="NTD")).extract()).report

    assert not report.passed
    assert any("no NTD amount" in problem for problem in report.blocking)
