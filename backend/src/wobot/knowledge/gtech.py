"""G-Tech's website, its WhizToys documentation and the PDFs the website links to, as
ingestion sources.

The website's pages are listed; the documentation's are found in its sitemap under one
path, so a page added there is read on the next run. Both are read by heading, with the
images each page shows read by a vision model. The PDFs are listed too, and read page by
page: the text layer as written, the drawn page by the vision model.
"""

from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from wobot.knowledge.chunking.sections import section_chunks
from wobot.knowledge.chunking.visual import document_page_chunks
from wobot.knowledge.extraction import CachedReader
from wobot.knowledge.grc import site_chrome
from wobot.knowledge.page_images import ImageReading, read_page_images
from wobot.knowledge.profiles import (
    DOCS_EXCLUDED,
    DOCS_PREFIX,
    DOCS_SITE,
    DOCS_SITEMAP,
    GTECH_CONTACT_HEADING,
    GTECH_DOCUMENTS,
    GTECH_EXCLUDED,
    GTECH_LINKED_ONLY,
    GTECH_PAGES,
    GTECH_SITE,
    GTECH_SITEMAP,
)
from wobot.knowledge.records.documents import document_page_record
from wobot.knowledge.records.drafts import RecordDraft
from wobot.knowledge.records.images import ShownImage, shown_images
from wobot.knowledge.records.sections import SectionPage, image_placements, section_records
from wobot.knowledge.source import Extraction, Snapshot
from wobot.knowledge.sources.documents import FetchDocument
from wobot.knowledge.sources.docusaurus import article_blocks
from wobot.knowledge.sources.http import PageFetcher
from wobot.knowledge.sources.pdf import PdfError, pdf_pages, render_page
from wobot.knowledge.sources.sitemaps import sitemap_urls, unlisted_pages
from wobot.knowledge.sources.wix import Block, page_blocks
from wobot.knowledge.validation import check_pages
from wobot.knowledge.vision import VisualInput

WEBSITE_SOURCE_ID = "gtech_website"
DOCS_SOURCE_ID = "gtech_docs"
DOCUMENTS_SOURCE_ID = "gtech_documents"


class GtechWebsiteSource:
    source_id = WEBSITE_SOURCE_ID

    def __init__(self, fetcher: PageFetcher, images: ImageReading) -> None:
        self._fetcher = fetcher
        self._images = images

    async def extract(self) -> Extraction:
        snapshots, blocks = [], {}
        for profile in GTECH_PAGES:
            page = await self._fetcher.fetch(profile.url)
            snapshots.append(Snapshot(profile.url, page.content, {"final_url": page.url}))
            html = page.content.decode("utf-8", errors="replace")
            blocks[profile.url] = page_blocks(html, buttons=True, images=True)
        chrome = site_chrome(list(blocks.values()))
        pages, chunks, drafts_by_page, problems_by_page = [], [], {}, {}
        shown: list[tuple[SectionPage, list[ShownImage]]] = []
        for profile, snapshot in zip(GTECH_PAGES, snapshots, strict=True):
            own = page_content(blocks[profile.url], chrome, home=profile.key == "home")
            page = SectionPage(profile.url, GTECH_SITE, "gtech", profile.title, profile.key)
            parsed = section_records(own, page, nested=False)
            pages.append((snapshot, parsed.drafts))
            chunks += section_chunks(parsed.drafts)
            drafts_by_page[profile.url] = list(parsed.drafts)
            problems_by_page[profile.url] = parsed.problems
            shown.append((page, shown_images(image_placements(own, nested=False), profile.url)))
        images = await read_page_images(shown, self._images)
        pages += images.files
        chunks += images.chunks
        for url, drafts in images.drafts_by_page.items():
            drafts_by_page[url] += drafts
        notes = await unlisted_pages(
            self._fetcher, GTECH_SITEMAP, [p.url for p in GTECH_PAGES], GTECH_EXCLUDED
        )
        notes += unlisted_documents(drafts_by_page.values())
        report = check_pages(drafts_by_page, problems_by_page, chunks, notes=notes + images.notes)
        report.counts |= {"images": len(images.files)} | images.stats.counts("vision_reads")
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


def unlisted_documents(pages: Iterable[Sequence[RecordDraft]]) -> list[str]:
    """Files the pages link to that no document profile lists, to read or to rule out."""
    known = {document_id(url) for url in (*(d.url for d in GTECH_DOCUMENTS), *GTECH_LINKED_ONLY)}
    notes = []
    for drafts in pages:
        for draft in drafts:
            for link in draft.raw.get("links", []):
                url = link["url"] or ""
                if _is_document(url) and (key := document_id(url)) not in known:
                    known.add(key)
                    notes.append(f"document not in any profile: {url}")
    return notes


def _is_document(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == "drive.google.com" or parts.path.startswith("/_files/")


def document_id(url: str) -> str:
    """The same file however it is linked: a Drive file by its ID, else without a query."""
    parts = urlsplit(url)
    if parts.hostname == "drive.google.com" and "/file/d/" in parts.path:
        return "drive:" + parts.path.split("/file/d/")[1].split("/")[0]
    return f"{parts.hostname}{parts.path}"


class GtechDocsSource:
    source_id = DOCS_SOURCE_ID

    def __init__(self, fetcher: PageFetcher, images: ImageReading) -> None:
        self._fetcher = fetcher
        self._images = images

    async def extract(self) -> Extraction:
        urls = [url for url in await sitemap_urls(self._fetcher, DOCS_SITEMAP) if is_doc(url)]
        pages, chunks, drafts_by_page, problems_by_page = [], [], {}, {}
        shown: list[tuple[SectionPage, list[ShownImage]]] = []
        listed: dict[str, str] = {}  # where each page was served → where the sitemap lists it
        for url in urls:
            page = await self._fetcher.fetch(url)
            blocks = article_blocks(page.content.decode("utf-8", errors="replace"))
            title = next((b.text for b in blocks if b.kind == "heading" and b.level == 1), url)
            key = urlsplit(url).path.removeprefix(DOCS_PREFIX).strip("/")
            # Links and images resolve against where the page was served, after redirects.
            section_page = SectionPage(page.url, DOCS_SITE, "whiztoys_docs", title, key)
            parsed = section_records(blocks, section_page, nested=True)
            pages.append((Snapshot(url, page.content, {"final_url": page.url}), parsed.drafts))
            chunks += section_chunks(parsed.drafts)
            drafts_by_page[url] = list(parsed.drafts)
            problems_by_page[url] = parsed.problems
            listed[page.url] = url
            placements = image_placements(blocks, nested=True)
            shown.append((section_page, shown_images(placements, page.url)))
        images = await read_page_images(shown, self._images)
        pages += images.files
        chunks += images.chunks
        for served, drafts in images.drafts_by_page.items():
            drafts_by_page[listed[served]] += drafts
        report = check_pages(drafts_by_page, problems_by_page, chunks, notes=images.notes)
        report.counts |= {"images": len(images.files)} | images.stats.counts("vision_reads")
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


class GtechDocumentsSource:
    """The listed PDFs, a record per page: its text layer as written, and the page drawn as
    an image as a vision model reads it."""

    source_id = DOCUMENTS_SOURCE_ID

    def __init__(self, fetch: FetchDocument, reader: CachedReader[VisualInput]) -> None:
        self._fetch = fetch
        self._reader = reader

    async def extract(self) -> Extraction:
        read, files, problems = [], [], {}
        for document in GTECH_DOCUMENTS:
            snapshot = await self._fetch(document)
            try:
                read.append((document, snapshot, pdf_pages(snapshot.content)))
            except PdfError as error:
                problems[document.url] = [str(error)]
                files.append((snapshot, []))
        inputs = {
            (document.key, page.number): VisualInput(
                snapshot.sha256, render_page(snapshot.content, page.number), page=page.number
            )
            for document, snapshot, pdf in read
            for page in pdf
        }
        answers, stats = await self._reader.read_all(list(inputs.values()))

        chunks, notes = [], []
        drafts_by_document: dict[str, list[RecordDraft]] = {url: [] for url in problems}
        for document, snapshot, pdf in read:
            drafts = []
            for page in pdf:
                answer = answers[inputs[document.key, page.number]]
                if answer.output is None:
                    notes.append(f"{document.url} page {page.number}: no reading: {answer.failure}")
                    if not page.text:
                        continue  # nothing of the page is known
                drafts.append(
                    document_page_record(
                        document,
                        page,
                        site=GTECH_SITE,
                        pages=len(pdf),
                        sha256=snapshot.sha256,
                        answer=answer if answer.output is not None else None,
                        question=self._reader.question,
                    )
                )
            files.append((snapshot, drafts))
            drafts_by_document[document.url] = drafts
            chunks += document_page_chunks(drafts)
        report = check_pages(drafts_by_document, problems, chunks, notes=notes)
        report.counts |= {"document_pages": sum(len(pdf) for _, _, pdf in read)} | stats.counts(
            "vision_reads"
        )
        return Extraction(
            source_id=DOCUMENTS_SOURCE_ID, kind="pdf", pages=files, chunks=chunks, report=report
        )
