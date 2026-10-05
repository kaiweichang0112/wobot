"""Run datasets against one index version: each case's check, from labels to scores.

Four checks, all on item identities:
- list: the records a structured filter returns against the labelled items. This is the
  query the answer agent will run for "list every …" questions.
- fields: labelled field values against the matched records' fields.
- retrieval: the top k chunks of a semantic search, mapped to their records, against the
  labelled relevant items.
- transcription: what a vision model read in one image or PDF page against what a person
  transcribed from it.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.eval import gold
from wobot.eval.corpus import Corpus
from wobot.eval.dataset import Case, Dataset
from wobot.eval.metrics import (
    FieldScore,
    RetrievalScore,
    SetScore,
    TranscriptionScore,
    field_score,
    retrieval_score,
    set_score,
    transcription_score,
)
from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.search import chunk_record_keys, search_chunks


@dataclass
class CaseResult:
    dataset: str
    case_id: str
    split: str
    kind: str | None
    status: str  # scored, pending or error
    detail: str | None = None
    labels: int = 0
    # Labels that match no record: each counts as an expected item not returned.
    unresolved: list[str] = field(default_factory=list)
    set: SetScore | None = None
    fields: FieldScore | None = None
    retrieval: RetrievalScore | None = None
    transcription: TranscriptionScore | None = None


@dataclass
class RunResult:
    version_id: int
    k: int
    cases: list[CaseResult]
    gold_files: dict[str, str]  # file name → sha256 of the labels the run used


async def run_datasets(
    conn: AsyncConnection,
    corpus: Corpus,
    embedder: Embedder | None,
    datasets: Sequence[Dataset],
    *,
    k: int,
    gold_dir: Path = gold.GOLD_DIR,
    splits: Sequence[str] = ("dev", "heldout"),
) -> RunResult:
    """Score every case of the given splits. Without an embedder, retrieval stays pending."""
    run = RunResult(version_id=corpus.version_id, k=k, cases=[], gold_files={})
    retrieval: list[tuple[CaseResult, Case, list[gold.Resolved]]] = []
    for dataset in datasets:
        for case in (c for c in dataset.cases if c.split in splits):
            result = CaseResult(dataset.name, case.case_id, case.split, _kind(case), "pending")
            run.cases.append(result)
            if case.check is None:
                result.detail = case.pending or "no check defined"
                continue
            if result.kind in AGENT_CHECKS:
                result.detail = "scored by wobot-eval agent"
                continue
            try:
                refs = case_refs(case, gold_dir, run.gold_files)
            except gold.GoldError as error:
                result.status, result.detail = "error", str(error)
                continue
            if not refs:
                result.detail = "not labelled yet"
                continue
            resolved = gold.resolve(refs, corpus)
            result.labels = len(refs)
            result.unresolved = [_describe(r) for r in resolved if not r.keys]
            if result.kind == "retrieval":
                retrieval.append((result, case, resolved))
                continue
            if result.kind == "transcription":
                await _score_transcription(result, resolved, corpus)
                continue
            await _score_records(result, case, resolved, corpus)
    if retrieval and embedder is not None:
        await _score_retrieval(conn, embedder, corpus.version_id, retrieval, k)
    for result, _, _ in retrieval:
        if result.retrieval is None:
            result.detail = "needs an embedder"
    return run


# Checks scored from what the chat agent did, by `wobot-eval agent`: they need paid model
# calls, which `run` never makes.
AGENT_CHECKS = frozenset({"tools"})


def _kind(case: Case) -> str | None:
    return case.check["kind"] if case.check else None


def case_refs(case: Case, gold_dir: Path, used: dict[str, str]) -> list[gold.Ref]:
    """The labels a case's check reads; `used` collects each gold file's hash."""
    spec = case.check["gold"]
    path = gold_dir / spec["file"]
    if not path.exists():
        raise gold.GoldError(f"{spec['file']}: no such gold file")
    used[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    match spec.get("as"):
        case "speech" | "publication" | "product" as kind:
            return gold.line_refs(path, kind)
        case "student":
            return gold.student_refs(path, spec["degree"], spec["years"])
        case "project":
            return gold.project_refs(path)
        case "speech_fields":
            return gold.speech_field_refs(path)
        case "transcription":
            return gold.transcription_refs(path, case.case_id)
        case None:
            return gold.named_refs(path, case.case_id, spec.get("key", "relevant"))
    raise gold.GoldError(f"{case.case_id}: unknown gold format {spec.get('as')!r}")


def labelled_keys(
    case: Case, corpus: Corpus, gold_dir: Path, used: dict[str, str]
) -> tuple[set[str], list[str]]:
    """The record keys a case's labels name, and the labels that match no record.

    A label that matches no record stays in the set under a key no record holds, so it
    counts as missed, as in the run's own checks.
    """
    resolved = gold.resolve(case_refs(case, gold_dir, used), corpus)
    unresolved = [_describe(r) for r in resolved if not r.keys]
    keys = {key for r in resolved for key in r.keys}
    return keys | {f"unresolved {where}" for where in unresolved}, unresolved


def _describe(resolved: gold.Resolved) -> str:
    ref = resolved.ref
    value = ref.value if isinstance(ref.value, str) else ", ".join(map(str, ref.value.values()))
    problem = f" ({resolved.problem})" if resolved.problem else ""
    return f"{ref.where}: {ref.kind} {value[:120]!r}{problem}"


async def _score_records(
    result: CaseResult, case: Case, resolved: list[gold.Resolved], corpus: Corpus
) -> None:
    check = case.check
    by_key = {item.logical_key: item for item in corpus.of(check["records"])}
    if result.kind == "list":
        expected = {key for r in resolved for key in r.keys}
        expected |= {f"unresolved {where}" for where in result.unresolved}
        actual = {
            item.logical_key
            for item in corpus.of(check["records"])
            if _matches(item.fields, check.get("where", {}))
        }
        result.set = await set_score(expected, actual)
    names = check.get("fields")
    if names:
        pairs = [
            (r.ref.where, r.ref.expected, by_key[r.keys[0]].fields)
            for r in resolved
            if r.keys and r.keys[0] in by_key
        ]
        result.fields = await field_score(pairs, names)
    seen: set[tuple[str, ...]] = set()
    duplicates = 0
    for r in (r for r in resolved if r.keys):
        duplicates += tuple(r.keys) in seen
        seen.add(tuple(r.keys))
    if duplicates:
        result.detail = f"{duplicates} labels name an item already labelled"
    result.status = "scored"


async def _score_transcription(
    result: CaseResult, resolved: list[gold.Resolved], corpus: Corpus
) -> None:
    [label] = resolved
    if not label.keys:
        result.status, result.detail = "error", "the picture is not in this version"
        return
    item = next(i for i in corpus.items if i.logical_key == label.keys[0])
    expected = label.ref.expected
    result.transcription = await transcription_score(
        expected["text"], expected["values"], item.fields["reading"]
    )
    if item.fields["reading"] is None:
        result.detail = "the model gave no reading"
    result.status = "scored"


def _matches(fields: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
    for name, condition in where.items():
        value = fields.get(name)
        if isinstance(condition, dict):
            if "in" in condition and value not in condition["in"]:
                return False
        elif value != condition:
            return False
    return True


async def _score_retrieval(
    conn: AsyncConnection,
    embedder: Embedder,
    version_id: int,
    cases: list[tuple[CaseResult, Case, list[gold.Resolved]]],
    k: int,
) -> None:
    embedded = await embedder.embed([case.user_input for _, case, _ in cases])
    for (result, _, resolved), vector in zip(cases, embedded.vectors, strict=True):
        hits = await search_chunks(conn, vector, embedder.config_id, k, version_id=version_id)
        keys = await chunk_record_keys(conn, [hit.chunk_id for hit in hits], version_id)
        relevant = {key for r in resolved for key in r.keys}
        relevant |= {f"unresolved {where}" for where in result.unresolved}
        ranked = [(keys[hit.chunk_id], hit.token_count) for hit in hits]
        result.retrieval = await retrieval_score(relevant, ranked, k)
        result.status = "scored"
