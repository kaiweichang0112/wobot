"""Chunk strategies for GRC records: one chunk per list block, an item never split.

Blocks follow how people ask: graduates of a degree in a year, projects of a year,
publications of a category in a year, one profile section. Every chunk shows its links;
what is embedded leaves the URLs out, as they mean nothing a question could match.
"""

import re
from collections.abc import Callable, Hashable, Sequence
from itertools import groupby

from wobot.knowledge.chunking.drafts import BlockItem, ChunkDraft, block_chunks
from wobot.knowledge.records.drafts import RecordDraft
from wobot.knowledge.records.lectures import LECTURE_CATEGORIES

CENTER = "元智大學老人福祉科技研究中心"
PERSON = "徐業良"
STUDENT_BLOCK = "student_block"
PROJECT_BLOCK = "project_block"
PUBLICATION_BLOCK = "publication_block"
PROFILE_SECTION = "profile_section"
LECTURE_BLOCK = "lecture_block"
STRATEGY_VERSIONS = {
    STUDENT_BLOCK: 1,
    PROJECT_BLOCK: 1,
    PUBLICATION_BLOCK: 1,
    PROFILE_SECTION: 1,
    LECTURE_BLOCK: 1,
}
DEGREE_LABELS = {"master": "碩士", "phd": "博士"}
# The page's own heading for each category.
LECTURE_LABELS = {code: heading for heading, code in LECTURE_CATEGORIES.items()}
_URL = re.compile(r"\s*https?://\S+")


def _runs(
    drafts: Sequence[RecordDraft], key: Callable[[RecordDraft], Hashable]
) -> list[tuple[Hashable, list[RecordDraft]]]:
    """Consecutive drafts with the same key, in page order."""
    return [(value, list(run)) for value, run in groupby(drafts, key=key)]


def _chunks(
    strategy: str, heading_path: list[str], header: str, items: list[BlockItem]
) -> list[ChunkDraft]:
    return block_chunks(
        strategy=strategy,
        strategy_version=STRATEGY_VERSIONS[strategy],
        heading_path=heading_path,
        context_header=header,
        items=items,
    )


def student_chunks(drafts: Sequence[RecordDraft]) -> list[ChunkDraft]:
    chunks = []
    by_block = _runs(drafts, lambda d: (d.fields["degree"], d.fields["graduation_year"]))
    for (degree, year), run in by_block:
        label = f"{DEGREE_LABELS[degree]}畢業生"
        items = []
        for draft in run:
            fields = draft.fields
            lines = [fields["name"]]
            if fields["thesis_title_zh"]:
                lines.append(f"論文名稱：{fields['thesis_title_zh']}")
            if fields["thesis_title_en"]:
                lines.append(f"Thesis title: {fields['thesis_title_en']}")
            url = fields["fulltext_url"]
            shown = [*lines, f"電子全文：{url}"] if url else lines
            links = ({"kind": "thesis", "text": "電子全文", "url": url},) if url else ()
            items.append(BlockItem("\n".join(shown), "\n".join(lines), draft.revision, links))
        chunks += _chunks(
            STUDENT_BLOCK, [CENTER, label, str(year)], f"{CENTER}｜{label}｜{year}年", items
        )
    return chunks


def project_chunks(drafts: Sequence[RecordDraft]) -> list[ChunkDraft]:
    chunks = []
    for year, run in _runs(drafts, lambda d: d.fields["year"]):
        items = []
        for draft in run:
            fields = draft.fields
            start, end = fields["period_start"], fields["period_end"]
            period = (
                f"{start:%Y/%m/%d}～{end:%Y/%m/%d}"
                if start and end
                else f"{fields['period_raw']}（原文如此）"
            )
            lines = [
                *(title for title in (fields["title_zh"], fields["title_en"]) if title),
                f"委託單位：{fields['funder_raw']}",
                f"執行期間：{period}",
                f"金額：新台幣 {fields['amount_ntd']:,} 元",
            ]
            text = "\n".join(lines)
            items.append(BlockItem(text, text, draft.revision))
        chunks += _chunks(
            PROJECT_BLOCK, [CENTER, "研究計畫", str(year)], f"{CENTER}｜研究計畫｜{year}年", items
        )
    return chunks


def publication_chunks(drafts: Sequence[RecordDraft]) -> list[ChunkDraft]:
    chunks = []
    by_block = _runs(drafts, lambda d: (d.fields["category"], d.fields["year"]))
    for (category, year), run in by_block:
        year_label = f"{year}年" if year else "年份不詳"
        items = []
        for draft in run:
            fields = draft.fields
            links = tuple(link for link in fields["links"] if link["url"])
            shown = [fields["item_text"], *(f"{_label(link)}：{link['url']}" for link in links)]
            embedded = _URL.sub("", fields["item_text"]).strip()
            items.append(BlockItem("\n".join(shown), embedded, draft.revision, links))
        chunks += _chunks(
            PUBLICATION_BLOCK,
            [PERSON, "Publications", category, str(year or "")],
            f"{PERSON}教授著作｜{category}｜{year_label}",
            items,
        )
    return chunks


def _label(link: dict[str, str]) -> str:
    # A DOI anchor's text is often the URL itself; say what it is instead.
    return "DOI" if link.get("kind") == "doi" else link["text"]


def profile_chunks(drafts: Sequence[RecordDraft]) -> list[ChunkDraft]:
    """A section's prose, or a list of profile items, per chunk; long prose splits by paragraph."""
    chunks = []
    for draft in (d for d in drafts if d.record_type == "section"):
        heading = draft.raw["heading_path"][-1]
        items = [BlockItem(text, text, draft.revision) for text in draft.raw["paragraphs"]]
        chunks += _chunks(PROFILE_SECTION, [PERSON, heading], f"{PERSON}｜{heading}", items)
    items_only = [d for d in drafts if d.record_type == "list_item"]
    for section, run in _runs(items_only, lambda d: d.fields["category"]):
        items = [BlockItem(d.fields["item_text"], d.fields["item_text"], d.revision) for d in run]
        chunks += _chunks(PROFILE_SECTION, [PERSON, section], f"{PERSON}｜{section}", items)
    return chunks


def lecture_chunks(drafts: Sequence[RecordDraft]) -> list[ChunkDraft]:
    """A category's year block per chunk, each talk as the page writes it.

    Built from the entry text alone, never from what a model read: a model's answers can
    change with its prompt, and the text a search matches should not.
    """
    chunks = []
    by_block = _runs(drafts, lambda d: (d.fields["category"], d.fields["year_block"]))
    for (category, year_block), run in by_block:
        label = LECTURE_LABELS[category]
        items = []
        for draft in run:
            fields = draft.fields
            links = tuple(link for link in fields["links"] if link["url"])
            shown = [fields["entry_text"], *(f"{link['text']}：{link['url']}" for link in links)]
            items.append(BlockItem("\n".join(shown), fields["entry_text"], draft.revision, links))
        chunks += _chunks(
            LECTURE_BLOCK,
            [PERSON, "Speeches", label, year_block],
            f"{PERSON}教授演講｜{label}｜{year_block}年",
            items,
        )
    return chunks
