"""wobot-eval: score an index version against the labelled datasets, and check the labels.

Reads only, so it runs as the API's read-only role. Retrieval checks embed each question
once with the version's embedding model; every other check is computed from records.
Scores come from RAGAS, a dev dependency: this runs from a checkout, not in the API image.

`vision` is the exception: it asks vision models to read the dev pictures and keeps their
answers where ingestion does, so it runs as the ingestion role. `route` and `chat` compare
models for the chat graph's nodes on the route dataset; they call the models alone and
read no database. `rewrite` compares models for rewrite_query by what the retrieve node
then finds, so it reads the index as `run` does; `answer` compares models for the answer on
what the knowledge path finds. `list-agent` and `write-list` compare models for the list
path's two model nodes on lists-v1, querying the records as the path does. `turns` plays
turns-v1's conversations through the configured graph and scores each last turn.
"""

import argparse
import asyncio
import subprocess
from collections.abc import Sequence
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from openai import AsyncOpenAI

from wobot.agent import answer as answering
from wobot.agent import chat as chatting
from wobot.agent import list_agent as listing
from wobot.agent import retrieval
from wobot.agent import rewrite as rewriting
from wobot.agent import route as routing
from wobot.agent import write_list as writing
from wobot.agent.graph import build_graph, models_for
from wobot.agent.models import openai_model
from wobot.agent.route import router_for
from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.eval import gold
from wobot.eval.answer import DATASET as ANSWER_DATASET
from wobot.eval.answer import answer_cases, knowledge_lookup, scored_cases
from wobot.eval.answer import compare as compare_answer
from wobot.eval.candidates import Candidate, parse_candidate
from wobot.eval.chat import chat_cases
from wobot.eval.chat import compare as compare_chat
from wobot.eval.corpus import Corpus, active_version, load_corpus
from wobot.eval.dataset import dataset_names, load_dataset
from wobot.eval.lists import DATASET as LIST_DATASET
from wobot.eval.lists import (
    ModelWriter,
    compare_agents,
    compare_writers,
    list_cases,
    list_path,
    write_cases,
)
from wobot.eval.report import (
    summary,
    write_answer_report,
    write_chat_report,
    write_list_agent_report,
    write_report,
    write_rewrite_report,
    write_route_report,
    write_turns_report,
    write_vision_report,
    write_write_list_report,
)
from wobot.eval.rewrite import (
    BASELINE,
    DATASETS,
    ModelRewriter,
    as_is,
    retrieve_node_search,
    rewrite_cases,
)
from wobot.eval.rewrite import compare as compare_rewrite
from wobot.eval.route import compare, route_cases
from wobot.eval.runner import case_refs, run_datasets
from wobot.eval.turns import DATASET as TURNS_DATASET
from wobot.eval.turns import play_all, turn_cases
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

    rewrite = commands.add_parser(
        "rewrite", help="compare models for rewrite_query by what their searches find (paid calls)"
    )
    rewrite.add_argument(
        "--model",
        action="append",
        required=True,
        type=parse_candidate,
        metavar="MODEL:EFFORT",
        help="a candidate, such as gpt-6-luna:none or gpt-4o:default; repeat for more",
    )
    rewrite.add_argument("--runs", type=int, default=3, help="times each case is played")
    rewrite.add_argument("--split", choices=["all", "dev", "heldout"], default="dev")
    rewrite.add_argument("--index-version", type=int, help="a version other than the active one")
    rewrite.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    rewrite.set_defaults(handler=_rewrite)

    answer = commands.add_parser(
        "answer", help="compare models for the answer on what the search finds (paid calls)"
    )
    answer.add_argument(
        "--model",
        action="append",
        required=True,
        type=parse_candidate,
        metavar="MODEL:EFFORT",
        help="a candidate, such as gpt-6-luna:none or gpt-4o:default; repeat for more",
    )
    answer.add_argument("--runs", type=int, default=3, help="times each case is played")
    answer.add_argument("--split", choices=["all", "dev", "heldout"], default="dev")
    answer.add_argument("--index-version", type=int, help="a version other than the active one")
    answer.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    answer.set_defaults(handler=_answer)

    agent = commands.add_parser(
        "list-agent", help="compare models for list_agent by the calls they make (paid calls)"
    )
    agent.add_argument(
        "--model",
        action="append",
        required=True,
        type=parse_candidate,
        metavar="MODEL:EFFORT",
        help="a candidate, such as gpt-6-luna:none or gpt-4o:default; repeat for more",
    )
    agent.add_argument(
        "--writer",
        type=parse_candidate,
        metavar="MODEL:EFFORT",
        help="the model every candidate's lists are written with (default: write_list's)",
    )
    agent.add_argument("--runs", type=int, default=3, help="times each case is played")
    agent.add_argument("--split", choices=["all", "dev", "heldout"], default="dev")
    agent.add_argument("--index-version", type=int, help="a version other than the active one")
    agent.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    agent.set_defaults(handler=_list_agent)

    writer = commands.add_parser(
        "write-list", help="compare models for write_list on the same lists (paid calls)"
    )
    writer.add_argument(
        "--model",
        action="append",
        required=True,
        type=parse_candidate,
        metavar="MODEL:EFFORT",
        help="a candidate, such as gpt-6-luna:none or gpt-4o:default; repeat for more",
    )
    writer.add_argument(
        "--agent",
        type=parse_candidate,
        metavar="MODEL:EFFORT",
        help="the model that finds each case's lists once (default: list_agent's)",
    )
    writer.add_argument("--runs", type=int, default=3, help="times each case is written")
    writer.add_argument("--split", choices=["all", "dev", "heldout"], default="dev")
    writer.add_argument("--index-version", type=int, help="a version other than the active one")
    writer.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    writer.set_defaults(handler=_write_list)

    turns = commands.add_parser(
        "turns", help="play conversations through the configured graph (paid calls)"
    )
    turns.add_argument("--runs", type=int, default=3, help="times each conversation is played")
    turns.add_argument("--split", choices=["all", "dev", "heldout"], default="dev")
    turns.add_argument("--index-version", type=int, help="a version other than the active one")
    turns.add_argument("--out", type=Path, default=RUNS_DIR, help="where reports are written")
    turns.set_defaults(handler=_turns)

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


async def _rewrite(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set")
        return 1
    if jev := [c.label for c in args.model if c.effort == "-"]:
        print(f"{jev}: Jev classifies and writes no plans")
        return 1
    datasets = [load_dataset(name) for name in DATASETS]
    splits = ("dev", "heldout") if args.split == "all" else (args.split,)
    rewriters = [(BASELINE, as_is)] + [
        (c, ModelRewriter(openai_model(settings, c.model, c.effort))) for c in args.model
    ]
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            version_id = args.index_version or await active_version(conn)
            if version_id is None:
                print("no active version; pass --index-version")
                return 1
            corpus = await load_corpus(conn, version_id)
        cases, gold_files = rewrite_cases(datasets, splits, corpus, gold.GOLD_DIR)
        search = retrieve_node_search(engine, _embedder(settings, corpus), version_id)
        started = datetime.now().astimezone()
        results = await compare_rewrite(rewriters, cases, search, args.runs, progress=print)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "code_version": _code_version(),
        "prompt_version": rewriting.PROMPT_VERSION,
        "index_version": version_id,
        "embedding_config": corpus.embedding_config_id,
        "datasets": {d.name: d.sha256[:12] for d in datasets},
        "gold_files": {name: sha[:12] for name, sha in sorted(gold_files.items())},
        "splits": list(splits),
        "cases": len(cases),
        "runs": args.runs,
        "prices": "spec 07, checked 2026-10-01; gpt-4o's model page, 2026-10-06",
    }
    stem = f"{started:%Y%m%d-%H%M%S}-rewrite"
    path = write_rewrite_report(results, cases, meta, args.out, stem)
    for r in results:
        print(
            f"{r.candidate.label}: passed {r.mean_passed:.1f} / {r.cases}, "
            f"p50 {r.latency(0.5):.2f}s, p95 {r.latency(0.95):.2f}s, {r.errors} errors"
        )
    print(f"report: {path}")
    return 0


async def _answer(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set")
        return 1
    if jev := [c.label for c in args.model if c.effort == "-"]:
        print(f"{jev}: Jev classifies and writes no replies")
        return 1
    dataset = load_dataset(ANSWER_DATASET)
    splits = ("dev", "heldout") if args.split == "all" else (args.split,)
    rewrite = Candidate(settings.rewrite_model, settings.rewrite_effort)
    rewriter = ModelRewriter(openai_model(settings, rewrite.model, rewrite.effort))
    models = [(c, openai_model(settings, c.model, c.effort)) for c in args.model]
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            version_id = args.index_version or await active_version(conn)
            if version_id is None:
                print("no active version; pass --index-version")
                return 1
            corpus = await load_corpus(conn, version_id)
        lookup = knowledge_lookup(engine, _embedder(settings, corpus), version_id)
        started = datetime.now().astimezone()
        cases = await answer_cases(scored_cases([dataset], splits), rewriter, lookup, print)
        results = await compare_answer(models, cases, args.runs, progress=print)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "code_version": _code_version(),
        "prompt_version": answering.PROMPT_VERSION,
        "evidence": (
            f"rewrite {rewrite.label} prompt {rewriting.PROMPT_VERSION}, index version "
            f"{version_id}, depth {retrieval.FUSION_DEPTH}, k {retrieval.SEARCHED_CHUNKS}"
        ),
        "dataset": f"{dataset.name} {dataset.sha256[:12]}",
        "splits": list(splits),
        "cases": len(cases),
        "runs": args.runs,
        "prices": "spec 07, checked 2026-10-01; gpt-4o's model page, 2026-10-06",
    }
    path = write_answer_report(results, cases, meta, args.out, f"{started:%Y%m%d-%H%M%S}-answer")
    for r in results:
        print(
            f"{r.candidate.label}: correct {r.mean_correct:.1f}, first words p50 "
            f"{r.first_token(0.5):.2f}s, total p50 {r.total(0.5):.2f}s p95 "
            f"{r.total(0.95):.2f}s, {r.errors} errors"
        )
    print(f"report: {path}")
    return 0


def _list_meta(
    started: datetime,
    dataset: Any,
    version_id: int,
    gold_files: dict[str, str],
    splits: Sequence[str],
    cases: int,
    runs: int,
) -> dict[str, Any]:
    return {
        "started_at": started.isoformat(timespec="seconds"),
        "code_version": _code_version(),
        "prompt_versions": (
            f"list_agent {listing.PROMPT_VERSION}, write_list {writing.PROMPT_VERSION}"
        ),
        "index_version": version_id,
        "dataset": f"{dataset.name} {dataset.sha256[:12]}",
        "gold_files": {name: sha[:12] for name, sha in sorted(gold_files.items())},
        "splits": list(splits),
        "cases": cases,
        "runs": runs,
        "prices": "spec 07, checked 2026-10-01; gpt-4o's model page, 2026-10-06",
    }


async def _list_agent(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set")
        return 1
    if jev := [c.label for c in args.model if c.effort == "-"]:
        print(f"{jev}: Jev classifies and calls no tools")
        return 1
    dataset = load_dataset(LIST_DATASET)
    splits = ("dev", "heldout") if args.split == "all" else (args.split,)
    writer = args.writer or Candidate(settings.write_list_model, settings.write_list_effort)
    agents = [(c, openai_model(settings, c.model, c.effort)) for c in args.model]
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            version_id = args.index_version or await active_version(conn)
            if version_id is None:
                print("no active version; pass --index-version")
                return 1
            corpus = await load_corpus(conn, version_id)
        cases, gold_files = list_cases([dataset], splits, corpus, gold.GOLD_DIR)
        started = datetime.now().astimezone()
        results = await compare_agents(
            agents,
            openai_model(settings, writer.model, writer.effort),
            cases,
            engine,
            version_id,
            args.runs,
            progress=print,
        )
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    meta = _list_meta(started, dataset, version_id, gold_files, splits, len(cases), args.runs)
    meta["writer"] = writer.label
    path = write_list_agent_report(
        results, cases, meta, args.out, f"{started:%Y%m%d-%H%M%S}-list-agent"
    )
    for r in results:
        print(
            f"{r.candidate.label}: calls right {r.mean_correct:.1f} / {len(cases)}, "
            f"agent p50 {r.latency(0.5):.2f}s p95 {r.latency(0.95):.2f}s, "
            f"path p95 {r.total(0.95):.2f}s, {r.errors} errors"
        )
    print(f"report: {path}")
    return 0


async def _write_list(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set")
        return 1
    if jev := [c.label for c in args.model if c.effort == "-"]:
        print(f"{jev}: Jev classifies and writes no replies")
        return 1
    dataset = load_dataset(LIST_DATASET)
    splits = ("dev", "heldout") if args.split == "all" else (args.split,)
    agent = args.agent or Candidate(settings.list_agent_model, settings.list_agent_effort)
    default_writer = openai_model(settings, settings.write_list_model, settings.write_list_effort)
    writers = [(c, ModelWriter(openai_model(settings, c.model, c.effort))) for c in args.model]
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            version_id = args.index_version or await active_version(conn)
            if version_id is None:
                print("no active version; pass --index-version")
                return 1
            corpus = await load_corpus(conn, version_id)
        cases, gold_files = list_cases([dataset], splits, corpus, gold.GOLD_DIR)
        finder = list_path(
            openai_model(settings, agent.model, agent.effort), default_writer, engine
        )
        started = datetime.now().astimezone()
        prepared = await write_cases(cases, finder, version_id, print)
        results = await compare_writers(writers, prepared, args.runs, progress=print)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    meta = _list_meta(started, dataset, version_id, gold_files, splits, len(cases), args.runs)
    meta["lists found by"] = agent.label
    path = write_write_list_report(
        results, prepared, meta, args.out, f"{started:%Y%m%d-%H%M%S}-write-list"
    )
    scored = sum(c.expected is not None for c in prepared)
    for r in results:
        print(
            f"{r.candidate.label}: correct {r.mean_correct:.1f} / {scored}, "
            f"p50 {r.latency(0.5):.2f}s p95 {r.latency(0.95):.2f}s, {r.errors} errors"
        )
    print(f"report: {path}")
    return 0


async def _turns(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set")
        return 1
    dataset = load_dataset(TURNS_DATASET)
    splits = ("dev", "heldout") if args.split == "all" else (args.split,)
    cases = turn_cases([dataset], splits)
    engine, connector = await create_engine(settings)
    try:
        async with engine.connect() as conn:
            version_id = args.index_version or await active_version(conn)
            if version_id is None:
                print("no active version; pass --index-version")
                return 1
            corpus = await load_corpus(conn, version_id)
        embedder = _embedder(settings, corpus)
        app = build_graph(models_for(settings), engine, embedder, MemorySaver())
        started = datetime.now().astimezone()
        result = await play_all(app, cases, version_id, args.runs, progress=print)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    nodes = ("chat", "rewrite", "answer", "list_agent", "write_list")
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "code_version": _code_version(),
        "models": f"classify {settings.classify_model}, "
        + ", ".join(
            f"{node} {getattr(settings, f'{node}_model')}:{getattr(settings, f'{node}_effort')}"
            for node in nodes
        ),
        "index_version": version_id,
        "dataset": f"{dataset.name} {dataset.sha256[:12]}",
        "splits": list(splits),
        "cases": len(cases),
        "runs": args.runs,
    }
    path = write_turns_report(result, cases, meta, args.out, f"{started:%Y%m%d-%H%M%S}-turns")
    print(
        f"right {result.mean_correct:.1f} / {len(cases)}, last turn p50 "
        f"{result.latency(0.5):.2f}s p95 {result.latency(0.95):.2f}s, {result.errors} errors"
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
                print(f"    {r.ref.where}: {r.ref.kind} {gold.label_value(r.ref)!r}{problem}")
            if case.case_id in args.show:
                for r in resolved:
                    for key in r.keys:
                        print(f"    {r.ref.where}: {gold.label_value(r.ref)[:120]!r} -> {key}")
    return 1 if problems else 0
