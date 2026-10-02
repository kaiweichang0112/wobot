"""wobot-eval: score an index version against the labelled datasets, and check the labels.

Reads only, so it runs as the API's read-only role. Retrieval checks embed each question
once with the version's embedding model; every other check is computed from records.
Scores come from RAGAS, a dev dependency: this runs from a checkout, not in the API image.
"""

import argparse
import asyncio
import subprocess
from collections.abc import Sequence
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.eval import gold
from wobot.eval.corpus import Corpus, active_version, load_corpus
from wobot.eval.dataset import dataset_names, load_dataset
from wobot.eval.report import summary, write_report
from wobot.eval.runner import case_refs, run_datasets
from wobot.knowledge.embeddings import OpenAIEmbedder

RUNS_DIR = Path(__file__).resolve().parents[3] / "eval" / "runs"
BACKEND_DIR = Path(__file__).resolve().parents[3]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wobot-eval", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="score a version against the datasets")
    run.add_argument(
        "--dataset",
        action="append",
        choices=dataset_names(),
        help="a dataset to run; repeat for several (default: all)",
    )
    run.add_argument("-k", type=int, default=5, help="chunks retrieved per question")
    run.add_argument("--index-version", type=int, help="a version other than the active one")
    run.add_argument("--split", choices=["all", "dev", "heldout"], default="all")
    run.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    run.set_defaults(handler=_run)

    check = commands.add_parser("check-gold", help="read the labels and match them to records")
    check.add_argument("--index-version", type=int, help="a version other than the active one")
    check.add_argument(
        "--show",
        type=lambda value: value.split(","),
        default=[],
        metavar="CASE_IDS",
        help="comma-separated cases whose labels are printed with the records they match, "
        "since a label that fits one record may still fit the wrong one",
    )
    check.set_defaults(handler=_check_gold)

    args = parser.parse_args(argv)
    return asyncio.run(args.handler(args, get_settings()))


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    datasets = [load_dataset(name) for name in (args.dataset or dataset_names())]
    splits = ("dev", "heldout") if args.split == "all" else (args.split,)
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            version_id = args.index_version or await active_version(conn)
            if version_id is None:
                print("no active version; pass --index-version")
                return 1
            corpus = await load_corpus(conn, version_id)
            embedder = _embedder(settings, corpus)
            started = datetime.now().astimezone()
            result = await run_datasets(conn, corpus, embedder, datasets, k=args.k, splits=splits)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    meta = _meta(started, corpus, args.k, datasets, result.gold_files, splits)
    stem = f"{started:%Y%m%d-%H%M%S}-v{version_id}-k{args.k}"
    path = write_report(result, meta, args.out, stem)
    for group, values in summary(result).items():
        print(f"{group}: {values}")
    print(f"report: {path}")
    return 0


def _embedder(settings: Settings, corpus: Corpus) -> OpenAIEmbedder | None:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set: retrieval cases stay pending")
        return None
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=3,
    )
    embedder = OpenAIEmbedder(client, settings.embedding_model)
    if embedder.config_id != corpus.embedding_config_id:
        raise SystemExit(
            f"version {corpus.version_id} was embedded with {corpus.embedding_config_id}, "
            f"not {embedder.config_id}"
        )
    return embedder


def _meta(
    started: datetime,
    corpus: Corpus,
    k: int,
    datasets: Sequence[Any],
    gold_files: dict[str, str],
    splits: Sequence[str],
) -> dict[str, Any]:
    extraction = sorted(
        {
            f"{item.fields['extraction_model']} prompt {item.fields['extraction_prompt_version']}"
            for item in corpus.of("lecture")
        }
    )
    return {
        "started_at": started.isoformat(timespec="seconds"),
        "code_version": _code_version(),
        "ragas": metadata.version("ragas"),
        "index_version": corpus.version_id,
        "embedding_config": corpus.embedding_config_id,
        "strategies": corpus.strategies,
        "extraction": extraction,
        "k": k,
        "splits": list(splits),
        "datasets": {d.name: d.sha256[:12] for d in datasets},
        "gold_files": {name: sha[:12] for name, sha in sorted(gold_files.items())},
    }


def _code_version() -> str:
    """The commit, marked dirty when the working tree differs: a run must be reproducible."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src"],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return get_settings().app_version
    return f"{commit}-dirty" if dirty else commit


async def _check_gold(args: argparse.Namespace, settings: Settings) -> int:
    datasets = [load_dataset(name) for name in dataset_names()]
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            version_id = args.index_version or await active_version(conn)
            if version_id is None:
                print("no active version; pass --index-version")
                return 1
            corpus = await load_corpus(conn, version_id)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    print(f"labels matched against version {version_id}")
    problems = 0
    for dataset in datasets:
        for case in (c for c in dataset.cases if c.check and "gold" in c.check):
            try:
                refs = case_refs(case, gold.GOLD_DIR, {})
            except gold.GoldError as error:
                problems += 1
                print(f"  {case.case_id}: error: {error}")
                continue
            if not refs:
                print(f"  {case.case_id}: not labelled yet ({case.check['gold']['file']})")
                continue
            resolved = gold.resolve(refs, corpus)
            unresolved = [r for r in resolved if not r.keys]
            problems += len(unresolved)
            print(f"  {case.case_id}: {len(refs)} labels, {len(unresolved)} match no record")
            for r in unresolved:
                problem = f" ({r.problem})" if r.problem else ""
                print(f"    {r.ref.where}: {r.ref.kind} {r.ref.value!r}{problem}")
            if case.case_id in args.show:
                for r in resolved:
                    for key in r.keys:
                        print(f"    {r.ref.where}: {r.ref.value!r} -> {key}")
    return 1 if problems else 0
