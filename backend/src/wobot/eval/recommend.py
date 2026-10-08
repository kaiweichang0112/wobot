"""Compares models for rec_agent (DV11, DEC-066) on recommendation-v2: what the turn did with
the need, whether every product it showed fits and none it may never show, how many it
showed, and at what latency and cost.

Each case is played through the chat graph's recommendation path with the candidate as
rec_agent: the agent asks, searches, judges and recommends, and code checks and shows, as
in the app. Every message is routed there, so the router's mistakes are not counted
against the agent; a case that checks the route itself is the router's, and left out.
Each play keeps its flow, node by node, with seconds and what each node did.
"""

import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult
from langgraph.graph.state import CompiledStateGraph

from wobot.agent.cli import describe
from wobot.agent.graph import Models, build_graph
from wobot.agent.rewrite import writes_chinese
from wobot.agent.route import Routed
from wobot.eval import candidates
from wobot.eval.candidates import Candidate, case_messages, cost_per_thousand
from wobot.eval.dataset import Case, Dataset
from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.repository import Database

DATASET = "recommendation-v2"

# The nodes a recommendation turn passes after classify.
NODES = ("rec_agent", "rec_tools")


@dataclass(frozen=True)
class RecCase:
    case: Case
    query_time: datetime
    actions: tuple[str, ...]  # every acceptable outcome: recommend or replied
    recommends: frozenset[str] | None  # every product shown must be one of these
    never: frozenset[str]
    prefers: str | None  # shown first whenever it is shown at all (DEC-065)
    count: int | str | None  # how many are shown; "all" is two or more
    searches: bool | None  # False: the turn must not search
    language: str | None  # the reply's, when not Chinese


def rec_cases(datasets: Sequence[Dataset], splits: Sequence[str]) -> list[RecCase]:
    """The cases rec_agent answers; those checking the route are left to the router."""
    found = []
    for dataset in datasets:
        query_time = datetime.fromisoformat(dataset.query_time)
        for case in candidates.cases_of([dataset], splits, "recommendation"):
            check = case.check
            if "route" in check:
                continue
            fits = check.get("recommends")
            found.append(
                RecCase(
                    case,
                    query_time,
                    tuple(check["action"]),
                    None if fits is None else frozenset(fits),
                    frozenset(check.get("never", ())),
                    check.get("prefers"),
                    check.get("count"),
                    check.get("searches"),
                    check.get("language"),
                )
            )
    return found


def problems(
    item: RecCase, action: str, shown: Sequence[str], searches: int, text: str
) -> list[str]:
    """What the turn got wrong, in words; none when it is right."""
    found = []
    if action not in item.actions:
        found.append(f"{action}, not {' or '.join(item.actions)}")
    if action == "recommend":
        found += [f"shows {name}, which may never be shown" for name in shown if name in item.never]
        if item.recommends is not None:
            found += [
                f"shows {name}, not labelled to fit"
                for name in shown
                if name not in item.recommends and name not in item.never
            ]
        if item.count == "all" and len(shown) < 2:
            found.append(f"shows {len(shown)}, not all")
        elif isinstance(item.count, int) and len(shown) != item.count:
            found.append(f"shows {len(shown)}, not {item.count}")
    if item.searches is False and searches:
        found.append("searched")
    if item.language == "en" and writes_chinese(text.split("\n", 1)[0]):
        found.append("not in English")
    return found


def preferred(item: RecCase, action: str, shown: Sequence[str]) -> bool | None:
    """Whether the preferred product came first; None where the case prefers none or the
    turn showed no product."""
    if item.prefers is None or action != "recommend" or not shown:
        return None
    return shown[0] == item.prefers


class ModelUsage(BaseCallbackHandler):
    """Counts the model calls of a run and their tokens."""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.calls += 1
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if usage:
                    self.input_tokens += usage.get("input_tokens", 0)
                    self.output_tokens += usage.get("output_tokens", 0)


async def always_recommend(messages: Any, pending_question: Any) -> Routed:
    """A router that sends every message down the recommendation path."""
    return Routed("recommend", None)


def rec_path(agent: BaseChatModel, db: Database, embedder: Embedder) -> CompiledStateGraph:
    """The chat graph with every message routed to recommendations. Its other models are
    never called on this path, so the agent stands in for them."""
    models = Models(
        router=always_recommend,
        chat=agent,
        rewrite=agent,
        answer=agent,
        list_agent=agent,
        write_list=agent,
        rec_agent=agent,
    )
    return build_graph(models, db, embedder)


@dataclass(frozen=True)
class Step:
    node: str
    seconds: float
    note: str  # what the node did, as wobot-chat shows it


@dataclass(frozen=True)
class RecPlay:
    case_id: str
    run: int
    action: str  # recommend, replied or unverified
    shown: tuple[str, ...]  # the products shown, in order
    searches: int
    problems: tuple[str, ...]
    preferred: bool | None
    text: str
    flow: tuple[Step, ...]  # every node after classify, in order
    seconds: float  # from the first node after classify to the reply
    nodes: dict[str, float]  # seconds in each node, every visit summed
    model_calls: int
    input_tokens: int
    output_tokens: int
    error: str | None = None

    @property
    def right(self) -> bool:
        return self.error is None and not self.problems


async def play(app: CompiledStateGraph, item: RecCase, version_id: int, run: int) -> RecPlay:
    """One case through the recommendation path, timed node by node."""
    case = item.case
    state = {
        "messages": case_messages(case),
        "pending_question": case.pending_question,
        "index_version": version_id,
        "query_time": item.query_time,
        "chatbot_name": case.chatbot_name or "Wobot",
    }
    usage = ModelUsage()
    flow: list[Step] = []
    final: dict[str, Any] = {}
    error = None
    last = time.perf_counter()
    try:
        async for mode, chunk in app.astream(
            state, {"callbacks": [usage]}, stream_mode=["updates", "values"]
        ):
            if mode == "values":
                final = chunk
                continue
            now = time.perf_counter()
            for node, update in chunk.items():
                if node != "classify":
                    flow.append(Step(node, now - last, describe(node, update or {})))
            last = now
    except Exception as failure:  # a failed play is wrong, reported, and left out of latency
        error = repr(failure)
    reply = final.get("reply") or {}
    if error is None and reply.get("retryable"):
        error = "the path reported a failure"
    action = reply.get("action", "")
    shown = tuple(p["name"] for p in reply.get("products", []))
    asked = [m for m in final.get("rec_messages", []) if isinstance(m, AIMessage)]
    searches = sum(c["name"] == "search_products" for m in asked for c in m.tool_calls)
    text = reply.get("text", "")
    nodes: dict[str, float] = defaultdict(float)
    for step in flow:
        nodes[step.node] += step.seconds
    return RecPlay(
        case_id=case.case_id,
        run=run,
        action=action,
        shown=shown,
        searches=searches,
        problems=() if error else tuple(problems(item, action, shown, searches, text)),
        preferred=None if error else preferred(item, action, shown),
        text=text,
        flow=tuple(flow),
        seconds=sum(step.seconds for step in flow),
        nodes=dict(nodes),
        model_calls=usage.calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        error=error,
    )


@dataclass
class RecResult:
    candidate: Candidate
    runs: int
    plays: list[RecPlay] = field(default_factory=list)

    @property
    def done(self) -> list[RecPlay]:
        return [p for p in self.plays if p.error is None]

    def correct_per_run(self) -> list[int]:
        return [sum(p.right for p in self.plays if p.run == run) for run in range(self.runs)]

    @property
    def mean_correct(self) -> float:
        return statistics.mean(self.correct_per_run())

    @property
    def never_shown(self) -> int:
        """Plays that showed a product the case says may never be shown: the worst miss."""
        return sum(
            any("may never be shown" in problem for problem in p.problems) for p in self.done
        )

    def preferred_shown(self) -> tuple[int, int]:
        """Plays where the preferred product could come first, and how many it did."""
        judged = [p.preferred for p in self.done if p.preferred is not None]
        return sum(judged), len(judged)

    def latency(self, share: float) -> float:
        """What DV11's rule weighs: the whole path after classify, every round."""
        return candidates.percentile([p.seconds for p in self.done], share)

    def node_latency(self, node: str, share: float) -> float:
        return candidates.percentile([p.nodes[node] for p in self.done if node in p.nodes], share)

    @property
    def mean_model_calls(self) -> float:
        return statistics.mean(p.model_calls for p in self.done) if self.done else 0.0

    @property
    def mean_searches(self) -> float:
        return statistics.mean(p.searches for p in self.done) if self.done else 0.0

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
        """For a thousand turns, every call of the agent's model."""
        return cost_per_thousand(self.candidate.model, *self.mean_tokens)

    def failures(self) -> dict[str, list[RecPlay]]:
        failed: dict[str, list[RecPlay]] = {}
        for p in self.plays:
            if not p.right:
                failed.setdefault(p.case_id, []).append(p)
        return failed


async def compare(
    agents: Sequence[tuple[Candidate, BaseChatModel]],
    cases: Sequence[RecCase],
    db: Database,
    embedder: Embedder,
    version_id: int,
    runs: int,
    progress: Callable[[str], None] = lambda line: None,
) -> list[RecResult]:
    """Every case through the recommendation path with each agent, `runs` times, the
    agents taking turns case by case."""
    apps = [rec_path(model, db, embedder) for _, model in agents]
    results = [RecResult(candidate, runs) for candidate, _ in agents]
    for run in range(runs):
        for number, item in enumerate(cases, start=1):
            shown = []
            for result, app in zip(results, apps, strict=True):
                played = await play(app, item, version_id, run)
                result.plays.append(played)
                mark = "error" if played.error else "ok" if played.right else "MISS"
                shown.append(
                    f"{result.candidate.label} {mark} {played.action or '-'} "
                    f"x{played.model_calls} {played.seconds:.1f}s"
                )
            progress(
                f"run {run + 1}/{runs} case {number}/{len(cases)} {item.case.case_id}: "
                + "; ".join(shown)
            )
    return results


def rule_pick(results: Sequence[RecResult]) -> RecResult:
    """The candidate DV11's rule chooses, by cases right and the path's p95; the person
    decides, having read the failures and the flows."""
    return candidates.rule_pick(results, lambda r: r.mean_correct)
