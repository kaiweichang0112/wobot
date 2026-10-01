"""The GRC website as an ingestion source: the listed pages, read by their page profiles."""

import re
from collections.abc import Callable, Sequence
from urllib.parse import unquote, urlsplit

from wobot.knowledge.chunking import grc as grc_chunking
from wobot.knowledge.chunking.drafts import ChunkDraft
from wobot.knowledge.profiles import GRC_EXCLUDED, GRC_LATER, GRC_PAGES, GRC_SITEMAP
from wobot.knowledge.records import grc as grc_records
from wobot.knowledge.records.drafts import RecordDraft
from wobot.knowledge.source import Extraction, Snapshot
from wobot.knowledge.sources.http import FetchError, PageFetcher
from wobot.knowledge.sources.wix import Block, page_blocks
from wobot.knowledge.validation import check_pages

SOURCE_ID = "grc_website"
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")

Parser = Callable[[Sequence[Block]], grc_records.Parsed]
Chunker = Callable[[Sequence[RecordDraft]], list[ChunkDraft]]
PARSERS: dict[str, tuple[Parser, Chunker]] = {
    "profile": (
        lambda blocks: grc_records.parse_profile(
            blocks, person=grc_chunking.PERSON, intro_heading="簡介"
        ),
        grc_chunking.profile_chunks,
    ),
    "publications": (grc_records.parse_publications, grc_chunking.publication_chunks),
    "projects": (grc_records.parse_projects, grc_chunking.project_chunks),
    "students_master": (
        lambda blocks: grc_records.parse_students(blocks, "master"),
        grc_chunking.student_chunks,
    ),
    "students_phd": (
        lambda blocks: grc_records.parse_students(blocks, "phd"),
        grc_chunking.student_chunks,
    ),
}


class GrcWebsiteSource:
    source_id = SOURCE_ID

    def __init__(self, fetcher: PageFetcher) -> None:
        self._fetcher = fetcher

    async def extract(self) -> Extraction:
        snapshots, blocks = [], {}
        for profile in GRC_PAGES:
            page = await self._fetcher.fetch(profile.url)
            snapshots.append(Snapshot(profile.url, page.content, {"final_url": page.url}))
            blocks[profile.url] = page_blocks(page.content.decode("utf-8", errors="replace"))
        chrome = site_chrome(list(blocks.values()))
        pages, chunks, drafts_by_page, problems_by_page = [], [], {}, {}
        for profile, snapshot in zip(GRC_PAGES, snapshots, strict=True):
            parse, chunk = PARSERS[profile.parser]
            parsed = parse(content_blocks(blocks[profile.url], chrome))
            pages.append((snapshot, parsed.drafts))
            chunks += chunk(parsed.drafts)
            drafts_by_page[profile.url] = parsed.drafts
            problems_by_page[profile.url] = parsed.problems
        notes = await self._unlisted_pages()
        return Extraction(
            source_id=SOURCE_ID,
            kind="website",
            pages=pages,
            chunks=chunks,
            report=check_pages(drafts_by_page, problems_by_page, chunks, notes=notes),
        )

    async def _unlisted_pages(self) -> list[str]:
        """Pages the sitemap lists that no profile covers: new pages a person should see."""
        known = {_normal(url) for url in (*(p.url for p in GRC_PAGES), *GRC_LATER)}
        try:
            index = await self._fetcher.fetch(GRC_SITEMAP)
            urls = []
            for sitemap in _LOC.findall(index.content.decode()):
                urls += _LOC.findall((await self._fetcher.fetch(sitemap)).content.decode())
        except FetchError as error:
            return [f"sitemap not read: {error}"]
        return [
            f"page not in any profile: {unquote(url)}"
            for url in urls
            if _normal(url) not in known
            and not any(pattern.search(unquote(urlsplit(url).path)) for pattern in GRC_EXCLUDED)
        ]


def site_chrome(pages: Sequence[Sequence[Block]]) -> set[str]:
    """Text elements on every page: header, footer and the like, never content.

    Needs two pages or more; with one, every element would look like chrome.
    """
    if len(pages) < 2:
        return set()
    return set.intersection(*({block.element_id for block in page} for page in pages))


def content_blocks(blocks: Sequence[Block], chrome: set[str]) -> list[Block]:
    """The page's own blocks: no chrome, and no paragraph that is only a link to a page."""
    return [
        block
        for block in blocks
        if block.element_id not in chrome
        and not (
            block.kind == "paragraph"
            and len(block.links) == 1
            and block.text == block.links[0].text
            and urlsplit(block.links[0].url or "").hostname == urlsplit(GRC_SITEMAP).hostname
        )
    ]


def _normal(url: str) -> str:
    return unquote(url).rstrip("/")
