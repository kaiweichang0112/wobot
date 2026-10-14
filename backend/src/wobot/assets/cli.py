"""wobot-assets: make or remove one voice or avatar version, as one execution of the
`wobot-assets` Cloud Run job; or paint avatar frames on this machine, to try prompts.

    wobot-assets make voice|avatar <version_id>
    wobot-assets clean voice|avatar <version_id>
    wobot-assets paint --photo <path> [--frames <key> ... [--only]] [--quality medium]
                       [--from <dir>]

`paint` touches no database or bucket: it writes the frames, a sheet of them on the app's
backdrop and a report to `.data/avatar/<run>/`, never committed, and prints what each
frame cost.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx2
from sqlalchemy.ext.asyncio import async_sessionmaker

from wobot.assets.avatar import (
    Blocked,
    Painted,
    contact_sheet,
    paint_all,
    painter_of,
    prepare_photo,
)
from wobot.assets.avatar_prompts import PROMPT_VERSION, prompt_hash
from wobot.assets.eleven import ElevenLabs
from wobot.assets.frames import FRAME_KEYS, descendants
from wobot.assets.media import media_store
from wobot.assets.worker import Worker, perform
from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.logs import configure_logging

AVATAR_RUNS = Path(".data/avatar")

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
        async with eleven_client(settings) as eleven, painter_of(settings) as painter:
            worker = Worker(
                sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
                media=media_store(settings),
                eleven=eleven,
                # Cloud Run names each execution of a job in this variable.
                execution=os.environ.get("CLOUD_RUN_EXECUTION", "local"),
                painter=painter,
                concurrency=settings.avatar_concurrency,
            )
            return await perform(worker, command, kind, version_id)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()


def paint_sources(photo: Path, frames: list[str], reuse: Path | None) -> dict[str | None, bytes]:
    """The photo, and frames an earlier run made that the frames painted now come from."""
    sources: dict[str | None, bytes] = {None: prepare_photo(photo.read_bytes())}
    if reuse is not None:
        for key in FRAME_KEYS:
            if key not in frames and (reuse / f"{key}.png").exists():
                sources[key] = (reuse / f"{key}.png").read_bytes()
    return sources


async def paint_locally(
    out: Path, frames: list[str], sources: dict[str | None, bytes], quality: str | None
) -> dict:
    """Paints the frames into `out`; returns the run's report."""
    settings = get_settings()
    report: dict[str, dict] = {}
    async with painter_of(settings, quality) as painter:
        if painter is None:
            raise SystemExit("OPENAI_API_KEY is not set; run with --env-file .env")

        async def paint(frame_key: str, source: bytes) -> bytes:
            painted: Painted = await painter.paint(frame_key, source)
            await asyncio.to_thread((out / f"{frame_key}.png").write_bytes, painted.png)
            report[frame_key] = {
                "seconds": painted.seconds,
                "text_input_tokens": painted.text_input_tokens,
                "image_input_tokens": painted.image_input_tokens,
                "output_tokens": painted.output_tokens,
                "cost_usd": round(painted.cost, 4),
            }
            print(f"{frame_key:18} {painted.seconds:6.1f}s  ${painted.cost:.4f}", flush=True)
            return painted.png

        started = time.perf_counter()
        results = await paint_all(frames, sources, paint, settings.avatar_concurrency)
        elapsed = time.perf_counter() - started
    for key, result in results.items():
        if isinstance(result, BaseException):
            reason = "blocked" if isinstance(result, Blocked) else getattr(result, "code", "")
            report[key] = {"error": reason or type(result).__name__}
            print(f"{key:18} failed: {report[key]['error']}")
    return {
        "model": painter.model,
        "quality": painter.quality,
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": prompt_hash(),
        "seconds": round(elapsed, 1),
        "cost_usd": round(sum(r.get("cost_usd", 0) for r in report.values()), 4),
        "frames": {key: report[key] for key in FRAME_KEYS if key in report},
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wobot-assets",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("make", "clean"):
        work = commands.add_parser(name)
        work.add_argument("kind", choices=["voice", "avatar"])
        work.add_argument("version_id", type=uuid.UUID)
    paint = commands.add_parser("paint", help="paint avatar frames locally")
    paint.add_argument("--photo", type=Path, required=True)
    paint.add_argument(
        "--frames",
        nargs="+",
        choices=FRAME_KEYS,
        metavar="KEY",
        help="these frames, such as neutral_closed, and every frame made from them "
        "(default: all 21)",
    )
    paint.add_argument(
        "--only", action="store_true", help="the frames named alone, not what they make"
    )
    paint.add_argument("--quality", choices=["low", "medium", "high"])
    paint.add_argument(
        "--from",
        dest="reuse",
        type=Path,
        metavar="DIR",
        help="an earlier run's directory, whose frames serve as parents",
    )
    args = parser.parse_args(argv)
    configure_logging()

    if args.command == "paint":
        chosen = args.frames or list(FRAME_KEYS)
        made_from = [] if args.only else [d for key in chosen for d in descendants(key)]
        frames = sorted({*chosen, *made_from}, key=FRAME_KEYS.index)
        if args.frames and len(frames) > len(chosen):
            print(f"also painting what they make: {', '.join(sorted(set(frames) - set(chosen)))}")
        out = AVATAR_RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
        out.mkdir(parents=True)
        sources = paint_sources(args.photo, frames, args.reuse)
        report = asyncio.run(paint_locally(out, frames, sources, args.quality))
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        made = {
            key: (out / f"{key}.png").read_bytes()
            for key in frames
            if (out / f"{key}.png").exists()
        }
        earlier = {key: png for key, png in sources.items() if key is not None}
        sheet = contact_sheet(earlier | made, get_settings().avatar_backdrop)
        (out / "sheet.png").write_bytes(sheet)
        print(
            f"{len(frames)} frames in {report['seconds']:.0f}s, ${report['cost_usd']:.4f} -> {out}"
        )
        return
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
