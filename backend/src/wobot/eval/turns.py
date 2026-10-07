"""Plays conversations through the chat graph with a checkpointer, and scores the last
turn (V5): whether a follow-up is understood from what the graph itself kept.

Each play is a thread of its own. The user's earlier messages are sent one turn at a
time, and the graph answers and keeps each, as in wobot-chat; then the last message is
played and timed node by node. The graph is the configured one, with every model as the
settings choose: no candidates are compared here.

The saver is LangGraph's MemorySaver, so evaluation leaves no threads in the database:
what is measured is what the graph keeps and reads back, not where it is kept. Keeping
threads in Postgres, and taking one up after a restart, is the checkpointer tests' part.
"""

import statistics
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from wobot.eval import candidates
from wobot.eval.candidates import lacking
from wobot.eval.dataset import Case, Dataset
from wobot.eval.lists import Call, calls_right, counted_calls

DATASET = "turns-v1"


@dataclass(frozen=True)
class TurnCase:
    case: Case
    query_time: datetime
    routes: tuple[str, ...]  # every acceptable path of the last turn
    accepted: tuple[tuple[Call, ...], ...] | None  # list calls, when the path lists
    question_mentions: tuple[Any, ...]
    mentions: tuple[Any, ...]


def turn_cases(datasets: Sequence[Dataset], splits: Sequence[str]) -> list[TurnCase]:
    found = []
    for dataset in datasets:
        query_time = datetime.fromisoformat(dataset.query_time)
        for case in candidates.cases_of([dataset], splits, "turns"):
            check = case.check
            accepted = check.get("calls")
            found.append(
                TurnCase(
                    case,
                    query_time,
                    tuple(check["route"]),
                    None if accepted is None else tuple(tuple(a) for a in accepted),
                    tuple(check.get("question_mentions", ())),
                    tuple(check.get("mentions", ())),
                )
            )
    return found


@dataclass(frozen=True)
class Played:
    case_id: str
    run: int
    earlier: tuple[str, ...]  # the graph's replies to the earlier messages
    route: str | None
    question: str  # as rewrite_query read the last message; empty off the knowledge path
    calls: tuple[Call, ...]  # the list calls that counted
    text: str
    seconds: float  # the last turn, from classify to its reply
    nodes: dict[str, float]  # the last turn's seconds in each node
    route_right: bool
    calls_right: bool | None  # None when the case names no calls
    question_lacking: tuple[str, ...]
    lacking: tuple[str, ...]
    error: str | None = None

    @property
    def right(self) -> bool:
        return (
            self.error is None
            and self.route_right
            and self.calls_right is not False
            and not self.question_lacking
            and not self.lacking
        )


def _turn(item: TurnCase, message: str, version_id: int) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(message)],
        "index_version": version_id,
        "query_time": item.query_time,
        "chatbot_name": item.case.chatbot_name or "Wobot",
    }


async def play(app: CompiledStateGraph, item: TurnCase, version_id: int, run: int) -> Played:
    """One conversation in a thread of its own: the earlier messages answered and kept,
    then the last one, timed."""
    case = item.case
    config = {"configurable": {"thread_id": f"eval-{case.case_id}-{uuid.uuid4()}"}}
    earlier: list[str] = []
    spent: dict[str, float] = defaultdict(float)
    final: dict[str, Any] = {}
    error = None
    started = time.perf_counter()
    try:
        for message in case.turns:
            state = await app.ainvoke(_turn(item, message, version_id), config)
            earlier.append(state["reply"].get("text", ""))
        started = last = time.perf_counter()
        turn = _turn(item, case.user_input, version_id)
        async for mode, chunk in app.astream(turn, config, stream_mode=["updates", "values"]):
            if mode == "values":
                final = chunk
                continue
            now = time.perf_counter()
            for node in chunk:
                spent[node] += now - last
            last = now
    except Exception as failure:  # a failed play is wrong, reported, and left out of latency
        error = repr(failure)
    seconds = time.perf_counter() - started
    reply = final.get("reply") or {}
    if error is None and reply.get("retryable"):
        error = "the path reported a failure"
    route = final.get("route")
    calls = tuple(counted_calls(final.get("list_results") or {}))
    text = reply.get("text", "")
    return Played(
        case_id=case.case_id,
        run=run,
        earlier=tuple(earlier),
        route=route,
        question=final.get("question") or "",
        calls=calls,
        text=text,
        seconds=seconds,
        nodes=dict(spent),
        route_right=route in item.routes,
        calls_right=None if item.accepted is None else calls_right(calls, item.accepted),
        question_lacking=tuple(lacking(final.get("question") or "", item.question_mentions)),
        lacking=tuple(lacking(text, item.mentions)),
        error=error,
    )


@dataclass
class TurnsResult:
    runs: int
    plays: list[Played] = field(default_factory=list)

    @property
    def done(self) -> list[Played]:
        return [p for p in self.plays if p.error is None]

    def correct_per_run(self) -> list[int]:
        return [sum(p.right for p in self.plays if p.run == run) for run in range(self.runs)]

    @property
    def mean_correct(self) -> float:
        return statistics.mean(self.correct_per_run())

    def latency(self, share: float, route: str | None = None) -> float:
        """The last turn's seconds, of every play or of those that took one path."""
        return candidates.percentile(
            [p.seconds for p in self.done if route is None or p.route == route], share
        )

    def node_latency(self, node: str, share: float) -> float:
        return candidates.percentile([p.nodes[node] for p in self.done if node in p.nodes], share)

    @property
    def errors(self) -> int:
        return len(self.plays) - len(self.done)

    def failures(self) -> dict[str, list[Played]]:
        failed: dict[str, list[Played]] = {}
        for p in self.plays:
            if not p.right:
                failed.setdefault(p.case_id, []).append(p)
        return failed


async def play_all(
    app: CompiledStateGraph,
    cases: Sequence[TurnCase],
    version_id: int,
    runs: int,
    progress: Callable[[str], None] = lambda line: None,
) -> TurnsResult:
    result = TurnsResult(runs)
    for run in range(runs):
        for number, item in enumerate(cases, start=1):
            played = await play(app, item, version_id, run)
            result.plays.append(played)
            mark = "error" if played.error else "ok" if played.right else "MISS"
            progress(
                f"run {run + 1}/{runs} case {number}/{len(cases)} {item.case.case_id}: "
                f"{mark} {played.route} {played.seconds:.1f}s"
            )
    return result
