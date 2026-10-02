"""G-Tech's website and its WhizToys documentation as ingestion sources, read by heading.

The website's pages are listed; the documentation's are found in its sitemap under one
path, so a page added there is read on the next run. Images and PDFs are left to the
visual step, which finds the catalogs among the links kept here.
"""

from collections.abc import Sequence
from urllib.parse import urlsplit

from wobot.knowledge.chunking.sections import section_chunks
from wobot.knowledge.grc import site_chrome
from wobot.knowledge.profiles import (
    DOCS_EXCLUDED,
    DOCS_PREFIX,
    DOCS_SITE,
    DOCS_SITEMAP,
    GTECH_CONTACT_HEADING,
    GTECH_EXCLUDED,
    GTECH_LATER,
    GTECH_PAGES,
    GTECH_SITE,
    GTECH_SITEMAP,
)
from wobot.knowledge.records.sections import SectionPage, section_records
from wobot.knowledge.source import Extraction, Snapshot
from wobot.knowledge.sources.docusaurus import article_blocks
from wobot.knowledge.sources.http import PageFetcher
from wobot.knowledge.sources.sitemaps import sitemap_urls, unlisted_pages
from wobot.knowledge.sources.wix import Block, page_blocks
from wobot.knowledge.validation import check_pages

WEBSITE_SOURCE_ID = "gtech_website"
DOCS_SOURCE_ID = "gtech_docs"


class GtechWebsiteSource:
    source_id = WEBSITE_SOURCE_ID

    def __init__(self, fetcher: PageFetcher) -> None:
        self._fetcher = fetcher

    async def extract(self) -> Extraction:
        snapshots, blocks = [], {}
        for profile in GTECH_PAGES:
            page = await self._fetcher.fetch(profile.url)
            snapshots.append(Snapshot(profile.url, page.content, {"final_url": page.url}))
            html = page.content.decode("utf-8", errors="replace")
            blocks[profile.url] = page_blocks(html, buttons=True)
        chrome = site_chrome(list(blocks.values()))
        pages, chunks, drafts_by_page, problems_by_page = [], [], {}, {}
        for profile, snapshot in zip(GTECH_PAGES, snapshots, strict=True):
            own = page_content(blocks[profile.url], chrome, home=profile.key == "home")
            page = SectionPage(profile.url, GTECH_SITE, "gtech", profile.title, profile.key)
            parsed = section_records(own, page, nested=False)
            pages.append((snapshot, parsed.drafts))
            chunks += section_chunks(parsed.drafts)
            drafts_by_page[profile.url] = parsed.drafts
            problems_by_page[profile.url] = parsed.problems
        notes = await unlisted_pages(
            self._fetcher,
            GTECH_SITEMAP,
            (*(p.url for p in GTECH_PAGES), *GTECH_LATER),
            GTECH_EXCLUDED,
        )
        report = check_pages(drafts_by_page, problems_by_page, chunks, notes=notes)
        return Extraction(
            source_id=WEBSITE_SOURCE_ID, kind="website", pages=pages, chunks=chunks, report=report
        )


def page_content(blocks: Sequence[Block], chrome: set[str], *, home: bool) -> list[Block]:
    """The home page whole, header and footer included, so the company's address and
    links are read once; any other page without them and without the contact form."""
    if home:
        return list(blocks)
    own = [block for block in blocks if block.element_id not in chrome]
    for position, block in enumerate(own):
        if block.kind == "heading" and block.text == GTECH_CONTACT_HEADING:
            return own[:position]
    return own


class GtechDocsSource:
    source_id = DOCS_SOURCE_ID

    def __init__(self, fetcher: PageFetcher) -> None:
        self._fetcher = fetcher

    async def extract(self) -> Extraction:
        urls = [url for url in await sitemap_urls(self._fetcher, DOCS_SITEMAP) if is_doc(url)]
        pages, chunks, drafts_by_page, problems_by_page = [], [], {}, {}
        for url in urls:
            page = await self._fetcher.fetch(url)
            blocks = article_blocks(page.content.decode("utf-8", errors="replace"))
            title = next((b.text for b in blocks if b.kind == "heading" and b.level == 1), url)
            key = urlsplit(url).path.removeprefix(DOCS_PREFIX).strip("/")
            # Links on the page resolve against where it was served, after redirects.
            parsed = section_records(
                blocks, SectionPage(page.url, DOCS_SITE, "whiztoys_docs", title, key), nested=True
            )
            pages.append((Snapshot(url, page.content, {"final_url": page.url}), parsed.drafts))
            chunks += section_chunks(parsed.drafts)
            drafts_by_page[url] = parsed.drafts
            problems_by_page[url] = parsed.problems
        report = check_pages(drafts_by_page, problems_by_page, chunks)
        if not urls:
            report.blocking.append(f"{DOCS_SITEMAP}: no page under {DOCS_PREFIX}")
        return Extraction(
            source_id=DOCS_SOURCE_ID, kind="website", pages=pages, chunks=chunks, report=report
        )


def is_doc(url: str) -> bool:
    path = urlsplit(url).path
    return path.startswith(DOCS_PREFIX) and not any(
        pattern.search(path) for pattern in DOCS_EXCLUDED
    )
