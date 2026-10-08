"""Compares models for the list path (DV11): list_agent by the tools and filters it calls,
then write_list by the lists it shows, both on lists-v1.

list_agent's candidates play the path through the chat graph's own nodes, from
list_agent to write_list, with one writer for all: the calls and the lists differ only
by the agent. write_list's candidates then read the same lists, found once per case by
one agent, so only the writer differs.

A set of calls is right when it is one the case accepts. The calls that count are those
that found something; when none did, those the tools ran, so a list the sources lack is
still asked for with the filters the message gave.
"""

import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.graph.state import CompiledStateGraph

from wobot.agent.graph import Models, build_graph
from wobot.agent.rewrite import writes_chinese
from wobot.agent.route import Routed
from wobot.agent.write_list import ListIntro, list_reply, lists_that_count, write_list_prompt
from wobot.eval import candidates, gold
from wobot.eval.candidates import Candidate, case_messages, cost_per_thousand, lacking
from wobot.eval.corpus import Corpus
from wobot.eval.dataset import Case, Dataset
from wobot.eval.metrics import SetScore, set_score
from wobot.eval.runner import case_refs
from wobot.knowledge.repository import Database

DATASET = "lists-v1"

# A call as scored: its tool and every filter it gave, as {"tool": name, **filters}.
Call = dict[str, Any]


@dataclass(frozen=True)
class ListCase:
    case: Case
    query_time: datetime  # the dataset's: "the last three years" counts back from it
    accepted: tuple[tuple[Call, ...], ...]  # every acceptable set of calls
    gold: frozenset[str] | None  # the items the reply must show, when labelled
    mentions: tuple[Any, ...]
    no_lists: bool


def list_cases(
    datasets: Sequence[Dataset], splits: Sequence[str], corpus: Corpus, gold_dir: Path
) -> tuple[list[ListCase], dict[str, str]]:
    """The list path's cases, with their labels matched to the corpus; and the hash of
    each gold file read. A label no record matches stays, so no reply can show it."""
    found: list[ListCase] = []
    gold_files: dict[str, str] = {}
    for dataset in datasets:
        query_time = datetime.fromisoformat(dataset.query_time)
        for case in candidates.cases_of([dataset], splits, "list_path"):
            check = case.check
            labelled = None
            if "gold" in check:
                resolved = gold.resolve(case_refs(case, gold_dir, gold_files), corpus)
                labelled = frozenset(key for r in resolved for key in r.keys)
                labelled |= {f"unresolved {r.ref.where}" for r in resolved if not r.keys}
            found.append(
                ListCase(
                    case,
                    query_time,
                    tuple(tuple(accepted) for accepted in check["calls"]),
                    labelled,
                    tuple(check.get("mentions", ())),
                    bool(check.get("no_lists")),
                )
            )
    return found, gold_files


def _call_matches(call: Call, expected: Call) -> bool:
    """The same tool and filters; a list for contains gives the spellings accepted."""
    if call.keys() != expected.keys():
        return False
    return all(
        call[name] in want if name == "contains" else call[name] == want
        for name, want in expected.items()
    )


def calls_right(calls: Sequence[Call], accepted: Sequence[Sequence[Call]]) -> bool:
    """Whether the calls, each counted once, are one of the accepted sets."""
    unique = [c for n, c in enumerate(calls) if c not in calls[:n]]
    return any(
        len(unique) == len(expected)
        and all(any(_call_matches(c, e) for e in expected) for c in unique)
        and all(any(_call_matches(c, e) for c in unique) for e in expected)
        for expected in accepted
    )


def counted_calls(results: Mapping[str, Any]) -> list[Call]:
    """The calls of the lists that count: those that found something, or, when none
    did, every call the tools ran. write_list reads the same lists."""
    return [{"tool": r["tool"], **r["filters"]} for r in lists_that_count(results).values()]


def shown_keys(reply: Mapping[str, Any]) -> set[str]:
    return {
        key for shown in reply.get("lists", []) for key in shown["keys"] + shown["uncertain_keys"]
    }


async def _lists_score(item: ListCase, shown: set[str]) -> SetScore | None:
    return None if item.gold is None else await set_score(set(item.gold), shown)


def list_f1(score: SetScore | None) -> float | None:
    """The items shown against the labelled ones. Showing none of them scores 0: RAGAS
    has no precision for an empty answer, and leaving it out would hide the failure."""
    if score is None:
        return None
    if score.actual == 0:
        return 0.0 if score.expected else 1.0
    return score.f1


# --- list_agent ------------------------------------------------------------------------


async def always_list(messages: Any, pending_question: Any) -> Routed:
    """A router that sends every message down the list path: classify is route-v1's."""
    return Routed("list", None)


def list_path(agent: BaseChatModel, writer: BaseChatModel, db: Database) -> CompiledStateGraph:
    """The chat graph with every message routed to lists. Its other models are never
    called on this path, so the writer stands in for them."""
    models = Models(
        router=always_list,
        chat=writer,
        rewrite=writer,
        answer=writer,
        list_agent=agent,
        write_list=writer,
        rec_agent=agent,
    )
    return build_graph(models, db, embedder=None)


@dataclass(frozen=True)
class ListPlay:
    case_id: str
    run: int
    rounds: tuple[tuple[Call, ...], ...]  # the calls of each round the model called tools
    counted: tuple[Call, ...]
    first_right: bool
    right: bool  # the counted calls are an accepted set
    model_calls: int  # times the agent's model was asked
    lists: SetScore | None  # the items shown against the labelled ones
    lacking: tuple[str, ...]
    shows_lists: bool
    text: str
    agent_seconds: float  # every round of list_agent
    seconds: float  # list_agent to the end of write_list
    input_tokens: int
    output_tokens: int
    error: str | None = None


async def play(app: CompiledStateGraph, item: ListCase, version_id: int, run: int) -> ListPlay:
    """One case through the list path, timed node by node."""
    case = item.case
    state = {
        "messages": case_messages(case),
        "index_version": version_id,
        "query_time": item.query_time,
        "chatbot_name": case.chatbot_name or "Wobot",
    }
    spent: dict[str, float] = defaultdict(float)
    final: dict[str, Any] = {}
    started = last = time.perf_counter()
    error = None
    try:
        async for mode, chunk in app.astream(state, stream_mode=["updates", "values"]):
            if mode == "values":
                final = chunk
                continue
            now = time.perf_counter()
            for node in chunk:
                spent[node] += now - last
            last = now
    except Exception as failure:  # a failed play is wrong, reported, and left out of latency
        error = repr(failure)
    seconds = time.perf_counter() - started - spent["classify"]
    reply = final.get("reply") or {}
    if error is None and reply.get("retryable"):
        error = "the path reported a failure"
    asked = [m for m in final.get("list_messages", []) if isinstance(m, AIMessage)]
    rounds = tuple(
        tuple(
            {"tool": c["name"], **{k: v for k, v in c["args"].items() if v is not None}}
            for c in m.tool_calls
        )
        for m in asked
        if m.tool_calls
    )
    counted = tuple(counted_calls(final.get("list_results", {})))
    shown = shown_keys(reply)
    usage = [m.usage_metadata or {} for m in asked]
    text = reply.get("text", "")
    return ListPlay(
        case_id=case.case_id,
        run=run,
        rounds=rounds,
        counted=counted,
        first_right=error is None and bool(rounds) and calls_right(rounds[0], item.accepted),
        right=error is None and calls_right(counted, item.accepted),
        model_calls=len(asked),
        lists=await _lists_score(item, shown) if error is None else None,
        lacking=tuple(lacking(text, item.mentions)),
        shows_lists=bool(shown),
        text=text,
        agent_seconds=spent["list_agent"],
        seconds=seconds,
        input_tokens=sum(u.get("input_tokens", 0) for u in usage),
        output_tokens=sum(u.get("output_tokens", 0) for u in usage),
        error=error,
    )


@dataclass
class AgentResult:
    candidate: Candidate
    runs: int
    plays: list[ListPlay] = field(default_factory=list)

    @property
    def done(self) -> list[ListPlay]:
        return [p for p in self.plays if p.error is None]

    def _per_run(self, right: Callable[[ListPlay], bool]) -> list[int]:
        return [sum(right(p) for p in self.plays if p.run == run) for run in range(self.runs)]

    def correct_per_run(self) -> list[int]:
        return self._per_run(lambda p: p.right)

    def first_right_per_run(self) -> list[int]:
        return self._per_run(lambda p: p.first_right)

    @property
    def mean_correct(self) -> float:
        return statistics.mean(self.correct_per_run())

    @property
    def mean_first_right(self) -> float:
        return statistics.mean(self.first_right_per_run())

    @property
    def mean_f1(self) -> float | None:
        scores = [f for p in self.done if (f := list_f1(p.lists)) is not None]
        return statistics.mean(scores) if scores else None

    @property
    def mean_model_calls(self) -> float:
        return statistics.mean(p.model_calls for p in self.done) if self.done else 0.0

    def second_rounds(self) -> tuple[int, int]:
        """Plays whose agent was asked again, and of those, how many ended right."""
        again = [p for p in self.done if p.model_calls > 1]
        return len(again), sum(p.right for p in again)

    def latency(self, share: float) -> float:
        """What DV11's rule weighs: the agent's own time, every round of it."""
        return candidates.percentile([p.agent_seconds for p in self.done], share)

    def total(self, share: float) -> float:
        return candidates.percentile([p.seconds for p in self.done], share)

    @property
    def errors(self) -> int:
        return len(self.plays) - len(self.done)

    @property
    def mean_tokens(self) -> tuple[float, float]:
        if not self.done:
            return (0.0, 0.0)
        return (
            statistics.mean(p.input_tokens for p in self.done),
            statistics.mean(p.output_tokens for p in self.done),
        )

    @property
    def cost_per_thousand(self) -> float | None:
        """For a thousand questions, every round the agent's model was asked."""
        return cost_per_thousand(self.candidate.model, *self.mean_tokens)

    def failures(self) -> dict[str, list[ListPlay]]:
        failed: dict[str, list[ListPlay]] = {}
        for p in self.plays:
            if not p.right:
                failed.setdefault(p.case_id, []).append(p)
        return failed


async def compare_agents(
    agents: Sequence[tuple[Candidate, BaseChatModel]],
    writer: BaseChatModel,
    cases: Sequence[ListCase],
    db: Database,
    version_id: int,
    runs: int,
    progress: Callable[[str], None] = lambda line: None,
) -> list[AgentResult]:
    """Every case through the list path with each agent, `runs` times, the agents taking
    turns case by case."""
    apps = [list_path(model, writer, db) for _, model in agents]
    results = [AgentResult(candidate, runs) for candidate, _ in agents]
    for run in range(runs):
        for number, item in enumerate(cases, start=1):
            shown = []
            for result, app in zip(results, apps, strict=True):
                played = await play(app, item, version_id, run)
                result.plays.append(played)
                mark = "error" if played.error else "ok" if played.right else "MISS"
                shown.append(
                    f"{result.candidate.label} {mark} x{played.model_calls} {played.seconds:.1f}s"
                )
            progress(
                f"run {run + 1}/{runs} case {number}/{len(cases)} {item.case.case_id}: "
                + "; ".join(shown)
            )
    return results


def agent_rule_pick(results: Sequence[AgentResult]) -> AgentResult:
    """The candidate DV11's rule chooses, by cases whose calls were right and the agent's
    p95; the person decides, having read the failures."""
    return candidates.rule_pick(results, lambda r: r.mean_correct)


# --- write_list ------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteCase:
    """A case and the lists found for it, read by every writer alike."""

    item: ListCase
    message: str
    results: dict[str, Any]
    calls: tuple[Call, ...]  # the calls that found them
    expected: frozenset[str] | None  # the items to show; None when the lists cannot tell


def expected_items(item: ListCase, results: Mapping[str, Any]) -> frozenset[str] | None:
    """What a writer should show of the lists found: the labelled items, if the lists hold
    all of them; else the items of the lists the case asks for. None when they hold
    neither, since no writer could show them."""
    if item.no_lists:
        return frozenset()
    held = {i["key"] for r in results.values() for i in r["items"] + r["uncertain"]}
    if item.gold is not None:
        return item.gold if item.gold <= held else None
    asked = [
        r
        for r in results.values()
        if any(
            _call_matches({"tool": r["tool"], **r["filters"]}, e)
            for accepted in item.accepted
            for e in accepted
        )
    ]
    keys = {i["key"] for r in asked for i in r["items"] + r["uncertain"]}
    return frozenset(keys) or None


async def write_cases(
    cases: Sequence[ListCase],
    app: CompiledStateGraph,
    version_id: int,
    progress: Callable[[str], None] = lambda line: None,
) -> list[WriteCase]:
    """Each case's lists, found once by the list path given."""
    prepared = []
    for item in cases:
        case = item.case
        state = {
            "messages": case_messages(case),
            "index_version": version_id,
            "query_time": item.query_time,
            "chatbot_name": case.chatbot_name or "Wobot",
        }
        final = await app.ainvoke(state)
        results = lists_that_count(final["list_results"])
        written = WriteCase(
            item,
            case.user_input,
            results,
            tuple(counted_calls(results)),
            expected_items(item, results),
        )
        progress(
            f"lists for {case.case_id}: "
            + (
                ", ".join(f"{r['tool']} {r['filters']} {len(r['items'])}" for r in results.values())
                or "none"
            )
        )
        prepared.append(written)
    return prepared


class ModelWriter:
    """The write_list node's prompt and schema on a candidate model, with its usage."""

    def __init__(self, model: BaseChatModel):
        self.model = model.with_structured_output(ListIntro, method="json_schema", include_raw=True)

    async def __call__(self, item: WriteCase) -> tuple[dict[str, Any], int, int]:
        result = await self.model.ainvoke(write_list_prompt(item.message, item.results))
        if result["parsed"] is None:
            raise ValueError(f"the intro did not parse: {result['parsing_error']}")
        usage = result["raw"].usage_metadata or {}
        reply, _ = list_reply(result["parsed"], item.results, writes_chinese(item.message))
        return reply, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


@dataclass(frozen=True)
class Written:
    case_id: str
    run: int
    text: str
    shown: tuple[str, ...]
    seconds: float
    input_tokens: int
    output_tokens: int
    missing: tuple[str, ...] = ()  # items it should show and does not
    unexpected: tuple[str, ...] = ()  # items it shows and should not
    lacking: tuple[str, ...] = ()  # texts it should hold and does not
    error: str | None = None


async def write(writer: ModelWriter, item: WriteCase, run: int) -> Written:
    case_id = item.item.case.case_id
    started = time.perf_counter()
    try:
        reply, tokens_in, tokens_out = await writer(item)
    except Exception as error:  # a failed reply is wrong, reported, and left out of latency
        seconds = time.perf_counter() - started
        return Written(case_id, run, "", (), seconds, 0, 0, error=repr(error))
    seconds = time.perf_counter() - started
    shown = shown_keys(reply)
    expected = item.expected or frozenset()
    return Written(
        case_id,
        run,
        reply["text"],
        tuple(sorted(shown)),
        seconds,
        tokens_in,
        tokens_out,
        missing=tuple(sorted(expected - shown)),
        unexpected=tuple(sorted(shown - expected)),
        lacking=tuple(lacking(reply["text"], item.item.mentions)),
    )


@dataclass
class WriteResult:
    candidate: Candidate
    runs: int
    scored: frozenset[str] = frozenset()  # the cases whose lists can tell what to show
    replies: list[Written] = field(default_factory=list)

    @property
    def done(self) -> list[Written]:
        return [r for r in self.replies if r.error is None]

    def correct(self, r: Written) -> bool:
        return (
            r.case_id in self.scored
            and r.error is None
            and not (r.missing or r.unexpected or r.lacking)
        )

    def correct_per_run(self) -> list[int]:
        return [
            sum(self.correct(r) for r in self.replies if r.run == run) for run in range(self.runs)
        ]

    @property
    def mean_correct(self) -> float:
        return statistics.mean(self.correct_per_run())

    def latency(self, share: float) -> float:
        return candidates.percentile([r.seconds for r in self.done], share)

    @property
    def errors(self) -> int:
        return len(self.replies) - len(self.done)

    @property
    def mean_tokens(self) -> tuple[float, float]:
        if not self.done:
            return (0.0, 0.0)
        return (
            statistics.mean(r.input_tokens for r in self.done),
            statistics.mean(r.output_tokens for r in self.done),
        )

    @property
    def cost_per_thousand(self) -> float | None:
        return cost_per_thousand(self.candidate.model, *self.mean_tokens)

    def failures(self) -> dict[str, list[Written]]:
        failed: dict[str, list[Written]] = {}
        for r in self.replies:
            if r.case_id in self.scored and not self.correct(r):
                failed.setdefault(r.case_id, []).append(r)
        return failed


async def compare_writers(
    writers: Sequence[tuple[Candidate, ModelWriter]],
    cases: Sequence[WriteCase],
    runs: int,
    progress: Callable[[str], None] = lambda line: None,
) -> list[WriteResult]:
    scored = frozenset(c.item.case.case_id for c in cases if c.expected is not None)
    results = [WriteResult(candidate, runs, scored) for candidate, _ in writers]
    for run in range(runs):
        for number, item in enumerate(cases, start=1):
            shown = []
            for result, (candidate, writer) in zip(results, writers, strict=True):
                written = await write(writer, item, run)
                result.replies.append(written)
                if written.error:
                    mark = "error"
                elif item.expected is None:
                    mark = "read"
                else:
                    mark = "ok" if result.correct(written) else "MISS"
                shown.append(f"{candidate.label} {mark} {written.seconds:.1f}s")
            progress(
                f"run {run + 1}/{runs} case {number}/{len(cases)} {item.item.case.case_id}: "
                + "; ".join(shown)
            )
    return results


def writer_rule_pick(results: Sequence[WriteResult]) -> WriteResult:
    return candidates.rule_pick(results, lambda r: r.mean_correct)
