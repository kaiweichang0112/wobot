"""Images a page shows → image records: each image's place on the page, and what a vision
model read in it.

The page decides which images there are; an image too small to hold anything, an icon
drawn as SVG, or one that is not a file at all is left out before anything is fetched. An
image shown several times on a page is one record listing every place. A model's reading
is kept as it answered, under who answered.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from wobot.knowledge.extraction import Answer, Question
from wobot.knowledge.records.drafts import RecordDraft, record_draft
from wobot.knowledge.records.sections import ImagePlacement, SectionPage
from wobot.knowledge.records.text import key_text

# Logos, social icons and footer badges are shown smaller than this, in CSS pixels.
MIN_SHOWN_SIDE = 100
# An alt text that is only a file name, as Wix fills it in, says nothing.
_FILE_NAME = re.compile(r"\.(png|jpe?g|gif|webp|svg)$", re.IGNORECASE)


@dataclass(frozen=True)
class ShownImage:
    url: str  # absolute
    alts: tuple[str, ...]  # what the page says about it, file names left out
    paths: tuple[tuple[str, ...], ...]  # the headings above each place it is shown


def shown_images(placements: Sequence[ImagePlacement], page_url: str) -> list[ShownImage]:
    """The page's images worth reading, in the order they first appear."""
    alts: dict[str, list[str]] = {}
    paths: dict[str, list[tuple[str, ...]]] = {}
    for placement in placements:
        image = placement.image
        url = urljoin(page_url, image.url)
        if not _worth_reading(url, image.width, image.height):
            continue
        alts.setdefault(url, [])
        paths.setdefault(url, [])
        if image.alt and not _FILE_NAME.search(image.alt) and image.alt not in alts[url]:
            alts[url].append(image.alt)
        if placement.path not in paths[url]:
            paths[url].append(placement.path)
    return [ShownImage(url, tuple(alts[url]), tuple(paths[url])) for url in alts]


def _worth_reading(url: str, width: int | None, height: int | None) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False  # an image written into the page, such as a data: URI
    if parts.path.lower().endswith(".svg"):
        return False  # drawn icons, which the vision API does not take
    return all(side >= MIN_SHOWN_SIDE for side in (width, height) if side is not None)


def image_record(
    page: SectionPage,
    image: ShownImage,
    *,
    sha256: str,
    answer: Answer,
    question: Question,
    position: int,
) -> RecordDraft:
    name = unquote(urlsplit(image.url).path.rstrip("/").rsplit("/", 1)[-1])
    key = ":".join(["image", page.site_key, page.page_key, key_text(name)])
    return record_draft(
        "image",
        key[:240],
        raw={
            "heading_path": [page.site, page.title, *image.paths[0]],
            "image_url": image.url,
            "alts": list(image.alts),
            "shown_under": [list(path) for path in image.paths],
            "sha256": sha256,
            "reading": answer.output,
            "read_by": read_by(question, answer),
        },
        fields={},
        locator={"url": page.url, "image_url": image.url, "position": position},
    )


def read_by(question: Question, answer: Answer) -> dict[str, Any]:
    """Who answered: the model asked, the snapshot that answered, under which prompt."""
    return {
        "model": question.model,
        "response_model": answer.response_model,
        "prompt_version": question.prompt_version,
    }
