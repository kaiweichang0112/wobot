"""wobot-sweep: one pass of the daily sweep, as one execution of the `wobot-sweep` Cloud
Run job, which Cloud Scheduler starts every day.

    wobot-sweep

Without `ASSETS_JOB`, as in development, it does all but start asset work, and says how
much it left.
"""

import argparse
import asyncio
import dataclasses
import logging
import sys
from collections.abc import Sequence

import httpx2
from sqlalchemy.ext.asyncio import async_sessionmaker

from wobot.assets.dispatch import CloudRunJob, GoogleToken
from wobot.config import get_settings
from wobot.db import create_engine
from wobot.logs import configure_logging
from wobot.maintenance.sweep import Report, sweep

logger = logging.getLogger(__name__)


async def run() -> Report:
    settings = get_settings()
    engine, connector = await create_engine(settings)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        if settings.assets_job is None:
            return await sweep(sessions, None)
        async with httpx2.AsyncClient(timeout=30) as client:
            job = CloudRunJob(
                client,
                settings.google_cloud_project,
                settings.assets_job_region,
                settings.assets_job,
                GoogleToken(),
            )
            return await sweep(sessions, job)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()


def main(argv: Sequence[str] | None = None) -> None:
    argparse.ArgumentParser(prog="wobot-sweep", description=__doc__).parse_args(argv)
    configure_logging()
    try:
        report = asyncio.run(run())
    except Exception:
        # The non-zero exit marks the execution failed; tomorrow's pass starts over.
        logger.exception("sweep failed")
        sys.exit(1)
    logger.info("sweep finished", extra=dataclasses.asdict(report))
