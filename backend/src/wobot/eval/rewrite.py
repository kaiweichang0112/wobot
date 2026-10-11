"""Compares models for rewrite_query: whether the search its plan leads to finds what
answers the question, whether the question it writes says what the message refers to,
how fast and at what cost (DV11).

Each plan is searched as the retrieve node searches it, on one index version, so a
candidate is scored by what its words find rather than by the words. A case passes when
the search finds every item labelled as answering it, and the plan holds the texts and
the name the case asks for. The message searched as it is, as phase A searched it, is the
baseline every candidate has to beat.
"""

import statistics
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from wobot.agent.retrieval import evidence_keys, records_named, search_knowledge
from wobot.agent.rewrite import SearchPlan, read_plan, rewrite_prompt
from wobot.eval import candidates, gold
from wobot.eval.candidates import Candidate, case_messages, cost_per_thousand, lacking, plain
from wobot.eval.corpus import Corpus
from wobot.eval.dataset import Case, Dataset
from wobot.eval.runner import case_refs
from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.repository import Database

# The datasets whose cases a rewrite is scored on: the questions phase A's search was
# measured on, and the knowledge path's own.
DATASETS = ("seed-v1", "items-v1", "knowledge-v1")
BASELINE = Candidate("as-is", "-")


@dataclass(frozen=True)
class RewriteCase:
    dataset: str
    case: Case
    relevant: frozenset[str]  # the labelled items; one that matched no record stays, unfound
    question_mentions: tuple[str, ...]
    name: str | None
    items: Mapping[str, str] = field(default_factory=dict)  # record key -> item it counts as


def rewrite_cases(
    datasets: Sequence[Dataset], splits: Sequence[str], corpus: Corpus, gold_dir: Path
) -> tuple[list[RewriteCase], dict[str, str]]:
    """The cases with something to score a rewrite on, and the hash of each gold file."""
    found: list[RewriteCase] = []
    gold_files: dict[str, str] = {}
    for dataset in datasets:
        for case in (c for c in dataset.cases if c.split in splits and c.check):
            check = case.check
            if check["kind"] not in ("retrieval", "knowledge"):
                continue
            relevant: set[str] = set()
            items: dict[str, str] = {}
            if "gold" in check:
                resolved = gold.resolve(case_refs(case, gold_dir, gold_files), corpus)
                items = gold.item_keys(resolved)
                relevant = set(items.values())
                relevant |= {f"unresolved {r.ref.where}" for r in resolved if not r.keys}
            mentions = tuple(check.get("question_mentions", ()))
            if relevant or mentions or check.get("name"):
                found.append(
                    RewriteCase(
                        dataset.name,
                        case,
                        frozenset(relevant),
                        mentions,
                        check.get("name"),
                        items,
                    )
                )
    return found, gold_files


@dataclass(frozen=True)
class Planned:
    question: str
    queries: list[str]
    name: str | None
    input_tokens: int = 0
    output_tokens: int = 0


# A rewrite: the conversation in, the plan out.
Rewriter = Callable[[Sequence[BaseMessage]], Awaitable[Planned]]


class ModelRewriter:
    """The rewrite_query node's prompt and schema on a candidate model, with its usage."""

    def __init__(self, model: BaseChatModel):
        self.model = model.with_structured_output(
            SearchPlan, method="json_schema", include_raw=True
        )

    async def __call__(self, messages: Sequence[BaseMessage]) -> Planned:
        result = await self.model.ainvoke(rewrite_prompt(messages))
        if result["parsed"] is None:
            raise ValueError(f"the plan did not parse: {result['parsing_error']}")
        usage = result["raw"].usage_metadata or {}
        return Planned(
            **read_plan(result["parsed"], messages[-1].text),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )


async def as_is(messages: Sequence[BaseMessage]) -> Planned:
    """The baseline: the message itself is the one query, and no name is looked up."""
    latest = messages[-1].text
    return Planned(latest, [latest], None)


# A search: the queries and the name in, the keys of the records found out, in order.
Search = Callable[[list[str], str | None], Awaitable[list[str]]]


def retrieve_node_search(db: Database, embedder: Embedder, version_id: int) -> Search:
    """What the retrieve node finds for a plan, as record keys."""

    async def search(queries: list[str], name: str | None) -> list[str]:
        passages = await search_knowledge(db, embedder, version_id, queries)
        records = await records_named(db, version_id, name) if name else []
        return evidence_keys({"passages": passages, "records": records})

    return search


@dataclass(frozen=True)
class Searched:
    dataset: str
    case_id: str
    run: int
    relevant: int
    checks_question: bool  # whether the case asks for texts in the question or a name
    seconds: float  # the rewrite alone: the search is the same for every candidate
    question: str = ""
    queries: tuple[str, ...] = ()
    name: str | None = None
    missing: tuple[str, ...] = ()  # labelled items the search did not find
    lacking: tuple[str, ...] = ()  # texts the question should hold and does not
    name_ok: bool | None = None  # None when the case expects no name
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def recall(self) -> float | None:
        return (self.relevant - len(self.missing)) / self.relevant if self.relevant else None

    @property
    def question_ok(self) -> bool | None:
        """Whether the question and the name are as the case asks; None if it asks neither."""
        if not self.checks_question:
            return None
        return self.error is None and not self.lacking and self.name_ok is not False

    @property
    def passed(self) -> bool:
        return self.error is None and not self.missing and self.question_ok is not False


async def play(rewriter: Rewriter, search: Search, item: RewriteCase, run: int) -> Searched:
    case = item.case
    asked = {
        "dataset": item.dataset,
        "case_id": case.case_id,
        "run": run,
        "relevant": len(item.relevant),
        "checks_question": bool(item.question_mentions) or item.name is not None,
    }
    started = time.perf_counter()
    try:
        plan = await rewriter(case_messages(case))
        seconds = time.perf_counter() - started
        found = {item.items.get(key, key) for key in await search(plan.queries, plan.name)}
    except Exception as error:  # a failed call fails the case, and is reported
        seconds = time.perf_counter() - started
        return Searched(**asked, seconds=seconds, missing=tuple(item.relevant), error=repr(error))
    return Searched(
        **asked,
        seconds=seconds,
        question=plan.question,
        queries=tuple(plan.queries),
        name=plan.name,
        missing=tuple(sorted(item.relevant - found)),
        lacking=tuple(lacking(plan.question, item.question_mentions)),
        name_ok=None if item.name is None else plain(plan.name or "") == plain(item.name),
        input_tokens=plan.input_tokens,
        output_tokens=plan.output_tokens,
    )


@dataclass
class RewriteResult:
    candidate: Candidate
    runs: int
    plays: list[Searched] = field(default_factory=list)

    @property
    def baseline(self) -> bool:
        return self.candidate == BASELINE

    def passed_per_run(self) -> list[int]:
        return [sum(p.passed for p in self.plays if p.run == run) for run in range(self.runs)]

    @property
    def cases(self) -> int:
        return len({p.case_id for p in self.plays})

    @property
    def mean_passed(self) -> float:
        return statistics.mean(self.passed_per_run())

    def recall(self, datasets: Sequence[str]) -> float | None:
        """The mean recall of the labelled cases of these datasets, over every run."""
        scores = [p.recall for p in self.plays if p.dataset in datasets and p.recall is not None]
        return statistics.mean(scores) if scores else None

    @property
    def question_checks(self) -> tuple[int, int]:
        """Plays whose question and name are as asked, of those whose case asks."""
        asked = [p.question_ok for p in self.plays if p.question_ok is not None]
        return sum(asked), len(asked)

    def latency(self, share: float) -> float:
        return candidates.percentile([p.seconds for p in self.plays if p.error is None], share)

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
        if self.baseline:
            return 0.0
        return cost_per_thousand(self.candidate.model, *self.mean_tokens)

    def failures(self) -> dict[str, list[Searched]]:
        """The plays that did not pass, by case."""
        failed: dict[str, list[Searched]] = {}
        for p in self.plays:
            if not p.passed:
                failed.setdefault(p.case_id, []).append(p)
        return failed


async def compare(
    rewriters: Sequence[tuple[Candidate, Rewriter]],
    cases: Sequence[RewriteCase],
    search: Search,
    runs: int,
    progress: Callable[[str], None] = lambda line: None,
) -> list[RewriteResult]:
    """Every case through every rewriter and the search, `runs` times, the rewriters
    taking turns case by case; `progress` hears of each case."""
    results = [RewriteResult(candidate, runs) for candidate, _ in rewriters]
    for run in range(runs):
        for number, item in enumerate(cases, start=1):
            shown = []
            for result, (candidate, rewriter) in zip(results, rewriters, strict=True):
                searched = await play(rewriter, search, item, run)
                result.plays.append(searched)
                mark = "ok" if searched.passed else "FAIL"
                shown.append(f"{candidate.label} {mark} {searched.seconds:.1f}s")
            progress(
                f"run {run + 1}/{runs} case {number}/{len(cases)} {item.case.case_id}: "
                + "; ".join(shown)
            )
    return results


def rule_pick(results: Sequence[RewriteResult]) -> RewriteResult:
    """The candidate DV11's rule chooses among the models; the baseline is not one."""
    models = [r for r in results if not r.baseline]
    return candidates.rule_pick(models, lambda r: r.mean_passed)
