"""One ingestion run of the product catalog, from file bytes to a published version.

Each step is a pure function or a short transaction. Nothing waits on the network inside
a transaction, and every write can be repeated: a run that dies midway leaves rows no
version lists, which the next run reuses.
"""

import hashlib
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date
from itertools import batched
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.knowledge import repository
from wobot.knowledge.blobs import BlobStore
from wobot.knowledge.chunking import products as product_chunking
from wobot.knowledge.embeddings import MAX_BATCH_SIZE, Embedder
from wobot.knowledge.records.products import normalize_catalog
from wobot.knowledge.sources.xlsx import read_catalog
from wobot.knowledge.validation import check_products, check_version

SOURCE_ID = "product_catalog"
STRATEGIES = {product_chunking.STRATEGY: product_chunking.STRATEGY_VERSION}

Policy = Literal["publish", "dry_run"]


class Database(Protocol):
    """Where each step's transaction comes from: an AsyncEngine, or a test's savepoints."""

    def begin(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


@dataclass(frozen=True)
class CatalogFile:
    locator: str  # where the bytes came from, such as a file name
    content: bytes


@dataclass
class RunResult:
    run_id: uuid.UUID
    status: str = "running"
    index_version_id: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


async def run_ingestion(
    db: Database,
    blobs: BlobStore,
    embedder: Embedder,
    catalog: CatalogFile,
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
        result.status = await _ingest(db, blobs, embedder, catalog, policy, result)
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
                source_results={SOURCE_ID: result.source},
                counts=result.counts,
                errors=result.errors,
            )
    return result


async def _ingest(
    db: Database,
    blobs: BlobStore,
    embedder: Embedder,
    catalog: CatalogFile,
    policy: Policy,
    result: RunResult,
) -> str:
    # The baseline, read once. If another run publishes after this, publishing below
    # fails rather than overwriting it.
    async with db.begin() as conn:
        active = await repository.read_active(conn, SOURCE_ID)
        # Checked before any paid call: vectors could never be stored under a missing config.
        if not await repository.embedding_config_exists(conn, embedder.config_id):
            raise LookupError(f"no embedding config {embedder.config_id!r}")

    sha256 = hashlib.sha256(catalog.content).hexdigest()
    result.source |= {"locator": catalog.locator, "sha256": sha256}
    storage_key = await blobs.put(catalog.content)
    # Parsed even when the file is the one the active version came from: new code may read
    # the same bytes differently, and only the content comparison below would notice.
    drafts = normalize_catalog(read_catalog(catalog.content))
    chunks = [product_chunking.product_chunk(draft) for draft in drafts]
    result.counts["rows"] = len(drafts)
    report = check_products(drafts, chunks, current_year=date.today().year)
    result.source["report"] = report.to_json()
    if not report.passed:
        return "failed"

    async with db.begin() as conn:
        snapshot_id = await repository.put_snapshot(
            conn,
            source_id=SOURCE_ID,
            kind="xlsx",
            locator=catalog.locator,
            sha256=sha256,
            storage_key=storage_key,
            byte_size=len(catalog.content),
        )
    candidate = (
        {(draft.logical_key, draft.content_hash) for draft in drafts},
        {chunk.content_hash for chunk in chunks},
        embedder.config_id,
        STRATEGIES,
    )
    baseline = (
        active.record_revisions,
        active.chunk_hashes,
        active.embedding_config_id,
        active.strategies,
    )
    if candidate == baseline:
        # Same file, or new bytes with the same content: re-saved, or rows reordered.
        same_file = sha256 in active.snapshot_sha256s
        result.source["no_change"] = "same file" if same_file else "same content"
        return "no_change"

    async with db.begin() as conn:
        record_ids, records_new = await repository.put_products(conn, drafts)
        chunk_ids, chunks_new = await repository.put_chunks(conn, chunks, record_ids)
        missing = await repository.missing_embeddings(conn, chunk_ids.values(), embedder.config_id)
    result.counts |= {
        "records": len(record_ids),
        "records_new": records_new,
        "chunks": len(chunk_ids),
        "chunks_new": chunks_new,
        "embeddings_new": 0,
        "embedding_tokens": 0,
    }

    for batch in batched(missing, MAX_BATCH_SIZE, strict=False):
        embedded = await embedder.embed([text for _, text in batch])
        # Stored batch by batch: a failure later keeps these, and a rerun skips them.
        async with db.begin() as conn:
            vectors = dict(zip((chunk_id for chunk_id, _ in batch), embedded.vectors, strict=True))
            await repository.put_embeddings(conn, embedder.config_id, vectors)
        result.counts["embeddings_new"] += len(batch)
        result.counts["embedding_tokens"] += embedded.input_tokens

    members = [
        repository.Member(
            record_id=record_ids[(draft.logical_key, draft.content_hash)],
            logical_key=draft.logical_key,
            snapshot_id=snapshot_id,
            locator={"row": draft.row_number},
        )
        for draft in drafts
    ]
    async with db.begin() as conn:
        version_id = await repository.create_version(
            conn,
            run_id=result.run_id,
            config_id=embedder.config_id,
            strategies=STRATEGIES,
            members=members,
            chunk_ids=chunk_ids.values(),
        )
        integrity = await repository.version_integrity(conn, version_id, embedder.config_id)
        check_version(
            report, integrity, expected_records=len(drafts), expected_chunks=len(chunk_ids)
        )
        result.source["report"] = report.to_json()
        status = "validated" if report.passed else "failed"
        await repository.set_version_status(conn, version_id, status, report.to_json())
    result.index_version_id = version_id
    if not report.passed or policy == "dry_run":
        return status

    async with db.begin() as conn:
        if not await repository.publish(conn, version_id, active.revision):
            result.errors.append("revision_conflict: another run published since this one read")
            return "failed"
    return "published"
