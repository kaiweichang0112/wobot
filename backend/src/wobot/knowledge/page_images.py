"""The images a source's pages show → image records and chunks: each file fetched once and
read once, however many pages show it.

An image that cannot be fetched, is not an image, or gets no reading from the model is
reported and left out; the page's text is still published. Every file fetched is kept as a
snapshot, read or not, as the record of what the run saw.
"""

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from wobot.knowledge.chunking.drafts import ChunkDraft
from wobot.knowledge.chunking.visual import image_chunks
from wobot.knowledge.extraction import CachedReader, ReadStats
from wobot.knowledge.records.drafts import RecordDraft, separate_collisions
from wobot.knowledge.records.images import ShownImage, image_record
from wobot.knowledge.records.sections import SectionPage
from wobot.knowledge.source import Snapshot
from wobot.knowledge.sources.http import FetchedPage, FetchError
from wobot.knowledge.vision import VisualInput

# A photo straight from a camera runs to a few megabytes; far more means the wrong file.
IMAGE_MAX_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class ImageReading:
    """How a source gets its images: fetched politely, read by a model through the cache."""

    fetch: Callable[[str], Awaitable[FetchedPage]]
    reader: CachedReader[VisualInput]


@dataclass
class PageImages:
    files: list[tuple[Snapshot, list[RecordDraft]]] = field(default_factory=list)
    chunks: list[ChunkDraft] = field(default_factory=list)
    drafts_by_page: dict[str, list[RecordDraft]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    stats: ReadStats = field(default_factory=ReadStats)


async def read_page_images(
    shown: Sequence[tuple[SectionPage, Sequence[ShownImage]]], reading: ImageReading
) -> PageImages:
    result = PageImages()
    fetched: dict[str, FetchedPage] = {}
    tried: set[str] = set()
    for page, images in shown:
        for image in images:
            if image.url in tried:
                continue
            tried.add(image.url)
            try:
                file = await reading.fetch(image.url)
            except FetchError as error:
                result.notes.append(f"{page.url}: image not fetched: {error}")
                continue
            if not file.content_type.startswith("image/"):
                result.notes.append(f"{page.url}: {image.url} is {file.content_type!r}, no image")
                continue
            fetched[image.url] = file

    inputs = {
        url: VisualInput(hashlib.sha256(file.content).hexdigest(), file.content)
        for url, file in fetched.items()
    }
    answers, result.stats = await reading.reader.read_all(list(inputs.values()))

    by_file: dict[str, list[RecordDraft]] = {url: [] for url in fetched}
    for page, images in shown:
        drafts = []
        for position, image in enumerate(images):
            if image.url not in inputs:
                continue
            answer = answers[inputs[image.url]]
            if answer.output is None:
                result.notes.append(f"{page.url}: {image.url} has no reading: {answer.failure}")
                continue
            drafts.append(
                image_record(
                    page,
                    image,
                    sha256=inputs[image.url].sha256,
                    answer=answer,
                    question=reading.reader.question,
                    position=position,
                )
            )
        drafts = separate_collisions(drafts)
        result.drafts_by_page[page.url] = drafts
        for draft in drafts:
            by_file[draft.raw["image_url"]].append(draft)
    for url, drafts in by_file.items():
        file = fetched[url]
        snapshot = Snapshot(url, file.content, {"content_type": file.content_type})
        result.files.append((snapshot, drafts))
        result.chunks += image_chunks(drafts)
    return result
