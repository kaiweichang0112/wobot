"""A person's verdicts on saved replies, which the judge is measured against (V8).

A sample of a judged run goes into a gold file for a person to label blind: every reply
the judge failed or the text check disagreed with, filled up with replies it passed, in
a shuffled order and without the judge's verdicts. A label belongs to a reply's text, not
to a verdict, so one file measures every version of the judge. Agreement is the share of
replies the judge and the person decide alike, and Cohen's kappa, which takes out what
two raters agree on by chance: when nearly every reply passes, a judge that passes all
of them agrees often and measures nothing. Each path has a file of its own, since each
path's prompt is measured apart.
"""

import hashlib
import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from wobot.eval import gold
from wobot.eval.judge import CRITERIA, PATHS, Judged, JudgeInput, shown_evidence

LABELS = {
    "knowledge": gold.GOLD_DIR / "judge-labels.yaml",
    "list": gold.GOLD_DIR / "judge-labels-lists.yaml",
    "recommend": gold.GOLD_DIR / "judge-labels-recommend.yaml",
}
VERDICTS = {"pass": True, "fail": False}
# What each label compares with: the rubric's criteria, and faithfulness's verdict that
# every statement is supported, against the person's supported.
COMPARED = {"expected": "expected", "supported": "supported", "grounded": "supported"}

HEADER = """\
# Judge labels: a person's verdicts on replies a wobot-eval answer run kept, which the
# judge is measured against (V8). Read the message, what the case expects and the reply;
# where an entry asks for supported, also its case's passages under evidence. Write pass
# or fail. A blank is not a label, and the judge's own verdicts are not shown.
#
#   expected   the reply does what expected says: judge the meaning, not the wording or
#              the order, and do not require what expected marks optional ("may")
#   supported  every fact the reply states is in the passages or records; a greeting,
#              advice, encouragement and wording the user asked to be written are not
#              facts, and neither is saying the sources do not state something
#
"""

LIST_HEADER = """\
# Judge labels: a person's verdicts on the words the list path wrote before its lists,
# which the judge is measured against (V8). Read the message, what the case expects, the
# words (reply) and how many items of each list they show (lists_shown); for supported,
# also the case's lists under evidence. Code writes the items, and lists-v1 scores them.
# Write pass or fail. A blank is not a label, and the judge's own verdicts are not shown.
#
#   expected   the words, with the lists shown, do what expected says: judge the meaning,
#              not the wording or the order; the items shown count, so the words need
#              not repeat them
#   supported  every fact the words state is in the lists under evidence, such as what a
#              list holds and its count; saying the sources list nothing, or that a part
#              can be asked again, is not a fact
#
"""
REC_HEADER = """\
# Judge labels: a person's verdicts on the words the recommendation agent wrote itself: a
# question, why no product fits, or the sentence before the products it chose. Read the
# conversation, the message, what the case expects, the words (reply) and the products
# shown as cards (products_shown); for supported, also each tool the agent called and what
# it returned (tools). Code chooses the products and writes their cards, and
# recommendation-v2 scores them. Write pass or fail. A blank is not a label, and the
# judge's own verdicts are not shown.
#
#   expected   the words, with the products shown, do what expected says: judge the
#              meaning, not the wording or the order; the cards count, so the words need
#              not name the products
#   supported  every fact the words state about the products or the catalog, such as what
#              was found, how many, or which need none meets, is in what the tools returned
#              or in the conversation; a question, the choices it offers, advice and a
#              greeting are not facts
#
"""
HEADERS = {"knowledge": HEADER, "list": LIST_HEADER, "recommend": REC_HEADER}

type Key = tuple[str, str, int]  # candidate, case, run


def key_of(judged: Judged | JudgeInput) -> Key:
    return judged.candidate, judged.case_id, judged.run


def reply_sha(text: str) -> str:
    """What a label is tied to: a label never moves to another reply."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def load_judged(path: Path) -> tuple[dict[str, Any], list[Judged]]:
    """A judge report's meta and every reply it judged."""
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {f.name for f in fields(Judged)}
    judged = []
    for candidate in data["candidates"]:
        for j in candidate["judged"]:
            if "mentions_passed" in j:  # reports before V8d
                j["checked"] = j.pop("mentions_passed")
            judged.append(Judged(**{k: v for k, v in j.items() if k in names}))
    return data["meta"], judged


# --- The sample -------------------------------------------------------------------------


def flagged(judged: Judged) -> bool:
    """The judge failed it, found a statement unsupported, or the run's check disagreed."""
    return (
        any(judged.passed(name) is False for name in CRITERIA)
        or judged.grounded is False
        or judged.unlike_check
    )


def unsupported(judged: Judged) -> bool:
    return judged.passed("supported") is False or judged.grounded is False


@dataclass(frozen=True)
class Sampled:
    judged: Judged
    asks_supported: bool


def sample(
    judged: Sequence[Judged], size: int = 40, supported: int = 15, seed: int = 0
) -> list[Sampled]:
    """Every flagged reply, filled up to `size` with replies the judge passed; supported
    asked of every reply found unsupported, filled up to `supported` with others. The
    order is shuffled, so it does not tell which the judge failed."""
    rng = random.Random(seed)
    verdicts = [j for j in judged if j.error is None]
    chosen = [j for j in verdicts if flagged(j)]
    passed = [j for j in verdicts if not flagged(j)]
    chosen += rng.sample(passed, max(0, min(len(passed), size - len(chosen))))
    asked = [j for j in chosen if unsupported(j)]
    others = [j for j in chosen if not unsupported(j)]
    asked += rng.sample(others, max(0, min(len(others), supported - len(asked))))
    asks = {key_of(j) for j in asked}
    rng.shuffle(chosen)
    return [Sampled(j, key_of(j) in asks) for j in chosen]


class _Text(str):
    """A string written as a YAML block when it runs over lines."""


def _text_block(dumper: yaml.SafeDumper, value: _Text) -> yaml.Node:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style=style)


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        """Each value written out where it stands: no &id001 for a person to follow."""
        return True


_Dumper.add_representer(_Text, _text_block)


def labels_template(
    sampled: Sequence[Sampled],
    inputs: Mapping[Key, JudgeInput],
    run_name: str,
    index_version: int | None,
    made_from: str,
    seed: int,
) -> str:
    """The file a person labels: each reply with what its case expects, blank verdicts,
    and what its model read where supported is asked: once a case, or, on the
    recommendation path, where each turn called its own tools, with the reply."""
    path = sampled[0].judged.path if sampled else "knowledge"
    replies = []
    for number, item in enumerate(sampled, start=1):
        j = item.judged
        given = inputs[key_of(j)]
        entry: dict[str, Any] = {
            "id": f"JL-{number:02d}",
            "case_id": j.case_id,
            "candidate": j.candidate,
            "run": j.run,
            "reply_sha": reply_sha(j.reply),
            **({"history": [dict(turn) for turn in given.history]} if given.history else {}),
            "message": given.message,
            "expected_behavior": given.expected,
            "reply": _Text(j.reply),
        }
        if path == "list":
            entry["lists_shown"] = _lists_shown(given)
        if path == "recommend":
            entry["products_shown"] = list(given.evidence["shown"])
            if item.asks_supported:
                entry["tools"] = given.evidence["tools"]
        entry["expected"] = ""
        if item.asks_supported:
            entry["supported"] = ""
        entry["note"] = ""
        replies.append(entry)
    evidence = {}
    once_a_case = path != "recommend"
    for case_id in sorted({i.judged.case_id for i in sampled if i.asks_supported and once_a_case}):
        given = next(inputs[key_of(i.judged)] for i in sampled if i.judged.case_id == case_id)
        evidence[case_id] = _lists_found(given) if path == "list" else _passages(given)
    body = {
        "run": run_name,
        "index_version": index_version,
        "labelled_on": "",
        "replies": replies,
        "evidence": evidence,
    }
    made = f"# Made by wobot-eval judge-sample from {made_from}, seed {seed}.\n"
    dumped = yaml.dump(body, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=88)
    return HEADERS[path] + made + dumped


def _passages(given: JudgeInput) -> dict[str, Any]:
    seen = shown_evidence(given.evidence)
    return {
        "passages": [{"header": p["header"], "text": _Text(p["text"])} for p in seen["passages"]],
        **({"records": seen["records"]} if seen["records"] else {}),
    }


def _list_name(found: Mapping[str, Any]) -> dict[str, Any]:
    return {"list": found["list"], "filters": found["filters"]}


def _lists_shown(given: JudgeInput) -> list[dict[str, Any]]:
    found = {f["result_id"]: f for f in given.evidence["found"]}
    return [
        {**_list_name(found[s["result_id"]]), "shown": s["shown"]} for s in given.evidence["shown"]
    ]


def _lists_found(given: JudgeInput) -> dict[str, Any]:
    """Each list as the writer and the judge read it: its count, its span where it has
    one, and at most 50 items, since words can state what the items hold."""
    lists = []
    for found in given.evidence["found"]:
        shown = {**_list_name(found), "count": found["count"]}
        if "window" in found:
            shown["window"] = found["window"]
            shown["uncertain_count"] = found["uncertain_count"]
            shown["uncertain"] = found["uncertain"]
        shown["items"] = found["items"]
        lists.append(shown)
    return {"lists": lists}


# --- The labels ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Label:
    id: str
    key: Key
    reply_sha: str
    expected: bool | None  # None: not labelled yet
    supported: bool | None  # None: not asked, or not labelled yet
    asks_supported: bool
    note: str = ""


@dataclass(frozen=True)
class Labels:
    run: str
    labelled_on: str
    entries: list[Label] = field(default_factory=list)

    @property
    def blank(self) -> int:
        """Verdicts asked for and not given."""
        return sum(
            (e.expected is None) + (e.asks_supported and e.supported is None) for e in self.entries
        )


def _verdict(value: Any, where: str) -> bool | None:
    if value in (None, ""):
        return None
    if value not in VERDICTS:
        raise ValueError(f"{where}: write pass or fail, not {value!r}")
    return VERDICTS[value]


def read_labels(path: Path) -> Labels:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = []
    for entry in data["replies"]:
        where = f"{path.name} {entry['id']}"
        entries.append(
            Label(
                id=entry["id"],
                key=(entry["candidate"], entry["case_id"], entry["run"]),
                reply_sha=entry["reply_sha"],
                expected=_verdict(entry.get("expected"), f"{where} expected"),
                supported=_verdict(entry.get("supported"), f"{where} supported"),
                asks_supported="supported" in entry,
                note=entry.get("note") or "",
            )
        )
    return Labels(data["run"], data.get("labelled_on") or "", entries)


def any_filled(path: Path) -> bool:
    """Whether a labels file holds a person's work, which is never written over."""
    if not path.exists():
        return False
    labels = read_labels(path)
    return any(e.expected is not None or e.supported is not None for e in labels.entries)


# --- Agreement ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Agreement:
    both_pass: int = 0
    judge_passes: int = 0  # the judge passes what the person fails
    judge_fails: int = 0  # the judge fails what the person passes
    both_fail: int = 0

    @property
    def labelled(self) -> int:
        return self.both_pass + self.judge_passes + self.judge_fails + self.both_fail

    @property
    def share(self) -> float | None:
        n = self.labelled
        return (self.both_pass + self.both_fail) / n if n else None

    @property
    def kappa(self) -> float | None:
        """Cohen's kappa: (observed - chance) / (1 - chance). None when chance is
        certain, as when the judge and the person both pass every reply."""
        n = self.labelled
        if not n:
            return None
        judge_pass = (self.both_pass + self.judge_passes) / n
        person_pass = (self.both_pass + self.judge_fails) / n
        chance = judge_pass * person_pass + (1 - judge_pass) * (1 - person_pass)
        if math.isclose(chance, 1.0):
            return None
        return ((self.share or 0.0) - chance) / (1 - chance)


def agreement(pairs: Iterable[tuple[bool, bool]]) -> Agreement:
    """From (judge, person) verdicts, True for a pass."""
    counts = {(True, True): 0, (True, False): 0, (False, True): 0, (False, False): 0}
    for pair in pairs:
        counts[pair] += 1
    return Agreement(
        counts[True, True], counts[True, False], counts[False, True], counts[False, False]
    )


def judge_verdict(judged: Judged, compared: str) -> bool | None:
    if compared == "grounded":
        # A path without faithfulness has no grounded verdict: never a pass.
        if judged.error or not PATHS[judged.path].faithfulness:
            return None
        return judged.grounded is not False
    return judged.passed(compared)


@dataclass(frozen=True)
class Compared:
    """The judge against a person, one comparison: its counts and each disagreement."""

    name: str
    agreement: Agreement
    unlike: list[tuple[Judged, Label]]


@dataclass(frozen=True)
class AgainstLabels:
    comparisons: list[Compared]
    blank: int  # verdicts the person has not given
    stale: list[str]  # labels whose reply changed, or that this run does not hold


def against_labels(judged: Sequence[Judged], labels: Labels) -> AgainstLabels:
    by_key = {key_of(j): j for j in judged}
    stale = [
        e.id
        for e in labels.entries
        if e.key not in by_key or reply_sha(by_key[e.key].reply) != e.reply_sha
    ]
    current = [e for e in labels.entries if e.id not in stale]
    comparisons = []
    for name, label_field in COMPARED.items():
        pairs, unlike = [], []
        for entry in current:
            person = getattr(entry, label_field)
            verdict = judge_verdict(by_key[entry.key], name)
            if person is None or verdict is None:
                continue
            pairs.append((verdict, person))
            if verdict != person:
                unlike.append((by_key[entry.key], entry))
        comparisons.append(Compared(name, agreement(pairs), unlike))
    return AgainstLabels(comparisons, labels.blank, stale)
