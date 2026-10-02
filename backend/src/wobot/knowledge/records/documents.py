"""PDF pages → document_page records: each page's text layer as written, and what a vision
model read in the page drawn as an image.

The document decides how many records there are, one per page. The text layer is the
document's own words; the model's reading is kept beside it, under who answered, and is
missing when the model gave none.
"""

from wobot.knowledge.extraction import Answer, Question
from wobot.knowledge.profiles import DocumentProfile
from wobot.knowledge.records.drafts import RecordDraft, record_draft
from wobot.knowledge.records.images import read_by
from wobot.knowledge.sources.pdf import PdfPage


def page_heading(number: int) -> str:
    return f"第 {number} 頁"


def document_page_record(
    document: DocumentProfile,
    page: PdfPage,
    *,
    site: str,
    pages: int,
    sha256: str,
    answer: Answer | None,
    question: Question,
) -> RecordDraft:
    reading = answer.output if answer is not None else None
    return record_draft(
        "document_page",
        f"document_page:{document.key}:{page.number}",
        raw={
            "heading_path": [site, document.title, page_heading(page.number)],
            "document_url": document.url,
            "page": page.number,
            "pages": pages,
            "text": page.text,
            "sha256": sha256,
            "reading": reading,
            "read_by": read_by(question, answer) if answer is not None else None,
        },
        fields={},
        locator={"url": document.url, "page": page.number},
    )
