"""Compares models for the chat path: how soon the first words come, how long a reply
takes, what it costs, and the replies themselves for a person to read (DV11).

No score is computed: small talk has no labelled answer, so the person judges the
replies until the judge of V8 is calibrated. The cases are the route dataset's chat
cases, sent with the chat node's own prompt; each reply is streamed, as the app will.
"""

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel

from wobot.agent.chat import chat_prompt
from wobot.eval.candidates import Candidate, case_messages, cost_per_thousand, percentile
from wobot.eval.dataset import Case, Dataset

# Candidates within this share of the fastest p95 count as fast; the cheapest of them
# is the rule's pick. The person decides, having read the replies.
CLOSE_LATENCY = 0.10


@dataclass(frozen=True)
class Reply:
    case_id: str
    run: int
    text: str
    first_token_seconds: float | None  # None when no words came
    seconds: float
    input_tokens: int
    output_tokens: int
    error: str | None = None


@dataclass
class ChatResult:
    candidate: Candidate
    replies: list[Reply] = field(default_factory=list)

    @property
    def done(self) -> list[Reply]:
        return [r for r in self.replies if r.error is None]

    def first_token(self, share: float) -> float:
        return percentile(
            [r.first_token_seconds for r in self.done if r.first_token_seconds], share
        )

    def total(self, share: float) -> float:
        return percentile([r.seconds for r in self.done], share)

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


def chat_cases(datasets: Sequence[Dataset], splits: Sequence[str]) -> list[Case]:
    """The route cases that may be answered as chat."""
    return [
        case
        for dataset in datasets
        for case in dataset.cases
        if case.split in splits
        and case.check
        and case.check["kind"] == "route"
        and "chat" in case.check["expect"]
    ]


async def reply(model: BaseChatModel, case: Case, run: int) -> Reply:
    prompt = chat_prompt(case.chatbot_name or "Wobot", case_messages(case))
    started = time.perf_counter()
    first: float | None = None
    whole = None
    try:
        async for chunk in model.astream(prompt):
            if first is None and chunk.text:
                first = time.perf_counter() - started
            whole = chunk if whole is None else whole + chunk
    except Exception as error:  # reported, and left out of the latencies
        seconds = time.perf_counter() - started
        return Reply(case.case_id, run, "", first, seconds, 0, 0, repr(error))
    seconds = time.perf_counter() - started
    usage = (whole.usage_metadata if whole is not None else None) or {}
    return Reply(
        case.case_id,
        run,
        whole.text if whole is not None else "",
        first,
        seconds,
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
    )


async def compare(
    models: Sequence[tuple[Candidate, BaseChatModel]],
    cases: Sequence[Case],
    runs: int,
    progress: Callable[[str], None] = lambda line: None,
) -> list[ChatResult]:
    """Every case through every model, `runs` times, the models taking turns per case."""
    results = [ChatResult(candidate) for candidate, _ in models]
    for run in range(runs):
        for number, case in enumerate(cases, start=1):
            shown = []
            for result, (candidate, model) in zip(results, models, strict=True):
                answered = await reply(model, case, run)
                result.replies.append(answered)
                shown.append(
                    f"{candidate.label} "
                    + ("error" if answered.error else f"{answered.seconds:.1f}s")
                )
            progress(
                f"run {run + 1}/{runs} case {number}/{len(cases)} {case.case_id}: "
                + "; ".join(shown)
            )
    return results


def rule_pick(results: Sequence[ChatResult]) -> ChatResult:
    """The fastest at p95, or the cheapest of those within a tenth of it: the user's own
    criteria for this node, cheap and fast."""
    fastest = min(r.total(0.95) for r in results)
    close = [r for r in results if r.total(0.95) <= fastest * (1 + CLOSE_LATENCY)]
    return min(close, key=lambda r: (r.cost_per_thousand is None, r.cost_per_thousand or 0))
