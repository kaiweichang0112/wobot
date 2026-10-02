"""Chunk strategies for section records: prose by heading, tables row by row.

section_text: a section's paragraphs, closed near TARGET_TOKENS at a paragraph boundary
and never past MAX_TOKENS. Consecutive short sections under the same heading join into
one chunk, each under its own heading line, so a page of one-line features or an FAQ of
short answers becomes a few chunks, not dozens. A heading with nothing under it leads into
the section after it, and one with nothing after it is chunked by its heading line alone.

technical_table: each row repeats the header, as "Column：value｜…", so a row read alone
still says what each value is. Rows flow with the section's paragraphs, and a chunk
holding table rows alone takes this strategy.

The header is the heading path, "site › page › heading". The token figures are a start
that evaluation is meant to tune.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from wobot.knowledge.chunking.drafts import ChunkDraft, build_chunk
from wobot.knowledge.records.drafts import RecordDraft
from wobot.knowledge.tokens import count_tokens

SECTION_TEXT = "section_text"
TECHNICAL_TABLE = "technical_table"
STRATEGY_VERSIONS = {SECTION_TEXT: 1, TECHNICAL_TABLE: 1}
MIN_TOKENS = 300  # a chunk below this takes the next paragraph even past the target
TARGET_TOKENS = 500
MAX_TOKENS = 800
# A paragraph longer than this is cut, leaving room for the header in its chunk.
PIECE_MAX_TOKENS = MAX_TOKENS - 100
SEPARATOR = " › "


@dataclass(frozen=True)
class _Piece:
    """One paragraph, table row or link list of a section: chunks split between pieces."""

    shown: str
    embedded: str  # without URLs, which no question matches
    record_revision: tuple[str, str]
    links: tuple[dict[str, Any], ...] = ()
    row: bool = False  # a table row

    @property
    def tokens(self) -> int:
        return count_tokens(self.embedded)


def section_chunks(drafts: Sequence[RecordDraft], *, per_item: bool = False) -> list[ChunkDraft]:
    """`per_item` is accepted like every chunker's, and changes nothing: prose has no item
    variant."""
    sections = [draft for draft in drafts if draft.record_type == "section"]
    chunks = []
    for run in _runs(sections):
        alone = len(run) == 1 and _pieces(run[0], [])
        header_path = _path(run[0]) if alone else _group(run[0])
        pieces = []
        for draft in run:
            heading = _path(draft)[len(header_path) :]
            pieces += _pieces(draft, heading)
        chunks += _chunks(header_path, pieces)
    return chunks


def _path(draft: RecordDraft) -> list[str]:
    return draft.raw["heading_path"]


def _group(draft: RecordDraft) -> list[str]:
    """Sections that may share a chunk: a page's top sections, or one heading's subsections."""
    path = _path(draft)
    return path[:-1] if len(path) > 2 else path


def _runs(sections: Sequence[RecordDraft]) -> list[list[RecordDraft]]:
    runs: list[list[RecordDraft]] = []
    size = 0
    for draft in sections:
        tokens = sum(piece.tokens for piece in _pieces(draft, []))
        joins = (
            runs
            and _group(runs[-1][0]) == _group(draft)
            and (
                not size  # the run so far is headings with nothing under them
                or (size < MIN_TOKENS and tokens < MIN_TOKENS and size + tokens <= TARGET_TOKENS)
            )
        )
        if joins:
            runs[-1].append(draft)
            size += tokens
        else:
            runs.append([draft])
            size = tokens
    return runs


def _pieces(draft: RecordDraft, heading: Sequence[str]) -> list[_Piece]:
    """The section in page order: paragraphs with each table where it stood, then links."""
    revision = draft.revision
    paragraphs: list[str] = draft.raw["paragraphs"]
    tables: list[dict[str, Any]] = draft.raw.get("tables", [])
    pieces: list[_Piece] = []
    for index in range(len(paragraphs) + 1):
        for table in (t for t in tables if t["after"] == index):
            pieces += [_Piece(row, row, revision, row=True) for row in table_rows(table["rows"])]
        if index < len(paragraphs):
            pieces += [_Piece(part, part, revision) for part in _fit(paragraphs[index])]
    links = tuple(link for link in draft.raw.get("links", []) if link["url"])
    if links:
        shown = "\n".join(f"{_label(link)}：{link['url']}" for link in links)
        named = "、".join(dict.fromkeys(_label(link) for link in links))
        pieces.append(_Piece(shown, named, revision, links))
    if heading and pieces:
        # The heading opens the section's first piece, so it is never left alone at the end
        # of a chunk.
        line = "## " + SEPARATOR.join(heading)
        first = pieces[0]
        pieces[0] = _Piece(
            f"{line}\n{first.shown}", f"{line}\n{first.embedded}", revision, first.links
        )
    elif heading:
        line = "## " + SEPARATOR.join(heading)
        pieces.append(_Piece(line, line, revision))
    return pieces


def table_rows(rows: Sequence[Sequence[str]]) -> list[str]:
    """Each row as "Column：value｜…", empty cells left out; a lone row is shown as is."""
    if len(rows) < 2:
        return ["｜".join(cell for cell in row if cell) for row in rows if any(row)]
    header, *body = rows
    lines = []
    for row in body:
        cells = [
            f"{name}：{value}" if name else value
            for name, value in zip(header, row, strict=False)
            if value
        ]
        if cells:
            lines.append("｜".join(cells))
    return lines


def _fit(text: str) -> list[str]:
    """A paragraph too long for a chunk, split between lines, then by length."""
    if count_tokens(text) <= PIECE_MAX_TOKENS:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if current and count_tokens(candidate) > PIECE_MAX_TOKENS:
            parts.append(current)
            candidate = line
        current = candidate
    if current:
        parts.append(current)
    return [piece for part in parts for piece in _cut(part)]


def _cut(text: str) -> list[str]:
    if count_tokens(text) <= PIECE_MAX_TOKENS:
        return [text]
    middle = len(text) // 2
    return _cut(text[:middle]) + _cut(text[middle:])


def _chunks(header_path: Sequence[str], pieces: Sequence[_Piece]) -> list[ChunkDraft]:
    header = SEPARATOR.join(header_path)
    parts = _pack(count_tokens(header), pieces)
    chunks = []
    for number, part in enumerate(parts, start=1):
        strategy = TECHNICAL_TABLE if all(piece.row for piece in part) else SECTION_TEXT
        chunks.append(
            build_chunk(
                strategy=strategy,
                strategy_version=STRATEGY_VERSIONS[strategy],
                heading_path=header_path,
                context_header=header if len(parts) == 1 else f"{header} ({number}/{len(parts)})",
                body="\n\n".join(piece.shown for piece in part),
                embedding_body="\n\n".join(piece.embedded for piece in part),
                links=[link for piece in part for link in piece.links],
                record_revisions=list(dict.fromkeys(piece.record_revision for piece in part)),
            )
        )
    return chunks


def _pack(header_tokens: int, pieces: Sequence[_Piece]) -> list[list[_Piece]]:
    """Close a chunk before a piece that would pass the target, unless the chunk is still
    short and the piece fits under the cap."""
    parts: list[list[_Piece]] = []
    current: list[_Piece] = []
    size = header_tokens
    for piece in pieces:
        tokens = piece.tokens
        if current and size + tokens > TARGET_TOKENS:
            if size >= MIN_TOKENS or size + tokens > MAX_TOKENS:
                parts.append(current)
                current, size = [], header_tokens
        current.append(piece)
        size += tokens
    if current:
        parts.append(current)
    return parts


def _label(link: dict[str, Any]) -> str:
    return link["text"] or urlsplit(link["url"]).hostname or link["url"]
