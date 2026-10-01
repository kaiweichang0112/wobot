"""A run's results as JSON, for comparing runs, and as Markdown, for reading one."""

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from wobot.eval.metrics import FieldScore, RetrievalScore, SetScore, mean
from wobot.eval.runner import CaseResult, RunResult


def to_json(run: RunResult, meta: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "meta": dict(meta),
        "summary": summary(run),
        "cases": [_case_json(case) for case in run.cases],
    }


def _case_json(case: CaseResult) -> dict[str, Any]:
    data = asdict(case)
    if case.set:
        data["set"]["f1"] = case.set.f1
    if case.fields:
        data["fields"]["accuracy"] = {n: case.fields.accuracy(n) for n in case.fields.correct}
        data["fields"]["presence"] = {n: case.fields.presence(n) for n in case.fields.present}
    if case.retrieval:
        r = case.retrieval
        data["retrieval"] |= {
            "chunk_precision": r.chunk_precision,
            "reciprocal_rank": r.reciprocal_rank,
        }
    return data


def summary(run: RunResult) -> dict[str, Any]:
    """Means per check and split, over the scored cases only."""
    groups: dict[tuple[str, str], list[CaseResult]] = defaultdict(list)
    for case in run.cases:
        if case.status == "scored":
            groups[case.kind or "", case.split].append(case)
    out: dict[str, Any] = {
        "cases": len(run.cases),
        "scored": sum(c.status == "scored" for c in run.cases),
        "pending": sum(c.status == "pending" for c in run.cases),
        "errors": sum(c.status == "error" for c in run.cases),
    }
    for (kind, split), cases in sorted(groups.items()):
        sets = [c.set for c in cases if c.set]
        retrievals = [c.retrieval for c in cases if c.retrieval]
        fields = [c.fields for c in cases if c.fields]
        group: dict[str, Any] = {"cases": len(cases)}
        if sets:
            group |= {
                "precision": mean([s.precision for s in sets]),
                "recall": mean([s.recall for s in sets]),
                "f1": mean([s.f1 for s in sets]),
                "complete": sum(s.f1 == 1 for s in sets),
            }
        if fields:
            group["field_accuracy"] = _pooled(fields, "correct")
            group["field_presence"] = _pooled(fields, "present")
        if retrievals:
            group |= {
                "recall_at_k": mean([r.recall for r in retrievals]),
                "mrr": mean([r.reciprocal_rank for r in retrievals]),
                "record_precision": mean([r.record_precision for r in retrievals]),
                "chunk_precision": mean([r.chunk_precision for r in retrievals]),
                "context_tokens": mean([r.context_tokens for r in retrievals]),
            }
        out[f"{kind}/{split}"] = group
    return out


def _pooled(scores: list[FieldScore], counts: str) -> dict[str, float | None]:
    """Each field's share over the labels of the cases that compare it."""
    pooled: dict[str, float | None] = {}
    for name in sorted({name for score in scores for name in getattr(score, counts)}):
        having = [score for score in scores if name in getattr(score, counts)]
        compared = sum(score.compared for score in having)
        hits = sum(getattr(score, counts)[name] for score in having)
        pooled[name] = hits / compared if compared else None
    return pooled


def to_markdown(run: RunResult, meta: Mapping[str, Any]) -> str:
    lines = [
        f"# Evaluation run, version {run.version_id}, k = {run.k}",
        "",
        *(f"- {name}: {_text(value)}" for name, value in meta.items()),
        "",
        "## Cases",
        "",
        "| Dataset | Case | Split | Check | Status | Result |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in run.cases:
        result = _result(case) if case.status == "scored" else (case.detail or "")
        lines.append(
            f"| {case.dataset} | {case.case_id} | {case.split} | {case.kind or '—'} "
            f"| {case.status} | {result} |"
        )
    lines += ["", "## Summary", ""]
    for group, values in summary(run).items():
        lines.append(f"- {group}: {_text(values)}")
    details = [line for case in run.cases for line in _details(case)]
    if details:
        lines += ["", "## Details", "", *details]
    return "\n".join(lines) + "\n"


def _result(case: CaseResult) -> str:
    parts = []
    if case.set:
        s: SetScore = case.set
        parts.append(
            f"P {_num(s.precision)} R {_num(s.recall)} F1 {_num(s.f1)} "
            f"({s.hits} of {s.expected} labelled, {s.actual} returned)"
        )
    if case.fields:
        accuracy = ", ".join(
            f"{n} {_num(case.fields.accuracy(n))} (present {_num(case.fields.presence(n))})"
            for n in case.fields.correct
        )
        parts.append(f"fields over {case.fields.compared}: {accuracy}")
    if case.retrieval:
        r: RetrievalScore = case.retrieval
        parts.append(
            f"recall@{r.k} {_num(r.recall)}, MRR {_num(r.reciprocal_rank)}, "
            f"record precision {_num(r.record_precision)}, {r.context_tokens} tokens"
        )
    if case.detail:
        parts.append(case.detail)
    return "; ".join(parts)


def _details(case: CaseResult) -> list[str]:
    lines = []
    for where in case.unresolved:
        lines.append(f"- {case.case_id} label matches no record: {where}")
    if case.set:
        lines += [f"- {case.case_id} missing: {key}" for key in case.set.missing]
        lines += [f"- {case.case_id} not labelled: {key}" for key in case.set.unexpected]
    if case.fields:
        lines += [
            f"- {case.case_id} {where} {name}: expected {want!r}, got {got!r}"
            for where, name, want, got in case.fields.mismatches
        ]
    if case.retrieval:
        lines += [f"- {case.case_id} not retrieved: {key}" for key in case.retrieval.missing]
    return lines


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k} {_text(v)}" for k, v in value.items())
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def write_report(run: RunResult, meta: Mapping[str, Any], directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    data = to_json(run, meta)
    (directory / f"{stem}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    markdown = directory / f"{stem}.md"
    markdown.write_text(to_markdown(run, meta), encoding="utf-8")
    return markdown
