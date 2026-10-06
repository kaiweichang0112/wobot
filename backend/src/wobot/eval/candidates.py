"""What the chat graph's model comparisons share: the candidates, their prices, how a
case is played as a conversation, and latency percentiles (DV11)."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

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
