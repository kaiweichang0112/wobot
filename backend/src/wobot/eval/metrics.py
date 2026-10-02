"""Scores from RAGAS metrics that need no model, so no judge runs and every number repeats.

Each labelled item is an ID, its record key. Lists and retrieval are scored with RAGAS's
ID-based context precision and recall, fields with its exact match and string presence.
What RAGAS does not
compute is counted here: F1 from that precision and recall, the items missed or not
labelled, and for retrieval the ranks, chunks and tokens. Retrieval is scored on records,
not chunks: a chunk counts as relevant when it holds a relevant record.
"""

import re
import unicodedata
import warnings
from collections import Counter
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


@dataclass
class TranscriptionScore:
    lines: int  # lines of text a person transcribed
    lines_found: int  # of them, present in the model's transcription
    values: int  # numbers with units a person transcribed
    values_read: int  # numbers with units the model gave
    values_matched: int
    # Whether a person listed the picture's values; an empty list says it has none.
    values_transcribed: bool = True
    missing_lines: list[str] = field(default_factory=list)
    missing_values: list[str] = field(default_factory=list)
    extra_values: list[str] = field(default_factory=list)  # read, but not transcribed

    @property
    def text_recall(self) -> float | None:
        """The mean of RAGAS's string presence over the transcribed lines."""
        return self.lines_found / self.lines if self.lines else None

    @property
    def value_precision(self) -> float | None:
        if not self.values_transcribed:
            return None
        if not self.values_read:
            return None if self.values else 1.0  # none there, and none read
        return self.values_matched / self.values_read

    @property
    def value_recall(self) -> float | None:
        return self.values_matched / self.values if self.values else None


async def transcription_score(
    lines: Sequence[str],
    values: Sequence[tuple[str, str | None]] | None,
    reading: Mapping[str, Any] | None,
) -> TranscriptionScore:
    """A model's reading of a picture against a person's transcription of it.

    Each transcribed line counts when RAGAS's string presence finds it in the model's text.
    Values count as (value, unit) pairs, each matched at most once, so precision shows
    numbers the model read that are not there, or read wrong. The label beside a value is
    not compared: the same number may be named in other words. `values` is None when the
    person did not list them, and then they are not scored.
    """
    reading = reading or {"verbatim_text": [], "values": []}
    transcribed = _folded("\n".join(reading["verbatim_text"])) or ""
    score = TranscriptionScore(
        len(lines),
        0,
        len(values or ()),
        len(reading["values"]),
        0,
        values_transcribed=values is not None,
    )
    for line in lines:
        sample = SingleTurnSample(reference=_folded(line), response=transcribed)
        if await STRING_PRESENCE.single_turn_ascore(sample) == 1:
            score.lines_found += 1
        else:
            score.missing_lines.append(line)
    read = Counter(_pair(v["value"], v["unit"]) for v in reading["values"])
    if values is None:
        return score
    for value, unit in values:
        pair = _pair(value, unit)
        if read[pair] > 0:
            read[pair] -= 1
            score.values_matched += 1
        else:
            score.missing_values.append(f"{value}{unit or ''}")
    score.extra_values = sorted(
        f"{value}{unit}" for (value, unit), n in read.items() for _ in range(n)
    )
    return score


def _pair(value: str, unit: str | None) -> tuple[str, str]:
    return _folded(value) or "", _folded(unit or "") or ""


# Marks no eye tells apart in print: hyphens and dashes, middle dots.
_DASHES = str.maketrans(dict.fromkeys("‐‑‒–—―−", "-") | dict.fromkeys("・･‧", "·"))
# A space beside a Chinese character, as between "公司" and "SEDA", may be layout too.
_SPACE_BY_CJK = re.compile(rf"(?<={_CJK}) +| +(?={_CJK})")


def _folded(text: str) -> str | None:
    """Text as compared for transcriptions: full-width forms folded, as "２０％" is "20%",
    dashes and middle dots as one, and spaces beside Chinese characters gone."""
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    return comparable(_SPACE_BY_CJK.sub("", " ".join(text.split())))


def mean(values: Sequence[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) / len(known) if known else None
