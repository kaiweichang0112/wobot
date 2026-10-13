"""Checks that conversations can be kept where the settings point: wobot-checkpoint check.

As the settings' database user, one throwaway thread is written, read back and deleted,
and each step timed. On Cloud Run it runs as a job with the API's service account and
Cloud SQL connection, which shows that the API's role may keep conversations (R1).
"""

import argparse
import asyncio
import time
import uuid
from collections.abc import Sequence

from langgraph.checkpoint.base import empty_checkpoint

from wobot.agent.checkpoints import open_checkpointer
from wobot.config import Settings, get_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wobot-checkpoint", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="write, read back and delete one throwaway thread")
    parser.parse_args(argv)
    return asyncio.run(check(get_settings()))


async def check(settings: Settings) -> int:
    thread_id = f"check-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    started = time.perf_counter()
    async with open_checkpointer(settings) as saver:
        opened = time.perf_counter()
        saved = await saver.aput(config, checkpoint, {"source": "input", "step": -1}, {})
        read = await saver.aget_tuple(saved)
        await saver.adelete_thread(thread_id)
        gone = await saver.aget_tuple(config)
        done = time.perf_counter()
    right = read is not None and read.checkpoint["id"] == checkpoint["id"] and gone is None
    print(
        f"{settings.db_mode} as {settings.db_user}: connected in {opened - started:.2f}s, "
        f"wrote, read back and deleted a thread in {done - opened:.2f}s: "
        + ("ok" if right else "FAILED")
    )
    return 0 if right else 1
