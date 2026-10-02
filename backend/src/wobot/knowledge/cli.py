"""wobot-ingest: ingest the knowledge sources, search the active version, show its status,
and move the published version by hand."""

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx2
from google.cloud import storage
from openai import APIError, AsyncOpenAI

from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.knowledge import catalog, grc, gtech, repository
from wobot.knowledge.blobs import BlobStore, GcsBlobStore, LocalBlobStore
from wobot.knowledge.catalog import CatalogFile, FetchCatalog, ProductCatalogSource
from wobot.knowledge.embeddings import OpenAIEmbedder
from wobot.knowledge.extraction import CachedReader, DbAnswerCache, OpenAILectureReader
from wobot.knowledge.grc import GrcWebsiteSource
from wobot.knowledge.gtech import GtechDocsSource, GtechDocumentsSource, GtechWebsiteSource
from wobot.knowledge.locks import exclusive_run
from wobot.knowledge.maintenance import MaintenanceError, accept, rollback
from wobot.knowledge.page_images import IMAGE_MAX_BYTES, ImageReading
from wobot.knowledge.pipeline import RunResult, run_ingestion, skipped_run
from wobot.knowledge.profiles import DOCS_HOST, GRC_HOST, GTECH_DOCUMENTS, GTECH_HOST
from wobot.knowledge.schedule import scheduled_run_due
from wobot.knowledge.search import search_chunks
from wobot.knowledge.source import Source
from wobot.knowledge.sources.documents import document_fetcher, document_files
from wobot.knowledge.sources.drive import DriveError, drive_token, fetch_drive_file
from wobot.knowledge.sources.http import FetchedPage, FetchError, PageFetcher, new_client
from wobot.knowledge.sources.wix import IMAGE_HOST
from wobot.knowledge.sources.xlsx import CatalogSchemaError
from wobot.knowledge.vision import OpenAIVisionReader, VisualInput, visual_input
from wobot.logs import configure_logging

# Statuses a retry cannot change; anything else exits non-zero, and the job retries once.
# A held version needs a person, not a retry: `accept` publishes it.
SUCCESSFUL = {"published", "no_change", "validated", "held", "skipped_concurrent"}
# Retries on 429 and 5xx, with backoff, before a run gives up.
OPENAI_MAX_RETRIES = 3
DRIVE_TIMEOUT_SECONDS = 60
# Logical keys a report lists per source and kind of change; the rest are counted.
REPORT_KEYS = 20
# Failures of the world outside, not of the code: reported in one line, no traceback.
# A run has already recorded them.
EXPECTED_ERRORS = (APIError, DriveError, CatalogSchemaError, FetchError)
SOURCES = (
    catalog.SOURCE_ID,
    grc.SOURCE_ID,
    gtech.WEBSITE_SOURCE_ID,
    gtech.DOCS_SOURCE_ID,
    gtech.DOCUMENTS_SOURCE_ID,
)

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
        "--document-file",
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="a local copy of a listed PDF, such as a Drive file the local credentials "
        f"cannot read; keys: {', '.join(d.key for d in GTECH_DOCUMENTS)}",
    )
    run.add_argument(
        "--chunking",
        choices=["block", "item"],
        default="block",
        help="GRC lists as one chunk per block (default) or per item, for comparing the two",
    )
    run.add_argument(
        "--policy",
        choices=["publish", "dry-run"],
        default="publish",
        help="dry-run builds and validates a version without publishing it",
    )
    run.add_argument(
        "--scheduled",
        action="store_true",
        help="started by the schedule: goes ahead only on the first Sunday of the month, "
        "Taipei time, and is recorded as scheduled",
    )
    run.set_defaults(handler=_run)

    search = commands.add_parser("search", help="semantic search over the active version")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5, help="number of chunks to show")
    search.set_defaults(handler=_search)

    status = commands.add_parser("status", help="show the active version and recent runs")
    status.set_defaults(handler=_status)

    report = commands.add_parser("report", help="show a version's checks and what it changes")
    report.add_argument("version", type=int)
    report.set_defaults(handler=_report)

    accept = commands.add_parser("accept", help="publish a held version as it was built")
    accept.add_argument("version", type=int)
    accept.set_defaults(handler=_accept)

    rollback = commands.add_parser("rollback", help="point back at a version published before")
    rollback.add_argument("version", type=int)
    rollback.set_defaults(handler=_rollback)

    args = parser.parse_args(argv)
    if args.command == "run" and args.scheduled and not scheduled_run_due(_now()):
        # Before any setting or connection is needed: a Sunday that is not the first costs
        # one container start and nothing else.
        configure_logging()
        logger.info("scheduled run skipped: not the first Sunday of the month in Taipei")
        return 0
    settings = get_settings()
    if args.command in ("run", "search") and settings.openai_api_key is None:
        parser.error("OPENAI_API_KEY is not set")
    if args.command == "run":
        if unknown := set(args.sources) - set(SOURCES):
            parser.error(f"unknown sources {sorted(unknown)}")
        needs_catalog = catalog.SOURCE_ID in args.sources
        if needs_catalog and not (args.catalog_file or settings.product_catalog_file_id):
            parser.error("pass --catalog-file or set PRODUCT_CATALOG_FILE_ID")
        try:
            args.document_files = document_files(args.document_file)
        except ValueError as error:
            parser.error(str(error))
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


def _openai(settings: Settings) -> AsyncOpenAI:
    assert settings.openai_api_key is not None
    return AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=OPENAI_MAX_RETRIES,
    )


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
    openai_client = _openai(settings)
    try:
        async with new_client() as client:
            sources: list[Source] = []
            if catalog.SOURCE_ID in args.sources:
                sources.append(ProductCatalogSource(_fetch_catalog(args.catalog_file, settings)))
            if grc.SOURCE_ID in args.sources:
                lectures = CachedReader(
                    OpenAILectureReader(openai_client, settings.extraction_model),
                    DbAnswerCache(engine),
                )
                sources.append(
                    GrcWebsiteSource(
                        PageFetcher(client, {GRC_HOST}),
                        lectures,
                        per_item=args.chunking == "item",
                    )
                )
            # One reader for every source, so an image two sources show is read once.
            vision = CachedReader[VisualInput](
                OpenAIVisionReader(openai_client, settings.vision_model),
                DbAnswerCache(engine),
                cache_input=visual_input,
            )
            if gtech.WEBSITE_SOURCE_ID in args.sources:
                image_host = PageFetcher(client, {IMAGE_HOST}, max_bytes=IMAGE_MAX_BYTES)
                sources.append(
                    GtechWebsiteSource(
                        PageFetcher(client, {GTECH_HOST}), ImageReading(image_host.fetch, vision)
                    )
                )
            if gtech.DOCS_SOURCE_ID in args.sources:
                docs = PageFetcher(client, {DOCS_HOST})

                async def fetch_docs_image(url: str) -> FetchedPage:
                    return await docs.fetch(url, max_bytes=IMAGE_MAX_BYTES)

                sources.append(GtechDocsSource(docs, ImageReading(fetch_docs_image, vision)))
            if gtech.DOCUMENTS_SOURCE_ID in args.sources:
                fetch = document_fetcher(client, args.document_files)
                sources.append(GtechDocumentsSource(fetch, vision))
            run = {
                "policy": "dry_run" if args.policy == "dry-run" else "publish",
                "triggered_by": "schedule" if args.scheduled else "manual",
                "code_version": settings.app_version,
            }
            async with exclusive_run(engine) as alone:
                if not alone:
                    result = await skipped_run(engine, **run)
                else:
                    result = await run_ingestion(
                        engine,
                        _blob_store(settings),
                        OpenAIEmbedder(openai_client, settings.embedding_model),
                        sources,
                        max_drop=settings.publish_max_drop,
                        **run,
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
    for reason in result.held:
        print(f"  held: {reason}")
    if result.held:
        print(f"  review with `wobot-ingest report {result.index_version_id}`, then accept it")
    for error in result.errors:
        print(f"  error: {error}")


async def _search(args: argparse.Namespace, settings: Settings) -> int:
    embedder = OpenAIEmbedder(_openai(settings), settings.embedding_model)
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


def _now() -> datetime:
    return datetime.now(UTC)


async def _report(args: argparse.Namespace, settings: Settings) -> int:
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            version = await repository.read_version(conn, args.version)
            pointer = await repository.read_pointer(conn)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    if version is None:
        print(f"no version {args.version}")
        return 1
    active = " (active)" if pointer.index_version_id == version.index_version_id else ""
    print(f"version {version.index_version_id}: {version.status}{active}")
    print(
        f"  created {version.created_at:%Y-%m-%d %H:%M %Z}, embedded {version.embedding_config_id}"
    )
    if version.published_at is not None:
        print(f"  first published {version.published_at:%Y-%m-%d %H:%M %Z}")
    report = version.validation_report or {}
    for label in ("blocking", "held"):
        for message in report.get(label, []):
            print(f"  {label}: {message}")
    for source, details in report.get("sources", {}).items():
        print(f"  {source}: {details['counts']}, {len(details['warnings'])} warnings")
    for source, change in report.get("changes", {}).items():
        print(f"  {source}: {len(change['added'])} added, {len(change['removed'])} removed")
        for label in ("added", "removed"):
            shown = change[label][:REPORT_KEYS]
            for key in shown:
                print(f"    {label}: {key}")
            if len(change[label]) > len(shown):
                print(f"    … {len(change[label]) - len(shown)} more {label}")
    return 0


async def _accept(args: argparse.Namespace, settings: Settings) -> int:
    engine, connector = await create_engine(settings)
    try:
        await accept(engine, args.version)
    except MaintenanceError as error:
        print(f"not accepted: {error}")
        return 1
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    logger.info("version %s accepted and published", args.version)
    return 0


async def _rollback(args: argparse.Namespace, settings: Settings) -> int:
    engine, connector = await create_engine(settings)
    try:
        replaced = await rollback(engine, args.version)
    except MaintenanceError as error:
        print(f"not rolled back: {error}")
        return 1
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    logger.info("rolled back from version %s to version %s", replaced, args.version)
    return 0
