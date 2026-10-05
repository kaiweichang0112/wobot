"""A run's results as JSON, for comparing runs, and as Markdown, for reading one."""

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from wobot.eval.agent import AgentCaseResult
from wobot.eval.metrics import FieldScore, RetrievalScore, SetScore, TranscriptionScore, mean
from wobot.eval.runner import CaseResult, RunResult
from wobot.eval.vision import ModelResult


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
    if case.transcription:
        t = case.transcription
        data["transcription"] |= {
            "text_recall": t.text_recall,
            "value_precision": t.value_precision,
            "value_recall": t.value_recall,
        }
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
        transcriptions = [c.transcription for c in cases if c.transcription]
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
        if transcriptions:
            group |= {
                "text_recall": mean([t.text_recall for t in transcriptions]),
                "value_precision": mean([t.value_precision for t in transcriptions]),
                "value_recall": mean([t.value_recall for t in transcriptions]),
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
    if case.transcription:
        parts.append(transcription_result(case.transcription))
    if case.detail:
        parts.append(case.detail)
    return "; ".join(parts)


def transcription_result(t: TranscriptionScore) -> str:
    return (
        f"text {t.lines_found} of {t.lines} lines ({_num(t.text_recall)}), "
        f"values P {_num(t.value_precision)} R {_num(t.value_recall)} "
        f"({t.values_matched} of {t.values} transcribed, {t.values_read} read)"
    )


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
    if case.transcription:
        lines += transcription_details(case.case_id, case.transcription)
    return lines


def transcription_details(case_id: str, t: TranscriptionScore) -> list[str]:
    return [
        *(f"- {case_id} line not read: {line!r}" for line in t.missing_lines),
        *(f"- {case_id} value not read: {value!r}" for value in t.missing_values),
        *(f"- {case_id} value read but not transcribed: {value!r}" for value in t.extra_values),
    ]


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


# --- Vision model comparison ------------------------------------------------------------


def vision_markdown(results: Sequence[ModelResult], meta: Mapping[str, Any]) -> str:
    cases = list(dict.fromkeys(case for result in results for case in result.scores))
    lines = [
        "# Vision models on the dev transcriptions",
        "",
        *(f"- {name}: {_text(value)}" for name, value in meta.items()),
        "",
        "## Models",
        "",
        "| Model | Text recall | Value precision | Value recall | Input tokens | Output tokens "
        "| New calls | No reading |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r.model} | {_num(r.mean('text_recall'))} | {_num(r.mean('value_precision'))} "
            f"| {_num(r.mean('value_recall'))} | {r.input_tokens} | {r.output_tokens} "
            f"| {r.calls} | {len(r.failures)} |"
        )
    lines += ["", "## Cases", "", "| Case | " + " | ".join(r.model for r in results) + " |"]
    lines.append("| --- |" + " --- |" * len(results))
    for case in cases:
        cells = [transcription_result(r.scores[case]) for r in results]
        lines.append(f"| {case} | " + " | ".join(cells) + " |")
    for r in results:
        details = [line for case in cases for line in transcription_details(case, r.scores[case])]
        details += [f"- {case} no reading: {failure}" for case, failure in r.failures.items()]
        if details:
            lines += ["", f"## {r.model}", "", *details]
    return "\n".join(lines) + "\n"


def write_vision_report(
    results: Sequence[ModelResult], meta: Mapping[str, Any], directory: Path, stem: str
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "meta": dict(meta),
        "models": [
            asdict(r)
            | {name: r.mean(name) for name in ("text_recall", "value_precision", "value_recall")}
            for r in results
        ],
    }
    (directory / f"{stem}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    markdown = directory / f"{stem}.md"
    markdown.write_text(vision_markdown(results, meta), encoding="utf-8")
    return markdown


# --- Agent checks -----------------------------------------------------------------------


def agent_summary(results: Sequence[AgentCaseResult]) -> dict[str, Any]:
    """Per split: tool-selection accuracy and retrieval; then what the turns cost."""
    scored = [r for r in results if r.status == "scored"]
    records = [r.record for r in scored if r.record]
    out: dict[str, Any] = {
        "cases": len(results),
        "scored": len(scored),
        "pending": sum(r.status == "pending" for r in results),
        "errors": sum(r.status == "error" for r in results),
    }
    for split in ("dev", "heldout"):
        tools = [r for r in scored if r.split == split and r.kind == "tools"]
        if tools:
            out[f"tools {split}"] = {"accuracy": mean([float(bool(r.passed)) for r in tools])}
        retrieval = [r for r in scored if r.split == split and r.kind == "retrieval"]
        if retrieval:
            first = [r.first_search for r in retrieval if r.first_search]
            turn = [r.turn_searches for r in retrieval if r.turn_searches]
            out[f"retrieval {split}"] = {
                "cases": len(retrieval),
                "first_recall_at_k": mean([s.recall for s in first]),
                "first_mrr": mean([s.reciprocal_rank for s in first]),
                "turn_recall": mean([s.recall for s in turn]),
                "turn_mrr": mean([s.reciprocal_rank for s in turn]),
                "turn_context_tokens": mean([s.context_tokens for s in turn]),
                "searches": mean([r.searches for r in retrieval]),
                "no_search": sum(r.searches == 0 for r in retrieval),
            }
        for kind in ("behavior", "recommendation"):
            played = [r for r in scored if r.split == split and r.kind == kind]
            if played:
                out[f"{kind} {split}"] = {
                    "cases": len(played),
                    "passed": sum(bool(r.passed) for r in played),
                }
        lists = [r.set for r in scored if r.split == split and r.kind == "list" and r.set]
        if lists:
            out[f"list {split}"] = {
                "cases": len(lists),
                "precision": mean([s.precision for s in lists]),
                "recall": mean([s.recall for s in lists]),
                "f1": mean([s.f1 for s in lists]),
                "complete": sum(s.f1 == 1 for s in lists),
            }
    if records:
        out |= {
            # Unsent: the model wrote a call as text and called nothing (see eval.agent).
            "unsent_calls": sum(bool(r.unsent_calls) for r in records),
            "refused_calls": sum(any(t.outcome == "error" for t in r.tools) for r in records),
            "failed_tools": sum(any(t.outcome == "failed" for t in r.tools) for r in records),
            # Turns whose answer the guard held back, and turns that ended retryable.
            "retried": sum(bool(r.retries) for r in records),
            "unverified": sum(r.status == "unverified" for r in records),
            "retryable": sum(r.status == "retryable" for r in records),
            "mean_seconds": mean([r.seconds for r in records]),
            "max_seconds": max(r.seconds for r in records),
            "model_calls": sum(r.model_calls for r in records),
            "input_tokens": sum(r.input_tokens for r in records),
            "output_tokens": sum(r.output_tokens for r in records),
        }
    return out


def _scenarios(results: Sequence[AgentCaseResult]) -> dict[str, str]:
    groups: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        if r.status == "scored" and r.kind == "tools":
            groups[r.scenario].append(bool(r.passed))
    return {name: f"{sum(p)} of {len(p)}" for name, p in sorted(groups.items())}


def _agent_result(r: AgentCaseResult) -> str:
    if r.kind == "tools":
        expected = " or ".join("{" + ", ".join(e) + "}" for e in r.expect)
        verdict = "pass" if r.passed else "**fail**"
        return f"{verdict}: expected {expected}"
    if r.kind in ("behavior", "recommendation"):
        return "pass" if r.passed else "**fail**: " + "; ".join(r.failures)
    if r.kind == "list":
        s = r.set
        return (
            f"F1 {_num(s.f1)}, P {_num(s.precision)} R {_num(s.recall)} "
            f"({s.hits} of {s.expected} labelled, {s.actual} shown)"
        )
    first, turn = r.first_search, r.turn_searches
    return (
        f"first search recall@{first.k} {_num(first.recall)}, MRR {_num(first.reciprocal_rank)};"
        f" {r.searches} searches, {turn.records} records: recall {_num(turn.recall)},"
        f" {turn.context_tokens} tokens"
    )


def agent_markdown(results: Sequence[AgentCaseResult], meta: Mapping[str, Any]) -> str:
    lines = [
        "# Agent checks",
        "",
        *(f"- {name}: {_text(value)}" for name, value in meta.items()),
        "",
        "## Summary",
        "",
        *(f"- {name}: {_text(value)}" for name, value in agent_summary(results).items()),
        "",
        "## Tool selection by scenario",
        "",
        *(f"- {name}: {value}" for name, value in _scenarios(results).items()),
        "",
        "## Cases",
        "",
        "| Case | Split | Check | Result | Called | Calls | Seconds |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        if r.status != "scored" or r.record is None:
            cells = f"| {r.case_id} | {r.split} | {r.kind} | {r.status}: {r.detail} |"
            lines.append(cells + " |" * 3)
            continue
        calls = ", ".join(f"{t.name} {t.outcome}" for t in r.record.tools) or "none"
        lines.append(
            f"| {r.case_id} | {r.split} | {r.kind} | {_agent_result(r)} "
            f"| {{{', '.join(r.called)}}} | {calls} | {r.record.seconds:.1f} |"
        )
    details = [
        f"- {r.case_id} not found by its searches: {key}"
        for r in results
        if r.turn_searches
        for key in r.turn_searches.missing
    ]
    for r in (r for r in results if r.set):
        details += [f"- {r.case_id} not shown: {key}" for key in r.set.missing]
        details += [f"- {r.case_id} shown, not labelled: {key}" for key in r.set.unexpected]
    if details:
        lines += ["", "## Missed and extra records", "", *details]
    lines += ["", "## Turns", ""]
    for r in results:
        if r.record is None:
            continue
        lines.append(f"### {r.case_id}: {r.user_input}")
        lines.append("")
        for tool in r.record.tools:
            args = json.dumps(tool.args, ensure_ascii=False)
            lines.append(f"- {tool.name} {args} → {tool.outcome}")
        for text in r.record.unsent_calls:
            lines.append(f"- unsent call, written as text: `{text}`")
        for held in r.record.retries:
            lines.append(f"- asked again, {held['reason']}: {'; '.join(held['detail'])}")
        for problem in r.record.problems:
            lines.append(f"- held back: {problem}")
        lines += ["", "> " + r.record.answer.replace("\n", "\n> "), ""]
    return "\n".join(lines) + "\n"


def write_agent_report(
    results: Sequence[AgentCaseResult], meta: Mapping[str, Any], directory: Path, stem: str
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "meta": dict(meta),
        "summary": agent_summary(results),
        "cases": [asdict(r) for r in results],
    }
    (directory / f"{stem}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    markdown = directory / f"{stem}.md"
    markdown.write_text(agent_markdown(results, meta), encoding="utf-8")
    return markdown
