"""What the chat graph's model comparisons share: the candidates, their prices, how a
case is played as a conversation, and latency percentiles (DV11)."""

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from wobot.eval.dataset import Case, Dataset

# US dollars per million tokens, input and output, from spec 07's check of 2026-10-01.
# Re-check the pricing pages before a comparison that decides on cost. Cached input is
# left out: these prompts are too short for the provider to cache.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.5": (5.00, 30.00),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-4o": (2.50, 10.00),  # the model page, checked 2026-10-06
    "jev-latest": (0.042, 0.0),  # output is not charged
}


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


def cost_per_thousand(model: str, tokens_in: float, tokens_out: float) -> float | None:
    """Dollars for a thousand calls of these mean sizes; None for an unknown price."""
    if model not in PRICES:
        return None
    price_in, price_out = PRICES[model]
    return 1000 * (tokens_in * price_in + tokens_out * price_out) / 1_000_000


def percentile(values: Sequence[float], share: float) -> float:
    """The nearest-rank percentile: the value that `share` of them do not exceed."""
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[max(math.ceil(share * len(ordered)) - 1, 0)]


def cases_of(datasets: Sequence[Dataset], splits: Sequence[str], kind: str) -> list[Case]:
    return [
        case
        for dataset in datasets
        for case in dataset.cases
        if case.split in splits and case.check and case.check["kind"] == kind
    ]


def case_messages(case: Case) -> list[BaseMessage]:
    """The scripted conversation, then the case's own message."""
    messages: list[BaseMessage] = []
    for turn in case.history:
        messages += [HumanMessage(turn["user"]), AIMessage(turn["answer"])]
    return [*messages, HumanMessage(case.user_input)]


# DV11's rule: keep the candidates within one case of the best, take the fastest at p95,
# and among those within a tenth of its p95 the cheapest.
CASES_BEHIND_BEST = 1
CLOSE_LATENCY = 0.10


class Measured(Protocol):
    def latency(self, share: float) -> float: ...

    @property
    def cost_per_thousand(self) -> float | None: ...


def rule_pick[M: Measured](results: Sequence[M], correct: Callable[[M], float]) -> M:
    """The candidate DV11's rule chooses, by its mean of cases right per run; the person
    decides, with the numbers."""
    best = max(correct(r) for r in results)
    accurate = [r for r in results if correct(r) >= best - CASES_BEHIND_BEST]
    fastest = min(r.latency(0.95) for r in accurate)
    close = [r for r in accurate if r.latency(0.95) <= fastest * (1 + CLOSE_LATENCY)]
    return min(close, key=lambda r: (r.cost_per_thousand is None, r.cost_per_thousand or 0))


# Spaces and dashes of every width: a reply may write 03-455-5726 as 03 4555726.
_IGNORED = re.compile(r"[\s\-\u2010-\u2015\uff0d]")


def plain(text: str) -> str:
    """Text as mentions are matched: ignoring case, spaces and dashes."""
    return _IGNORED.sub("", text).casefold()


def lacking(text: str, mentions: Sequence[str | Sequence[str]]) -> list[str]:
    """The mentions the text does not hold; a list among them is held by any one of its
    texts, and is shown as its texts joined by a slash."""
    held = plain(text)
    missing = []
    for mention in mentions:
        options = [mention] if isinstance(mention, str) else list(mention)
        if not any(plain(option) in held for option in options):
            missing.append(" / ".join(options))
    return missing
