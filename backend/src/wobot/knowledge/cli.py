"""wobot-ingest: ingest the knowledge sources, search the active version, show its status."""

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import httpx2
from google.cloud import storage
from openai import APIError, AsyncOpenAI

from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.knowledge import catalog, grc, repository
from wobot.knowledge.blobs import BlobStore, GcsBlobStore, LocalBlobStore
from wobot.knowledge.catalog import CatalogFile, FetchCatalog, ProductCatalogSource
from wobot.knowledge.embeddings import OpenAIEmbedder
from wobot.knowledge.grc import GrcWebsiteSource
from wobot.knowledge.pipeline import RunResult, run_ingestion
from wobot.knowledge.profiles import GRC_HOST
from wobot.knowledge.search import search_chunks
from wobot.knowledge.source import Source
from wobot.knowledge.sources.drive import DriveError, drive_token, fetch_drive_file
from wobot.knowledge.sources.http import FetchError, PageFetcher, new_client
from wobot.knowledge.sources.xlsx import CatalogSchemaError
from wobot.logs import configure_logging

# Statuses that need no one's attention; anything else exits non-zero.
SUCCESSFUL = {"published", "no_change", "validated"}
# Retries on 429 and 5xx, with backoff, before a run gives up.
OPENAI_MAX_RETRIES = 3
DRIVE_TIMEOUT_SECONDS = 60
# Failures of the world outside, not of the code: reported in one line, no traceback.
# A run has already recorded them.
EXPECTED_ERRORS = (APIError, DriveError, CatalogSchemaError, FetchError)
SOURCES = (catalog.SOURCE_ID, grc.SOURCE_ID)

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wobot-ingest", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="ingest the sources and publish a new version")
    run.add_argument(
        "--sources",
        type=lambda value: value.split(","),
        default=list(SOURCES),
        help=f"comma-separated, from {', '.join(SOURCES)} (default: all); the others are "
        "carried over from the active version",
    )
    run.add_argument(
        "--catalog-file",
        type=Path,
        help="a local catalog workbook (.xlsx); without it, the Drive file in "
        "PRODUCT_CATALOG_FILE_ID is downloaded",
    )
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
    if args.command == "run":
        if unknown := set(args.sources) - set(SOURCES):
            parser.error(f"unknown sources {sorted(unknown)}")
        needs_catalog = catalog.SOURCE_ID in args.sources
        if needs_catalog and not (args.catalog_file or settings.product_catalog_file_id):
            parser.error("pass --catalog-file or set PRODUCT_CATALOG_FILE_ID")
    configure_logging()
    handler: Callable[[argparse.Namespace, Settings], Awaitable[int]] = args.handler
    try:
        return asyncio.run(handler(args, settings))
    except EXPECTED_ERRORS as error:
        logger.error("%s: %s", type(error).__name__, error)
        return 1
    except Exception:
        # Logged rather than left to the interpreter, so Cloud Logging gets one entry.
        logger.exception("wobot-ingest crashed")
        return 1


def _embedder(settings: Settings) -> OpenAIEmbedder:
    assert settings.openai_api_key is not None
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=OPENAI_MAX_RETRIES,
    )
    return OpenAIEmbedder(client, settings.embedding_model)


def _blob_store(settings: Settings) -> BlobStore:
    if settings.knowledge_bucket is None:
        return LocalBlobStore(settings.knowledge_local_dir)
    client = storage.Client(project=settings.google_cloud_project)
    return GcsBlobStore(client.bucket(settings.knowledge_bucket))


def _fetch_catalog(catalog_file: Path | None, settings: Settings) -> FetchCatalog:
    async def from_file() -> CatalogFile:
        content = await asyncio.to_thread(catalog_file.read_bytes)
        # The file name only: a local path says nothing about the catalog.
        return CatalogFile(locator=catalog_file.name, content=content)

    async def from_drive() -> CatalogFile:
        file_id = settings.product_catalog_file_id
        transport = httpx2.AsyncHTTPTransport(retries=2)  # connection failures only
        async with httpx2.AsyncClient(timeout=DRIVE_TIMEOUT_SECONDS, transport=transport) as client:
            file = await fetch_drive_file(client, file_id, await drive_token())
        return CatalogFile(
            locator=f"drive:{file_id}",
            content=file.content,
            details={"name": file.name, "modified_time": file.modified_time},
        )

    return from_file if catalog_file is not None else from_drive


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    engine, connector = await create_engine(settings)
    try:
        async with new_client() as client:
            sources: list[Source] = []
            if catalog.SOURCE_ID in args.sources:
                sources.append(ProductCatalogSource(_fetch_catalog(args.catalog_file, settings)))
            if grc.SOURCE_ID in args.sources:
                sources.append(GrcWebsiteSource(PageFetcher(client, {GRC_HOST})))
            result = await run_ingestion(
                engine,
                _blob_store(settings),
                _embedder(settings),
                sources,
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
    if result.reason:
        line += f" ({result.reason})"
    print(line)
    counts = result.counts
    if "records" in counts:
        print(
            f"  {counts['records']} records ({counts['records_new']} new), "
            f"{counts['chunks']} chunks ({counts['chunks_new']} new), "
            f"{counts['embeddings_new']} embeddings ({counts['embedding_tokens']} tokens)"
        )
    for source, details in result.sources.items():
        report = details["report"]
        print(f"  {source}: {report['counts']}")
        for label in ("blocking", "warnings"):
            for message in report[label]:
                print(f"    {label}: {message}")
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
