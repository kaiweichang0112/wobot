import io

import pytest
from PIL import Image

from tests.knowledge.pdf_files import text_pdf
from wobot.knowledge.sources.pdf import (
    MAX_PAGES,
    PdfError,
    PdfPage,
    page_text,
    pdf_pages,
    render_page,
)


def test_reads_each_page_text_layer_in_order():
    pages = pdf_pages(text_pdf("WhizToys Manual\nOne Toy Many Games", "", "Safety"))

    assert pages == [
        PdfPage(1, "WhizToys Manual\nOne Toy Many Games"),
        PdfPage(2, ""),  # drawn only: the visual step reads it
        PdfPage(3, "Safety"),
    ]


def test_layer_text_loses_blank_lines_and_stray_spaces_but_keeps_its_lines():
    assert page_text("  Spec\r\n\r\nSize:\xa0 30 cm \r\n") == "Spec\nSize: 30 cm"


def test_draws_a_page_as_a_png_at_twice_its_size_in_points():
    png = render_page(text_pdf("Cover", "Back"), 2)

    image = Image.open(io.BytesIO(png))
    assert (image.format, image.size) == ("PNG", (1190, 1684))


def test_refuses_what_is_not_a_pdf_or_is_far_too_long():
    with pytest.raises(PdfError, match="not a readable PDF"):
        pdf_pages(b"<html>404</html>")
    with pytest.raises(PdfError, match="page limit"):
        pdf_pages(text_pdf(*[""] * (MAX_PAGES + 1)))
