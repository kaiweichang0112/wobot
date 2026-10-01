"""What a source hands the pipeline: snapshots with their records, chunks, and a report."""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from wobot.knowledge.chunking.drafts import ChunkDraft
from wobot.knowledge.records.drafts import RecordDraft
from wobot.knowledge.validation import ValidationReport


@dataclass(frozen=True)
class Snapshot:
    locator: str  # a file name, drive:<file ID>, or a page URL
    content: bytes
    details: Mapping[str, Any] = field(default_factory=dict)  # kept on the snapshot row

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class Extraction:
    source_id: str
    kind: str  # knowledge.sources.kind: xlsx, website or pdf
    # Each snapshot with the records read from it, so every record knows its snapshot.
    pages: Sequence[tuple[Snapshot, Sequence[RecordDraft]]]
    chunks: Sequence[ChunkDraft]
    report: ValidationReport

    @property
    def records(self) -> list[RecordDraft]:
        return [draft for _, drafts in self.pages for draft in drafts]


class Source(Protocol):
    source_id: str

    async def extract(self) -> Extraction:
        """Fetch, read, chunk and check everything this source holds; write nothing."""
        ...
