"""Compares routers on the route dataset: how often each picks an expected path, how
fast, and at what cost (DV11).

Each candidate plays every case several times, the candidates taking turns case by case
so that a slow minute of the network falls on all of them alike. Latency is the router's
own call: the classify node does nothing else.
"""

import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from wobot.agent.route import Router
from wobot.eval.dataset import Case, Dataset

# US dollars per million tokens, input and output, from spec 07's check of 2026-10-01.
# Re-check the pricing pages before a comparison that decides on cost. Cached input is
# left out: a router's prompt is too short for the provider to cache.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.5": (5.00, 30.00),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-6-luna": (0.10, 0.50),
    "jev-latest": (0.042, 0.0),  # output is not charged
}

# DV11's rule: keep the candidates within one case of the best, take the fastest at p95,
# and among those within a tenth of its p95 the cheapest.
CASES_BEHIND_BEST = 1
CLOSE_LATENCY = 0.10


@dataclass(frozen=True)
class Candidate:
    model: str
    effort: str  # "-" for Jev, which has none

    @property
    def label(self) -> str:
        return self.model if self.effort == "-" else f"{self.model}:{self.effort}"


def parse_candidate(spec: str) -> Candidate:
    """`model:effort`, or a Jev model alone."""
    model, _, effort = spec.partition(":")
    if model.startswith("jev-"):
        return Candidate(model, "-")
    if not effort:
        raise ValueError(f"{spec}: give the effort, as in gpt-6-luna:none")
    return Candidate(model, effort)


@dataclass(frozen=True)
class Played:
    case_id: str
    run: int
    expected: tuple[str, ...]
    got: str | None  # None when the call failed
    confidence: float | None
    seconds: float
    input_tokens: int
    output_tokens: int
    error: str | None = None

    @property
    def correct(self) -> bool:
        return self.got in self.expected


@dataclass
class CandidateResult:
    candidate: Candidate
    runs: int
    plays: list[Played] = field(default_factory=list)

    def correct_per_run(self) -> list[int]:
        return [sum(p.correct for p in self.plays if p.run == run) for run in range(self.runs)]

    @property
    def cases(self) -> int:
        return len({p.case_id for p in self.plays})

    @property
    def mean_correct(self) -> float:
        return statistics.mean(self.correct_per_run())

    @property
    def accuracy(self) -> float:
        return self.mean_correct / self.cases

    def latency(self, share: float) -> float:
        return percentile([p.seconds for p in self.plays if p.error is None], share)

    @property
    def errors(self) -> int:
        return sum(p.error is not None for p in self.plays)

    @property
    def mean_tokens(self) -> tuple[float, float]:
        done = [p for p in self.plays if p.error is None]
        if not done:
            return (0.0, 0.0)
        return (
            statistics.mean(p.input_tokens for p in done),
            statistics.mean(p.output_tokens for p in done),
        )

    @property
    def cost_per_thousand(self) -> float | None:
        """Dollars for a thousand messages; None for a model without a known price."""
        if self.candidate.model not in PRICES:
            return None
        price_in, price_out = PRICES[self.candidate.model]
        tokens_in, tokens_out = self.mean_tokens
        return 1000 * (tokens_in * price_in + tokens_out * price_out) / 1_000_000

    def misses(self) -> dict[str, list[Played]]:
        """The plays that missed, by case."""
        missed: dict[str, list[Played]] = {}
        for play in self.plays:
            if not play.correct:
                missed.setdefault(play.case_id, []).append(play)
        return missed


def percentile(values: Sequence[float], share: float) -> float:
    """The nearest-rank percentile: the value that `share` of them do not exceed."""
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[max(math.ceil(share * len(ordered)) - 1, 0)]


def route_cases(datasets: Sequence[Dataset], splits: Sequence[str]) -> list[Case]:
    return [
        case
        for dataset in datasets
        for case in dataset.cases
        if case.split in splits and case.check and case.check["kind"] == "route"
    ]


def case_messages(case: Case) -> list[BaseMessage]:
    """The scripted conversation, then the case's own message."""
    messages: list[BaseMessage] = []
    for turn in case.history:
        messages += [HumanMessage(turn["user"]), AIMessage(turn["answer"])]
    return [*messages, HumanMessage(case.user_input)]


async def play(router: Router, case: Case, run: int) -> Played:
    expected = tuple(case.check["expect"])
    started = time.perf_counter()
    try:
        routed = await router(case_messages(case), case.pending_question)
    except Exception as error:  # a failed call counts as a miss, and is reported
        seconds = time.perf_counter() - started
        return Played(case.case_id, run, expected, None, None, seconds, 0, 0, repr(error))
    seconds = time.perf_counter() - started
    return Played(
        case.case_id,
        run,
        expected,
        routed.route,
        routed.confidence,
        seconds,
        routed.input_tokens,
        routed.output_tokens,
    )


async def compare(
    routers: Sequence[tuple[Candidate, Router]],
    cases: Sequence[Case],
    runs: int,
    progress: Callable[[str], None] = lambda line: None,
) -> list[CandidateResult]:
    """Every case through every router, `runs` times; `progress` hears of each case."""
    results = [CandidateResult(candidate, runs) for candidate, _ in routers]
    for run in range(runs):
        for number, case in enumerate(cases, start=1):
            shown = []
            for result, (candidate, router) in zip(results, routers, strict=True):
                played = await play(router, case, run)
                result.plays.append(played)
                mark = "ok" if played.correct else f"MISS {played.got or 'error'}"
                shown.append(f"{candidate.label} {mark} {played.seconds:.1f}s")
            progress(
                f"run {run + 1}/{runs} case {number}/{len(cases)} {case.case_id}: "
                + "; ".join(shown)
            )
    return results


def rule_pick(results: Sequence[CandidateResult]) -> CandidateResult:
    """The candidate DV11's rule chooses; the person decides, with the numbers."""
    best = max(r.mean_correct for r in results)
    accurate = [r for r in results if r.mean_correct >= best - CASES_BEHIND_BEST]
    fastest = min(r.latency(0.95) for r in accurate)
    close = [r for r in accurate if r.latency(0.95) <= fastest * (1 + CLOSE_LATENCY)]
    return min(close, key=lambda r: (r.cost_per_thousand is None, r.cost_per_thousand or 0))
