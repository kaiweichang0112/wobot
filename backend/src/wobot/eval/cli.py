"""wobot-eval: score an index version against the labelled datasets, and check the labels.

Reads only, so it runs as the API's read-only role. Retrieval checks embed each question
once with the version's embedding model; every other check is computed from records.
Scores come from RAGAS, a dev dependency: this runs from a checkout, not in the API image.

`vision` is the exception: it asks vision models to read the dev pictures and keeps their
answers where ingestion does, so it runs as the ingestion role. `route` and `chat` compare
models for the chat graph's nodes on the route dataset; they call the models alone and
read no database.
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

from wobot.agent import chat as chatting
from wobot.agent import route as routing
from wobot.agent.models import openai_model
from wobot.agent.route import router_for
from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.eval import gold
from wobot.eval.candidates import parse_candidate
from wobot.eval.chat import chat_cases
from wobot.eval.chat import compare as compare_chat
from wobot.eval.corpus import Corpus, active_version, load_corpus
from wobot.eval.dataset import dataset_names, load_dataset
from wobot.eval.report import (
    summary,
    write_chat_report,
    write_report,
    write_route_report,
    write_vision_report,
)
from wobot.eval.route import compare, route_cases
from wobot.eval.runner import case_refs, run_datasets
from wobot.eval.vision import compare_models, dev_transcriptions, fetch_pictures
from wobot.knowledge.embeddings import OpenAIEmbedder
from wobot.knowledge.extraction import DbAnswerCache
from wobot.knowledge.sources.documents import document_files
from wobot.knowledge.sources.http import new_client
from wobot.knowledge.vision import PROMPT_VERSION as VISION_PROMPT_VERSION
from wobot.knowledge.vision import OpenAIVisionReader

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

    vision = commands.add_parser(
        "vision", help="compare vision models on the dev transcriptions (paid calls)"
    )
    vision.add_argument(
        "--model", action="append", required=True, help="a model to compare; repeat for more"
    )
    vision.add_argument(
        "--document-file",
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="a local copy of a listed PDF, as for wobot-ingest run",
    )
    vision.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    vision.set_defaults(handler=_vision)

    route = commands.add_parser(
        "route", help="compare the chat graph's routers on the route dataset (paid calls)"
    )
    route.add_argument(
        "--model",
        action="append",
        required=True,
        type=parse_candidate,
        metavar="MODEL[:EFFORT]",
        help="a candidate, such as gpt-6-luna:none or jev-latest; repeat for more",
    )
    route.add_argument("--runs", type=int, default=3, help="times each case is played")
    route.add_argument("--split", choices=["all", "dev", "heldout"], default="dev")
    route.add_argument("--dataset", default="route-v1", choices=dataset_names())
    route.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    route.set_defaults(handler=_route)

    chat = commands.add_parser(
        "chat", help="compare models for the chat path on the route dataset (paid calls)"
    )
    chat.add_argument(
        "--model",
        action="append",
        required=True,
        type=parse_candidate,
        metavar="MODEL:EFFORT",
        help="a candidate, such as gpt-6-luna:none; repeat for more",
    )
    chat.add_argument("--runs", type=int, default=5, help="times each case is played")
    chat.add_argument("--split", choices=["all", "dev", "heldout"], default="dev")
    chat.add_argument("--dataset", default="route-v1", choices=dataset_names())
    chat.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    chat.set_defaults(handler=_chat)

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


async def _vision(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set")
        return 1
    labelled = dev_transcriptions([load_dataset(name) for name in dataset_names()])
    if not labelled:
        print("no dev case is transcribed yet: see eval/gold/README.md")
        return 1
    started = datetime.now().astimezone()
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=3,
    )
    engine, connector = await create_engine(settings)
    try:
        async with new_client() as http:
            fixtures = await fetch_pictures(labelled, http, document_files(args.document_file))
        readers = [OpenAIVisionReader(client, model) for model in args.model]
        results = await compare_models(readers, fixtures, DbAnswerCache(engine))
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "code_version": _code_version(),
        "ragas": metadata.version("ragas"),
        "prompt_version": VISION_PROMPT_VERSION,
        "cases": [fixture.case_id for fixture in fixtures],
    }
    path = write_vision_report(results, meta, args.out, f"{started:%Y%m%d-%H%M%S}-vision")
    for r in results:
        print(
            f"{r.model}: text recall {r.mean('text_recall')}, values P {r.mean('value_precision')}"
            f" R {r.mean('value_recall')}, {r.input_tokens} + {r.output_tokens} tokens,"
            f" {r.calls} new calls"
        )
    print(f"report: {path}")
    return 0


async def _route(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None and any(c.effort != "-" for c in args.model):
        print("OPENAI_API_KEY is not set")
        return 1
    if settings.typesafe_api_key is None and any(c.effort == "-" for c in args.model):
        print("TYPESAFE_API_KEY is not set")
        return 1
    dataset = load_dataset(args.dataset)
    splits = ("dev", "heldout") if args.split == "all" else (args.split,)
    cases = route_cases([dataset], splits)
    routers = [(c, router_for(settings, c.model, c.effort)) for c in args.model]
    started = datetime.now().astimezone()
    results = await compare(routers, cases, args.runs, progress=print)
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "code_version": _code_version(),
        "prompt_version": routing.PROMPT_VERSION,
        "dataset": f"{dataset.name} {dataset.sha256[:12]}",
        "splits": list(splits),
        "cases": len(cases),
        "runs": args.runs,
        "prices": "spec 07, checked 2026-10-01",
    }
    path = write_route_report(results, meta, args.out, f"{started:%Y%m%d-%H%M%S}-route")
    for r in results:
        print(
            f"{r.candidate.label}: {r.accuracy:.3f}, p50 {r.latency(0.5):.2f}s, "
            f"p95 {r.latency(0.95):.2f}s, {r.errors} errors"
        )
    print(f"report: {path}")
    return 0


async def _chat(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set")
        return 1
    if jev := [c.label for c in args.model if c.effort == "-"]:
        print(f"{jev}: Jev classifies and writes no replies")
        return 1
    dataset = load_dataset(args.dataset)
    splits = ("dev", "heldout") if args.split == "all" else (args.split,)
    cases = chat_cases([dataset], splits)
    models = [(c, openai_model(settings, c.model, c.effort)) for c in args.model]
    started = datetime.now().astimezone()
    results = await compare_chat(models, cases, args.runs, progress=print)
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "code_version": _code_version(),
        "prompt_version": chatting.PROMPT_VERSION,
        "dataset": f"{dataset.name} {dataset.sha256[:12]}",
        "splits": list(splits),
        "cases": len(cases),
        "runs": args.runs,
        "prices": "spec 07, checked 2026-10-01",
    }
    stem = f"{started:%Y%m%d-%H%M%S}-chat"
    path = write_chat_report(results, cases, meta, args.out, stem)
    for r in results:
        print(
            f"{r.candidate.label}: first words p50 {r.first_token(0.5):.2f}s, "
            f"total p50 {r.total(0.5):.2f}s p95 {r.total(0.95):.2f}s, {r.errors} errors"
        )
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
