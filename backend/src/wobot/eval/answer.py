"""Compares models for answer: whether the reply holds what the sources give, how soon its
first words come and how long it takes, and at what cost (DV11).

Every candidate reads the same evidence: each case's message is rewritten and searched
once, as the knowledge path does, before any reply is asked for, so only the answer
differs. Replies are streamed, as the app will show them. A reply that must hold texts is
scored by them; one the sources cannot answer is read by a person until the judge of V8.
"""

import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel

from wobot.agent.answer import answer_prompt
from wobot.agent.retrieval import records_named, search_knowledge
from wobot.eval import candidates
from wobot.eval.candidates import Candidate, case_messages, cost_per_thousand, lacking
from wobot.eval.dataset import Case, Dataset
from wobot.eval.rewrite import Rewriter
from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.repository import Database

DATASET = "knowledge-v1"


@dataclass(frozen=True)
class AnswerCase:
    case: Case
    question: str  # as the rewrite read the message
    evidence: dict[str, Any]  # what the search found, read by every candidate alike
    mentions: tuple[Any, ...]  # texts the reply must hold; a list among them, alternatives
    no_info: bool  # the sources do not answer it: a person reads the reply

    @property
    def scored(self) -> bool:
        return bool(self.mentions)


# The search: the queries and the name in, the evidence out.
Lookup = Callable[[list[str], str | None], Awaitable[dict[str, Any]]]


def knowledge_lookup(db: Database, embedder: Embedder, version_id: int) -> Lookup:
    """What the retrieve node finds for a plan, as the evidence the answer reads."""

    async def lookup(queries: list[str], name: str | None) -> dict[str, Any]:
        passages = await search_knowledge(db, embedder, version_id, queries)
        records = await records_named(db, version_id, name) if name else []
        return {"passages": passages, "records": records}

    return lookup


def scored_cases(datasets: Sequence[Dataset], splits: Sequence[str]) -> list[Case]:
    """The knowledge cases whose reply is checked: by its texts, or by a person."""
    return [
        case
        for dataset in datasets
        for case in dataset.cases
        if case.split in splits
        and case.check
        and case.check["kind"] == "knowledge"
        and (case.check.get("mentions") or case.check.get("no_info"))
    ]


async def answer_cases(
    cases: Sequence[Case],
    rewriter: Rewriter,
    lookup: Lookup,
    progress: Callable[[str], None] = lambda line: None,
) -> list[AnswerCase]:
    """Each case with the question and evidence the knowledge path would give the answer."""
    prepared = []
    for case in cases:
        plan = await rewriter(case_messages(case))
        evidence = await lookup(plan.queries, plan.name)
        progress(
            f"evidence for {case.case_id}: {len(evidence['passages'])} passages, "
            f"{len(evidence['records'])} records"
        )
        prepared.append(
            AnswerCase(
                case,
                plan.question,
                evidence,
                tuple(case.check.get("mentions", ())),
                bool(case.check.get("no_info")),
            )
        )
    return prepared


@dataclass(frozen=True)
class Reply:
    case_id: str
    run: int
    text: str
    first_token_seconds: float | None  # None when no words came
    seconds: float
    input_tokens: int
    output_tokens: int
    lacking: tuple[str, ...] = ()  # texts the reply should hold and does not
    error: str | None = None


async def reply(model: BaseChatModel, item: AnswerCase, run: int) -> Reply:
    case = item.case
    prompt = answer_prompt(
        case.user_input, item.question, item.evidence, case.chatbot_name or "Wobot"
    )
    started = time.perf_counter()
    first: float | None = None
    whole = None
    try:
        async for chunk in model.astream(prompt):
            if first is None and chunk.text:
                first = time.perf_counter() - started
            whole = chunk if whole is None else whole + chunk
    except Exception as error:  # a failed reply is wrong, reported, and left out of latency
        seconds = time.perf_counter() - started
        missing = tuple(lacking("", item.mentions))
        return Reply(case.case_id, run, "", first, seconds, 0, 0, missing, repr(error))
    seconds = time.perf_counter() - started
    text = whole.text if whole is not None else ""
    usage = (whole.usage_metadata if whole is not None else None) or {}
    return Reply(
        case.case_id,
        run,
        text,
        first,
        seconds,
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        tuple(lacking(text, item.mentions)),
    )


@dataclass
class AnswerResult:
    candidate: Candidate
    runs: int
    scored: frozenset[str] = frozenset()  # the cases checked by their texts
    replies: list[Reply] = field(default_factory=list)

    @property
    def done(self) -> list[Reply]:
        return [r for r in self.replies if r.error is None]

    def correct(self, r: Reply) -> bool:
        return r.case_id in self.scored and r.error is None and not r.lacking

    def correct_per_run(self) -> list[int]:
        return [
            sum(self.correct(r) for r in self.replies if r.run == run) for run in range(self.runs)
        ]

    @property
    def mean_correct(self) -> float:
        return statistics.mean(self.correct_per_run())

    def first_token(self, share: float) -> float:
        return candidates.percentile(
            [r.first_token_seconds for r in self.done if r.first_token_seconds], share
        )

    def total(self, share: float) -> float:
        return candidates.percentile([r.seconds for r in self.done], share)

    def latency(self, share: float) -> float:
        """What DV11's rule weighs: the whole reply."""
        return self.total(share)

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

    def failures(self) -> dict[str, list[Reply]]:
        """The scored replies that missed, by case."""
        failed: dict[str, list[Reply]] = {}
        for r in self.replies:
            if r.case_id in self.scored and not self.correct(r):
                failed.setdefault(r.case_id, []).append(r)
        return failed


async def compare(
    models: Sequence[tuple[Candidate, BaseChatModel]],
    cases: Sequence[AnswerCase],
    runs: int,
    progress: Callable[[str], None] = lambda line: None,
) -> list[AnswerResult]:
    """Every case through every model, `runs` times, the models taking turns case by case;
    `progress` hears of each case."""
    scored = frozenset(c.case.case_id for c in cases if c.scored)
    results = [AnswerResult(candidate, runs, scored) for candidate, _ in models]
    for run in range(runs):
        for number, item in enumerate(cases, start=1):
            shown = []
            for result, (candidate, model) in zip(results, models, strict=True):
                answered = await reply(model, item, run)
                result.replies.append(answered)
                if answered.error:
                    mark = "error"
                elif not item.scored:
                    mark = "read"
                else:
                    mark = "ok" if not answered.lacking else "MISS"
                shown.append(f"{candidate.label} {mark} {answered.seconds:.1f}s")
            progress(
                f"run {run + 1}/{runs} case {number}/{len(cases)} {item.case.case_id}: "
                + "; ".join(shown)
            )
    return results


def rule_pick(results: Sequence[AnswerResult]) -> AnswerResult:
    """The candidate DV11's rule chooses, by replies holding their texts and the whole
    reply's p95; the person decides, having read the replies."""
    return candidates.rule_pick(results, lambda r: r.mean_correct)
