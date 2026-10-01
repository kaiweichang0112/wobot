"""One ingestion run: every listed source, from fetched bytes to a published version.

Each step is a pure function or a short transaction. Nothing waits on the network inside
a transaction, and every write can be repeated: a run that dies midway leaves rows no
version lists, which the next run reuses. A version holds every source; a source this
run did not read is carried over from the active version unchanged.
"""

import logging
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from itertools import batched
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.knowledge import repository
from wobot.knowledge.blobs import BlobStore
from wobot.knowledge.embeddings import MAX_BATCH_SIZE, Embedder
from wobot.knowledge.source import Extraction, Source
from wobot.knowledge.validation import ValidationReport, check_version

Policy = Literal["publish", "dry_run"]

logger = logging.getLogger(__name__)


class Database(Protocol):
    """Where each step's transaction comes from: an AsyncEngine, or a test's savepoints."""

    def begin(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


@dataclass
class RunResult:
    run_id: uuid.UUID
    status: str = "running"
    index_version_id: int | None = None
    # "same file" or "same content" when the run ends as no_change.
    reason: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    # Per source: its snapshots and its validation report.
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


async def run_ingestion(
    db: Database,
    blobs: BlobStore,
    embedder: Embedder,
    sources: Sequence[Source],
    *,
    policy: Policy = "publish",
    triggered_by: str = "manual",
    code_version: str = "dev",
) -> RunResult:
    # Committed on its own, so even a run that crashes leaves a trace.
    async with db.begin() as conn:
        run_id = await repository.start_run(
            conn, triggered_by=triggered_by, policy=policy, code_version=code_version
        )
    result = RunResult(run_id)
    try:
        result.status = await _ingest(db, blobs, embedder, sources, policy, result)
    except Exception as error:
        result.status = "failed"
        result.errors.append(f"{type(error).__name__}: {error}")
        raise
    finally:
        async with db.begin() as conn:
            await repository.finish_run(
                conn,
                run_id,
                status=result.status,
                source_results=result.sources,
                counts=result.counts,
                errors=result.errors,
            )
        _log(
            result,
            "finished",
            f"run {result.status}",
            status=result.status,
            index_version_id=result.index_version_id,
            counts=result.counts,
            errors=result.errors,
        )
    return result


def _log(result: RunResult, stage: str, message: str, **fields: Any) -> None:
    """One line per stage; on Cloud Run the fields become searchable log fields."""
    logger.info(message, extra={"run_id": str(result.run_id), "stage": stage, **fields})


async def _ingest(
    db: Database,
    blobs: BlobStore,
    embedder: Embedder,
    sources: Sequence[Source],
    policy: Policy,
    result: RunResult,
) -> str:
    # The baseline, read once. If another run publishes after this, publishing below
    # fails rather than overwriting it.
    async with db.begin() as conn:
        active = await repository.read_active(conn)
        # Checked before any paid call: vectors could never be stored under a missing config.
        if not await repository.embedding_config_exists(conn, embedder.config_id):
            raise LookupError(f"no embedding config {embedder.config_id!r}")

    # Every source is read in full before anything is written: one that cannot be read
    # fails the run, rather than publishing a version that silently lacks it.
    extractions: list[Extraction] = []
    for source in sources:
        extraction = await source.extract()
        report = extraction.report
        result.sources[extraction.source_id] = {
            "snapshots": [
                {"locator": snapshot.locator, "sha256": snapshot.sha256}
                for snapshot, _ in extraction.pages
            ],
            "report": report.to_json(),
        }
        _log(
            result,
            "extracted",
            f"{extraction.source_id}: {len(extraction.records)} records, "
            f"{len(report.blocking)} blocking, {len(report.warnings)} warnings",
            source=extraction.source_id,
            blocking=report.blocking,
            warnings=report.warnings,
        )
        extractions.append(extraction)
    result.counts["rows"] = sum(len(e.records) for e in extractions)
    if any(not e.report.passed for e in extractions):
        return "failed"

    # Raw bytes are kept for every snapshot, even when nothing changed: they are the audit
    # trail of what each run saw. Equal bytes share one blob and one snapshot row.
    storage_keys = {}
    for extraction in extractions:
        for snapshot, _ in extraction.pages:
            storage_keys[snapshot.sha256] = await blobs.put(snapshot.content)
    snapshot_ids = {}
    async with db.begin() as conn:
        for extraction in extractions:
            for snapshot, _ in extraction.pages:
                snapshot_ids[
                    (extraction.source_id, snapshot.locator)
                ] = await repository.put_snapshot(
                    conn,
                    source_id=extraction.source_id,
                    kind=extraction.kind,
                    locator=snapshot.locator,
                    sha256=snapshot.sha256,
                    storage_key=storage_keys[snapshot.sha256],
                    byte_size=len(snapshot.content),
                    details=snapshot.details,
                )

    if embedder.config_id == active.embedding_config_id and all(
        {d.revision for d in e.records} == active.record_revisions(e.source_id)
        and {c.content_hash for c in e.chunks} == active.chunk_hashes(e.source_id)
        for e in extractions
    ):
        # Same files, or new bytes with the same content: re-saved, reordered, restyled.
        same_files = all(
            snapshot.sha256 in active.snapshot_sha256s(e.source_id)
            for e in extractions
            for snapshot, _ in e.pages
        )
        result.reason = "same file" if same_files else "same content"
        return "no_change"

    read = {e.source_id for e in extractions}
    carried_records = [r for r in active.records if r.source_id not in read]
    carried_chunks = [c for c in active.chunks if c.source_id not in read]
    drafts = [draft for e in extractions for draft in e.records]
    chunks = [chunk for e in extractions for chunk in e.chunks]

    async with db.begin() as conn:
        record_ids, records_new = await repository.put_records(conn, drafts)
        chunk_ids, chunks_new = await repository.put_chunks(conn, chunks, record_ids)
        # Carried chunks need vectors too when the embedding space changed.
        member_chunks = list(
            dict.fromkeys([*chunk_ids.values(), *(c.chunk_id for c in carried_chunks)])
        )
        missing = await repository.missing_embeddings(conn, member_chunks, embedder.config_id)
    result.counts |= {
        "records": len(drafts) + len(carried_records),
        "records_new": records_new,
        "chunks": len(member_chunks),
        "chunks_new": chunks_new,
        "embeddings_new": 0,
        "embedding_tokens": 0,
    }
    _log(
        result,
        "stored",
        f"{records_new} new records, {chunks_new} new chunks, {len(missing)} to embed",
    )

    for batch in batched(missing, MAX_BATCH_SIZE, strict=False):
        embedded = await embedder.embed([text for _, text in batch])
        # Stored batch by batch: a failure later keeps these, and a rerun skips them.
        async with db.begin() as conn:
            vectors = dict(zip((chunk_id for chunk_id, _ in batch), embedded.vectors, strict=True))
            await repository.put_embeddings(conn, embedder.config_id, vectors)
        result.counts["embeddings_new"] += len(batch)
        result.counts["embedding_tokens"] += embedded.input_tokens
        _log(result, "embedded", f"{len(batch)} chunks, {embedded.input_tokens} tokens")

    members = [
        repository.Member(
            record_id=record_ids[draft.revision],
            logical_key=draft.logical_key,
            snapshot_id=snapshot_ids[(e.source_id, snapshot.locator)],
            locator=draft.locator,
        )
        for e in extractions
        for snapshot, page_drafts in e.pages
        for draft in page_drafts
    ] + [record.member for record in carried_records]
    # Which strategy built the version's chunks, from the chunks themselves.
    strategies = {c.strategy: c.strategy_version for c in carried_chunks} | {
        c.strategy: c.strategy_version for c in chunks
    }
    report = ValidationReport()
    async with db.begin() as conn:
        version_id = await repository.create_version(
            conn,
            run_id=result.run_id,
            config_id=embedder.config_id,
            strategies=strategies,
            members=members,
            chunk_ids=member_chunks,
        )
        integrity = await repository.version_integrity(conn, version_id, embedder.config_id)
        check_version(
            report, integrity, expected_records=len(members), expected_chunks=len(member_chunks)
        )
        version_report = report.to_json() | {
            "sources": {source: details["report"] for source, details in result.sources.items()}
        }
        status = "validated" if report.passed else "failed"
        await repository.set_version_status(conn, version_id, status, version_report)
    result.index_version_id = version_id
    _log(result, "versioned", f"version {version_id} {status}", blocking=report.blocking)
    if not report.passed or policy == "dry_run":
        return status

    async with db.begin() as conn:
        if not await repository.publish(conn, version_id, active.revision):
            result.errors.append("revision_conflict: another run published since this one read")
            return "failed"
    return "published"
