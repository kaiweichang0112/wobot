"""Chunk strategies for what a vision model read: one image, or one page of a PDF.

image_explanation: an image's reading under the headings where the page shows it. The
model's description, the text it transcribed, the values, what the layout relates and
what it could not read each sit under a label saying they are the model's; the page's own
alt text is labelled as the page's.

pdf_page: a page under its document's title. The text layer comes first, as written. The
model's reading follows with only what the layer lacks: lines and values the layer holds
are left out, so a page of plain text is not said twice.

Both stay whole up to MAX_TOKENS, a picture's reading being one thing, and split between
parts past it, as sections do.
"""

import unicodedata
from collections.abc import Sequence
from typing import Any

from wobot.knowledge.chunking.drafts import ChunkDraft
from wobot.knowledge.chunking.sections import MAX_TOKENS, Piece, fit, pack_chunks
from wobot.knowledge.records.drafts import RecordDraft

IMAGE_EXPLANATION = "image_explanation"
PDF_PAGE = "pdf_page"
STRATEGY_VERSIONS = {IMAGE_EXPLANATION: 1, PDF_PAGE: 1}


def image_chunks(drafts: Sequence[RecordDraft], *, per_item: bool = False) -> list[ChunkDraft]:
    """`per_item` is accepted like every chunker's, and changes nothing."""
    chunks = []
    for draft in (d for d in drafts if d.record_type == "image"):
        raw, revision = draft.raw, draft.revision
        pieces = []
        if raw["alts"]:
            pieces.append(_piece(f"圖片替代文字（網頁原文）：{'；'.join(raw['alts'])}", revision))
        if len(raw["shown_under"]) > 1:
            places = "；".join(
                " › ".join(path) or raw["heading_path"][1] for path in raw["shown_under"]
            )
            pieces.append(_piece(f"出現位置：{places}", revision))
        pieces += _reading(raw["reading"], revision)
        url = raw["image_url"]
        pieces.append(Piece(f"原圖：{url}", "", revision, ({"text": "原圖", "url": url},)))
        chunks += pack_chunks(
            raw["heading_path"],
            pieces,
            strategy=(IMAGE_EXPLANATION, STRATEGY_VERSIONS[IMAGE_EXPLANATION]),
            target=MAX_TOKENS,
        )
    return chunks


def document_page_chunks(
    drafts: Sequence[RecordDraft], *, per_item: bool = False
) -> list[ChunkDraft]:
    """`per_item` is accepted like every chunker's, and changes nothing."""
    chunks = []
    for draft in (d for d in drafts if d.record_type == "document_page"):
        raw, revision = draft.raw, draft.revision
        pieces = []
        if raw["text"]:
            pieces += _labelled("頁面文字（原文）：\n", raw["text"], revision)
        pieces += _reading(raw["reading"], revision, layer=raw["text"])
        title, page = raw["heading_path"][1], raw["page"]
        url = f"{raw['document_url']}#page={page}"
        link = {"text": f"{title} 第 {page} 頁", "url": url}
        pieces.append(Piece(f"PDF：{url}", "", revision, (link,)))
        chunks += pack_chunks(
            raw["heading_path"],
            pieces,
            strategy=(PDF_PAGE, STRATEGY_VERSIONS[PDF_PAGE]),
            target=MAX_TOKENS,
        )
    return chunks


def _reading(
    reading: dict[str, Any] | None, revision: tuple[str, str], *, layer: str | None = None
) -> list[Piece]:
    """The model's reading, each part labelled as the model's; with a text `layer`, only
    the lines and values it does not already hold."""
    if reading is None:
        return []
    known = _squash(layer or "")
    lines = [line for line in reading["verbatim_text"] if not (known and _squash(line) in known)]
    values = [
        value
        for value in reading["values"]
        if not (known and _squash(value["value"] + (value["unit"] or "")) in known)
    ]
    pieces = []
    if reading["description"]:
        pieces += _labelled("模型描述：", reading["description"], revision)
    if lines:
        label = "圖中其他文字（模型轉錄）：\n" if layer else "圖中文字（模型轉錄）：\n"
        pieces += _labelled(label, "\n".join(lines), revision)
    if values:
        listed = "\n".join(f"- {v['label']}：{_amount(v['value'], v['unit'])}" for v in values)
        pieces += _labelled("數值（模型讀取）：\n", listed, revision)
    if reading["relationships"]:
        listed = "\n".join(f"- {text}" for text in reading["relationships"])
        pieces += _labelled("圖示關係（模型說明）：\n", listed, revision)
    if reading["unreadable"]:
        listed = "\n".join(f"- {text}" for text in reading["unreadable"])
        pieces += _labelled("無法辨識（模型回報）：\n", listed, revision)
    return pieces


def _labelled(label: str, text: str, revision: tuple[str, str]) -> list[Piece]:
    """Text under its label, cut to fit a chunk however long the model made it."""
    return [
        _piece(f"{label}{part}" if index == 0 else part, revision)
        for index, part in enumerate(fit(text))
    ]


def _piece(text: str, revision: tuple[str, str]) -> Piece:
    return Piece(text, text, revision)


def _amount(value: str, unit: str | None) -> str:
    """ "30 cm" but "88%" and "2003年": a space only before a unit spelled in letters."""
    if not unit:
        return value
    return f"{value} {unit}" if unit[0].isascii() and unit[0].isalpha() else f"{value}{unit}"


def _squash(text: str) -> str:
    """For telling whether the layer holds a line: full-width forms folded, spaces gone."""
    return "".join(unicodedata.normalize("NFKC", text).split())
