"""Rules a candidate version must pass before anyone can read it.

Blocking: the version would state something false or could not be built.
Warning: imperfect but true, and worth a person's look.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from wobot.knowledge.chunking.drafts import ChunkDraft
from wobot.knowledge.records.drafts import RecordDraft

# text-embedding-3-small rejects longer inputs.
MAX_EMBEDDING_TOKENS = 8191
# ROC year 100. The parser takes two- or three-digit ROC years; a dropped digit
# (112 → 12, i.e. 1923) lands before this.
FIRST_ADOPTION_YEAR = 2011


@dataclass
class ValidationReport:
    counts: dict[str, int] = field(default_factory=dict)
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.blocking

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "counts": self.counts,
            "blocking": self.blocking,
            "warnings": self.warnings,
        }


def check_products(
    drafts: Sequence[RecordDraft], chunks: Sequence[ChunkDraft], *, current_year: int
) -> ValidationReport:
    """Checks on the drafts alone, before anything is written."""
    report = ValidationReport(counts={"rows": len(drafts)})
    if not drafts:
        report.blocking.append("the catalog has no products")
    labels: dict[str, set[str]] = defaultdict(set)
    for draft in drafts:
        row = f"row {draft.locator['row']}"
        fields = draft.fields
        for name in ("product_name", "company_name"):
            if not fields[name]:
                report.blocking.append(f"{row}: {name} is empty")
        years = fields["adoption_years"]
        if implausible := [y for y in years if not FIRST_ADOPTION_YEAR <= y <= current_year + 1]:
            report.blocking.append(f"{row}: implausible adoption years {implausible}")
        l1, l2 = fields["category_l1_code"], fields["category_l2_code"]
        if l1 and l2 and not l2.startswith(f"{l1}-"):
            report.warnings.append(f"{row}: category {l2} is not under {l1}")
        if l1:
            labels[l1].add(fields["category_l1_label"])
        report.warnings += [f"{row}: {warning}" for warning in draft.warnings]
    for code, names in sorted(labels.items()):
        if len(names) > 1:
            report.warnings.append(f"category {code} has several labels: {sorted(names)}")
    for chunk in chunks:
        if chunk.token_count > MAX_EMBEDDING_TOKENS:
            report.blocking.append(
                f"chunk {chunk.context_header!r} has {chunk.token_count} tokens, "
                f"over {MAX_EMBEDDING_TOKENS}"
            )
    return report


def check_version(
    report: ValidationReport,
    integrity: Mapping[str, int],
    *,
    expected_records: int,
    expected_chunks: int,
) -> None:
    """Add the checks on the stored version to the report."""
    report.counts |= integrity
    if integrity["records"] != expected_records:
        report.blocking.append(
            f"the version holds {integrity['records']} records for {expected_records} rows"
        )
    if integrity["chunks"] != expected_chunks:
        report.blocking.append(
            f"the version holds {integrity['chunks']} chunks, expected {expected_chunks}"
        )
    if integrity["records_without_chunk"]:
        report.blocking.append(f"{integrity['records_without_chunk']} records have no chunk")
    if integrity["chunks_without_embedding"]:
        report.blocking.append(f"{integrity['chunks_without_embedding']} chunks have no embedding")


def check_pages(
    drafts_by_page: Mapping[str, Sequence[RecordDraft]],
    problems_by_page: Mapping[str, Sequence[str]],
    chunks: Sequence[ChunkDraft],
    *,
    notes: Sequence[str] = (),
) -> ValidationReport:
    """Checks on a website's drafts, before anything is written.

    A page that yields nothing, or whose parser met something it cannot read, blocks:
    the page changed shape, and publishing would quietly drop its items. `notes` are
    warnings from outside the pages, such as unknown pages in the sitemap.
    """
    report = ValidationReport(
        counts={
            "pages": len(drafts_by_page),
            "records": sum(len(drafts) for drafts in drafts_by_page.values()),
            "chunks": len(chunks),
        }
    )
    for url, drafts in drafts_by_page.items():
        if not drafts:
            report.blocking.append(f"{url}: no records read")
        report.blocking += [f"{url}: {problem}" for problem in problems_by_page.get(url, ())]
        report.warnings += [warning for draft in drafts for warning in draft.warnings]
    report.warnings += list(notes)
    for chunk in chunks:
        if chunk.token_count > MAX_EMBEDDING_TOKENS:
            report.blocking.append(
                f"chunk {chunk.context_header!r} has {chunk.token_count} tokens, "
                f"over {MAX_EMBEDDING_TOKENS}"
            )
    return report
