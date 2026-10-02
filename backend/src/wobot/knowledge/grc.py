"""The GRC website as an ingestion source: the listed pages, read by their page profiles."""

from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from wobot.knowledge.chunking import grc as grc_chunking
from wobot.knowledge.chunking.drafts import ChunkDraft
from wobot.knowledge.chunking.sections import section_chunks
from wobot.knowledge.extraction import CachedReader, ReadStats
from wobot.knowledge.profiles import GRC_EXCLUDED, GRC_PAGES, GRC_SITEMAP
from wobot.knowledge.records import grc as grc_records
from wobot.knowledge.records.lectures import lecture_records, split_lectures
from wobot.knowledge.records.sections import SectionPage, section_records
from wobot.knowledge.source import Extraction, Snapshot
from wobot.knowledge.sources.http import PageFetcher
from wobot.knowledge.sources.sitemaps import unlisted_pages
from wobot.knowledge.sources.wix import Block, page_blocks
from wobot.knowledge.validation import check_pages

SOURCE_ID = "grc_website"

Parser = Callable[[Sequence[Block]], grc_records.Parsed]
Chunker = Callable[..., list[ChunkDraft]]  # (drafts, *, per_item) → chunks
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

    def __init__(
        self, fetcher: PageFetcher, lectures: CachedReader, *, per_item: bool = False
    ) -> None:
        self._fetcher = fetcher
        # Reads the parts of each speech: answers kept from earlier runs, or a model.
        self._lectures = lectures
        # One chunk per list item instead of per block, for comparing the two.
        self._per_item = per_item

    async def extract(self) -> Extraction:
        snapshots, blocks = [], {}
        for profile in GRC_PAGES:
            page = await self._fetcher.fetch(profile.url)
            snapshots.append(Snapshot(profile.url, page.content, {"final_url": page.url}))
            blocks[profile.url] = page_blocks(page.content.decode("utf-8", errors="replace"))
        chrome = site_chrome(list(blocks.values()))
        pages, chunks, drafts_by_page, problems_by_page, notes = [], [], {}, {}, []
        model_reads = ReadStats()
        for profile, snapshot in zip(GRC_PAGES, snapshots, strict=True):
            # The home page keeps what every page shows, such as the address, so the
            # site's own facts are read once.
            own_blocks = content_blocks(
                blocks[profile.url], set() if profile.parser == "home" else chrome
            )
            if profile.parser == "home":
                page = SectionPage(profile.url, grc_chunking.CENTER, "grc", "首頁", "home")
                parsed = section_records(own_blocks, page, nested=False)
                chunk = section_chunks
            elif profile.parser == "speeches":
                parsed, model_reads = await self._read_speeches(own_blocks)
                chunk: Chunker = grc_chunking.lecture_chunks
            else:
                parse, chunk = PARSERS[profile.parser]
                parsed = parse(own_blocks)
            pages.append((snapshot, parsed.drafts))
            chunks += chunk(parsed.drafts, per_item=self._per_item)
            drafts_by_page[profile.url] = parsed.drafts
            problems_by_page[profile.url] = parsed.problems
            notes += parsed.notes
        notes += await unlisted_pages(
            self._fetcher, GRC_SITEMAP, (p.url for p in GRC_PAGES), GRC_EXCLUDED
        )
        report = check_pages(drafts_by_page, problems_by_page, chunks, notes=notes)
        report.counts |= model_reads.counts("model_reads")
        return Extraction(
            source_id=SOURCE_ID, kind="website", pages=pages, chunks=chunks, report=report
        )

    async def _read_speeches(self, blocks: Sequence[Block]) -> tuple[grc_records.Parsed, ReadStats]:
        """Code splits the talks; a model reads each one's parts, unless already read."""
        lectures = split_lectures(blocks)
        if lectures.problems:
            # The page changed shape and the run cannot publish: paying a model would buy
            # nothing.
            return grc_records.Parsed(problems=lectures.problems), ReadStats()
        answers, stats = await self._lectures.read_all([entry.text for entry in lectures.entries])
        parsed = lecture_records(
            lectures, answers, speaker=grc_chunking.PERSON, question=self._lectures.question
        )
        return parsed, stats


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
