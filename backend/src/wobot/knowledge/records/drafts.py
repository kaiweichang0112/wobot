"""What every record extractor produces: one item, its identity, and where it was found."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from wobot.knowledge.hashing import content_hash


@dataclass(frozen=True)
class RecordDraft:
    record_type: str
    logical_key: str
    content_hash: str
    # What the reader saw, for audit.
    raw: dict[str, Any]
    # The columns of the record type's own table; empty for a type without one.
    fields: dict[str, Any]
    # Where the item sits in its snapshot, such as {"row": 12}. It says where the item is,
    # not what it is, so it stays out of the hash and is stored on the version's membership:
    # a row moving down the sheet is not a new revision.
    locator: dict[str, Any]
    warnings: tuple[str, ...] = ()

    @property
    def revision(self) -> tuple[str, str]:
        return self.logical_key, self.content_hash


def record_draft(
    record_type: str,
    logical_key: str,
    *,
    raw: dict[str, Any],
    fields: dict[str, Any],
    locator: dict[str, Any],
    warnings: Sequence[str] = (),
) -> RecordDraft:
    return RecordDraft(
        record_type=record_type,
        logical_key=logical_key,
        # Normalized fields are hashed too: fixing a normalization bug must yield a new
        # revision even though the source text is unchanged.
        content_hash=content_hash({"record_type": record_type, "raw": raw, "fields": fields}),
        raw=raw,
        fields=fields,
        locator=locator,
        warnings=tuple(warnings),
    )


def separate_collisions(drafts: Sequence[RecordDraft]) -> list[RecordDraft]:
    """Give a repeated logical key a #n suffix, so no item is silently merged into another."""
    separated = []
    seen: Counter[str] = Counter()
    for draft in drafts:
        seen[draft.logical_key] += 1
        if (count := seen[draft.logical_key]) > 1:
            draft = replace(
                draft,
                logical_key=f"{draft.logical_key}#{count}",
                warnings=(*draft.warnings, f"{draft.logical_key}: logical key collision #{count}"),
            )
        separated.append(draft)
    return separated
