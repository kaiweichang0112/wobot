"""wobot-ingest: ingest the product catalog, search the active version, show its status."""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from openai import APIError, AsyncOpenAI

from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.knowledge import repository
from wobot.knowledge.blobs import LocalBlobStore
from wobot.knowledge.embeddings import OpenAIEmbedder
from wobot.knowledge.pipeline import CatalogFile, RunResult, run_ingestion
from wobot.knowledge.search import search_chunks

# Statuses that need no one's attention; anything else exits non-zero.
SUCCESSFUL = {"published", "no_change", "validated"}
# Retries on 429 and 5xx, with backoff, before a run gives up.
OPENAI_MAX_RETRIES = 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wobot-ingest", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="ingest the catalog and publish a new version")
    run.add_argument("--catalog-file", type=Path, required=True, help="catalog workbook (.xlsx)")
    run.add_argument(
        "--policy",
        choices=["publish", "dry-run"],
        default="publish",
        help="dry-run builds and validates a version without publishing it",
    )
    run.set_defaults(handler=_run)

    search = commands.add_parser("search", help="semantic search over the active version")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5, help="number of chunks to show")
    search.set_defaults(handler=_search)

    status = commands.add_parser("status", help="show the active version and recent runs")
    status.set_defaults(handler=_status)

    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command in ("run", "search") and settings.openai_api_key is None:
        parser.error("OPENAI_API_KEY is not set")
    handler: Callable[[argparse.Namespace, Settings], Awaitable[int]] = args.handler
    try:
        return asyncio.run(handler(args, settings))
    except APIError as error:
        # A refusal from OpenAI (no credit, a revoked key, an outage) is no bug, so no
        # traceback. A run has already recorded it as failed.
        print(f"error: OpenAI: {error}", file=sys.stderr)
        return 1


def _embedder(settings: Settings) -> OpenAIEmbedder:
    assert settings.openai_api_key is not None
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=OPENAI_MAX_RETRIES,
    )
    return OpenAIEmbedder(client, settings.embedding_model)


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    content = await asyncio.to_thread(args.catalog_file.read_bytes)
    engine, connector = await create_engine(settings)
    try:
        result = await run_ingestion(
            engine,
            LocalBlobStore(settings.knowledge_local_dir),
            _embedder(settings),
            # The file name only: a local path says nothing about the catalog.
            CatalogFile(locator=args.catalog_file.name, content=content),
            policy="dry_run" if args.policy == "dry-run" else "publish",
            code_version=settings.app_version,
        )
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    _print_run(result)
    return 0 if result.status in SUCCESSFUL else 1


def _print_run(result: RunResult) -> None:
    line = result.status
    if result.index_version_id is not None:
        line += f": version {result.index_version_id}"
    if reason := result.source.get("no_change"):
        line += f" ({reason})"
    print(line)
    counts = result.counts
    if "records" in counts:
        print(
            f"  {counts['records']} records ({counts['records_new']} new), "
            f"{counts['chunks']} chunks ({counts['chunks_new']} new), "
            f"{counts['embeddings_new']} embeddings ({counts['embedding_tokens']} tokens)"
        )
    report = result.source.get("report", {})
    for label in ("blocking", "warnings"):
        for message in report.get(label, []):
            print(f"  {label}: {message}")
    for error in result.errors:
        print(f"  error: {error}")


async def _search(args: argparse.Namespace, settings: Settings) -> int:
    embedder = _embedder(settings)
    query = await embedder.embed([args.query])
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            hits = await search_chunks(conn, query.vectors[0], embedder.config_id, args.k)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    if not hits:
        print("no active version, or it holds no chunks")
    for hit in hits:
        print(f"{hit.distance:.3f}  {hit.context_header}")
        print(f"       {hit.body.splitlines()[0]}")
        for link in hit.links:
            print(f"       {link['url']}")
    return 0


async def _status(args: argparse.Namespace, settings: Settings) -> int:
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            pointer = await repository.read_pointer(conn)
            runs = await repository.recent_runs(conn, limit=5)
            integrity = None
            if pointer.index_version_id is not None:
                integrity = await repository.version_integrity(
                    conn, pointer.index_version_id, pointer.embedding_config_id
                )
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    if integrity is None:
        print("no version published yet")
    else:
        print(
            f"active version {pointer.index_version_id} (revision {pointer.revision}), "
            f"published {pointer.published_at:%Y-%m-%d %H:%M %Z}"
        )
        print(
            f"  {integrity['records']} records, {integrity['chunks']} chunks, "
            f"{integrity['records_without_chunk']} records without a chunk, "
            f"{integrity['chunks_without_embedding']} chunks without an embedding"
        )
    print("recent runs:")
    for run in runs:
        print(f"  {run.started_at:%Y-%m-%d %H:%M}  {run.status:<10} {run.policy:<8} {run.counts}")
        for error in run.errors:
            print(f"    error: {error}")
    return 0
