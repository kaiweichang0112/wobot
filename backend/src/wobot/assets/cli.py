"""wobot-assets: make or remove one voice or avatar version, as one execution of the
`wobot-assets` Cloud Run job.

    wobot-assets make voice <voice_id>
    wobot-assets clean voice <voice_id>
"""

import argparse
import asyncio
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx2
from sqlalchemy.ext.asyncio import async_sessionmaker

from wobot.assets.eleven import ElevenLabs
from wobot.assets.media import media_store
from wobot.assets.worker import Worker, perform
from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.logs import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def eleven_client(settings: Settings) -> AsyncIterator[ElevenLabs | None]:
    """ElevenLabs, when its key is set."""
    if settings.elevenlabs_api_key is None:
        yield None
        return
    async with httpx2.AsyncClient(timeout=settings.elevenlabs_timeout_seconds) as client:
        yield ElevenLabs(client, settings.elevenlabs_api_key.get_secret_value())


async def run(command: str, kind: str, version_id: uuid.UUID) -> str:
    settings = get_settings()
    engine, connector = await create_engine(settings)
    try:
        async with eleven_client(settings) as eleven:
            worker = Worker(
                sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
                media=media_store(settings),
                eleven=eleven,
                # Cloud Run names each execution of a job in this variable.
                execution=os.environ.get("CLOUD_RUN_EXECUTION", "local"),
            )
            return await perform(worker, command, kind, version_id)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wobot-assets",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["make", "clean"])
    parser.add_argument("kind", choices=["voice"])
    parser.add_argument("version_id", type=uuid.UUID)
    args = parser.parse_args(argv)
    configure_logging()
    try:
        outcome = asyncio.run(run(args.command, args.kind, args.version_id))
    except Exception:
        # The version is already marked failed; the non-zero exit marks the execution.
        logger.exception("asset work failed")
        sys.exit(1)
    # A failed version is an outcome, not a crash: the app shows it and offers a retry.
    logger.info(
        "asset work done",
        extra={"command": args.command, "kind": args.kind, "outcome": outcome},
    )
