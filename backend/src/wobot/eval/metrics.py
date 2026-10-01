"""Scores from RAGAS metrics that need no model, so no judge runs and every number repeats.

Each labelled item is an ID, its record key. Lists and retrieval are scored with RAGAS's
ID-based context precision and recall, fields with its exact match and string presence.
What RAGAS does not
compute is counted here: F1 from that precision and recall, the items missed or not
labelled, and for retrieval the ranks, chunks and tokens. Retrieval is scored on records,
not chunks: a chunk counts as relevant when it holds a relevant record.
"""

import re
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ragas.dataset_schema import SingleTurnSample

with warnings.catch_warnings():
    # In ragas 0.4 the ID-based metrics exist only at this deprecated path;
    # ragas.metrics.collections lacks them.
    warnings.simplefilter("ignore", DeprecationWarning)
    from ragas.metrics import (
        ExactMatch,
        IDBasedContextPrecision,
        IDBasedContextRecall,
        StringPresence,
    )

PRECISION = IDBasedContextPrecision()
RECALL = IDBasedContextRecall()
EXACT_MATCH = ExactMatch()
STRING_PRESENCE = StringPresence()


async def id_scores(
    reference: set[str], retrieved: Sequence[str]
) -> tuple[float | None, float | None]:
    """RAGAS's ID-based context precision and recall; None where either set is empty."""
    sample = SingleTurnSample(
        retrieved_context_ids=list(retrieved), reference_context_ids=sorted(reference)
    )
    precision = await PRECISION.single_turn_ascore(sample) if retrieved else None
    recall = await RECALL.single_turn_ascore(sample) if reference else None
    return precision, recall


@dataclass
class SetScore:
    expected: int
    actual: int
    hits: int
    precision: float | None  # RAGAS id_based_context_precision
    recall: float | None  # RAGAS id_based_context_recall
    missing: list[str] = field(default_factory=list)  # expected, not returned
    unexpected: list[str] = field(default_factory=list)  # returned, not expected

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if p + r else 0.0


async def set_score(expected: set[str], actual: set[str]) -> SetScore:
    precision, recall = await id_scores(expected, sorted(actual))
    return SetScore(
        expected=len(expected),
        actual=len(actual),
        hits=len(expected & actual),
        precision=precision,
        recall=recall,
        missing=sorted(expected - actual),
        unexpected=sorted(actual - expected),
    )


@dataclass
class FieldScore:
    compared: int = 0
    correct: dict[str, int] = field(default_factory=dict)  # exactly as labelled
    present: dict[str, int] = field(default_factory=dict)  # holding the labelled value
    # (label location, field, expected, actual) for each disagreement.
    mismatches: list[tuple[str, str, Any, Any]] = field(default_factory=list)

    def accuracy(self, name: str) -> float | None:
        """The mean of RAGAS's exact match over the compared labels."""
        return self.correct.get(name, 0) / self.compared if self.compared else None

    def presence(self, name: str) -> float | None:
        """The mean of RAGAS's string presence: the value holds the label, as a location
        "the Hong Kong Polytechnic University" holds "Hong Kong", wider but not wrong."""
        return self.present.get(name, 0) / self.compared if self.compared else None


async def field_score(
    pairs: Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]], names: Sequence[str]
) -> FieldScore:
    """RAGAS's exact match and string presence per field between labels and records, after
    collapsing spaces. An empty label is present only in an empty value."""
    score = FieldScore(correct=dict.fromkeys(names, 0), present=dict.fromkeys(names, 0))
    for where, expected, actual in pairs:
        score.compared += 1
        for name in names:
            want, got = comparable(expected.get(name)), comparable(actual.get(name))
            if await EXACT_MATCH.single_turn_ascore(SingleTurnSample(reference=want, response=got)):
                score.correct[name] += 1
            else:
                score.mismatches.append((where, name, want, got))
            if want is None:
                score.present[name] += got is None
            else:
                sample = SingleTurnSample(reference=want, response=got or "")
                score.present[name] += await STRING_PRESENCE.single_turn_ascore(sample) == 1
    return score


# A space between Chinese characters or their punctuation is layout, such as a line break
# on the page, which a person copying the text leaves out.
_CJK = r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]"
_LAYOUT_SPACE = re.compile(rf"(?<={_CJK}) (?={_CJK})")


def comparable(value: Any) -> str | None:
    """A value as text the way a label writes it: ISO dates, spaces collapsed, '' as None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return _LAYOUT_SPACE.sub("", " ".join(str(value).split())) or None


@dataclass
class RetrievalScore:
    k: int
    relevant: int
    found: int  # relevant records within the top k chunks
    records: int  # distinct records the top k chunks hold
    recall: float | None  # RAGAS id_based_context_recall over records
    record_precision: float | None  # RAGAS id_based_context_precision over records
    chunks_with_relevant: int
    first_relevant_rank: int | None  # 1-based rank of the first chunk holding one
    context_tokens: int  # what a model would read: the top k chunks' tokens
    missing: list[str] = field(default_factory=list)

    @property
    def chunk_precision(self) -> float | None:
        return self.chunks_with_relevant / self.k

    @property
    def reciprocal_rank(self) -> float:
        return 1 / self.first_relevant_rank if self.first_relevant_rank else 0.0


async def retrieval_score(
    relevant: set[str], ranked: Sequence[tuple[Sequence[str], int]], k: int
) -> RetrievalScore:
    """`ranked`: the top chunks in order, each as (its record keys, its token count)."""
    top = list(ranked[:k])
    held = list(dict.fromkeys(key for keys, _ in top for key in keys))  # in rank order
    hits = [any(key in relevant for key in keys) for keys, _ in top]
    precision, recall = await id_scores(relevant, held)
    return RetrievalScore(
        k=k,
        relevant=len(relevant),
        found=len(relevant & set(held)),
        records=len(held),
        recall=recall,
        record_precision=precision,
        chunks_with_relevant=sum(hits),
        first_relevant_rank=hits.index(True) + 1 if any(hits) else None,
        context_tokens=sum(tokens for _, tokens in top),
        missing=sorted(relevant - set(held)),
    )


def mean(values: Sequence[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) / len(known) if known else None
