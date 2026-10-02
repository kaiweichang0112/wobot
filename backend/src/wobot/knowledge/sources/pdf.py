"""PDF documents → their pages: the words each page's text layer holds, and the page drawn
as an image.

The text layer is the document's own words, read without a model. Each page is also drawn
for the visual step, which reads what the layer lacks: text set as outlines, figures and
photos.
"""

import io
from dataclasses import dataclass

import pypdfium2 as pdfium

from wobot.knowledge.sources.wix import clean_block_text

# Points to pixels: an A4 page (595 × 842 pt) is drawn at 1191 × 1684, enough for 8 pt text.
RENDER_SCALE = 2.0
# A brochure or a manual; far more pages means the wrong file.
MAX_PAGES = 60


class PdfError(Exception):
    """The bytes are not a PDF this reader can open, or not one it should."""


@dataclass(frozen=True)
class PdfPage:
    number: int  # from 1, as a #page=N link counts
    text: str  # the text layer, line by line; empty for a page drawn as a picture


def pdf_pages(content: bytes) -> list[PdfPage]:
    document = _open(content)
    try:
        if len(document) > MAX_PAGES:
            raise PdfError(f"{len(document)} pages is over the {MAX_PAGES} page limit")
        pages = []
        for index in range(len(document)):
            page = document[index]
            text_page = page.get_textpage()
            pages.append(PdfPage(index + 1, page_text(text_page.get_text_bounded())))
            text_page.close()
            page.close()
        return pages
    finally:
        document.close()


def page_text(text: str) -> str:
    """The layer's lines, each trimmed, empty ones left out."""
    lines = clean_block_text(text.replace("\r\n", "\n").replace("\r", "\n")).split("\n")
    return "\n".join(line for line in lines if line)


def render_page(content: bytes, number: int, *, scale: float = RENDER_SCALE) -> bytes:
    """Page `number` (from 1) drawn as a PNG."""
    document = _open(content)
    try:
        page = document[number - 1]
        image = page.render(scale=scale).to_pil()
        page.close()
    finally:
        document.close()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _open(content: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(content)
    except pdfium.PdfiumError as error:
        raise PdfError(f"not a readable PDF: {error}") from error
