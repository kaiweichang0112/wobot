"""Gold labels: what a person read in the sources, and the records each label points at.

Labels name items the way a person sees them: a talk pasted from the page, or only its
title, a product by name and company, a student by name. Matching them to records compares
normalized text only, so a label the system cannot match is reported, never guessed: a
typo in the label, a piece of text that several items share, or an item ingestion lost or
garbled.
"""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from wobot.eval.corpus import Corpus, Item
from wobot.knowledge.records.text import key_text

# backend/eval/gold, beside the datasets that refer to it.
GOLD_DIR = Path(__file__).resolve().parents[3] / "eval" / "gold"
KINDS = ("speech", "publication", "product", "student", "project", "profile")
# A date as labels write it: YYYY-MM-DD, or YYYY/MM/DD as the pages do.
_DATE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
# Anchor labels that end a pasted line and name a link, not the item.
_TRAILING_LABELS = ("pdf", "link", "電子全文", "影音", "video", "全文")


class GoldError(Exception):
    """A gold file that cannot be read as labels."""


@dataclass(frozen=True)
class Ref:
    """One labelled item: how a person named it, and where the label is written."""

    kind: str
    value: Any  # text, or a mapping such as {"name": ..., "company": ...}
    where: str  # file:line or file:key, for messages
    expected: Mapping[str, Any] = field(default_factory=dict)  # fields to compare, if any


def match_text(text: str) -> str:
    """Text as compared: invisible characters out, NFKC, case-folded, spaces collapsed.

    A trailing link label ("PDF") is dropped too: the page shows it, records keep it as a
    link.
    """
    visible = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    normal = key_text(visible)
    stripped = True
    while stripped:
        stripped = False
        for label in _TRAILING_LABELS:
            if normal.endswith(label) and normal != label:
                normal, stripped = normal[: -len(label)].rstrip(), True
    return normal


# --- Reading the files ------------------------------------------------------------------


def read_lines(path: Path) -> list[tuple[int, str]]:
    """A .txt label file: one item per line, `#` comments and blank lines skipped."""
    lines = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip() and not line.lstrip().startswith("#"):
            lines.append((number, line.strip()))
    return lines


def read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise GoldError(f"{path.name}: not valid YAML: {error}") from error


def blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def line_refs(path: Path, kind: str) -> list[Ref]:
    """Refs from a .txt file. A product line is `name<TAB>company`, as pasted from Excel."""
    refs = []
    for number, line in read_lines(path):
        where = f"{path.name}:{number}"
        if kind == "product":
            cells = [cell.strip() for cell in line.split("\t") if cell.strip()]
            if len(cells) < 2:
                raise GoldError(f"{where}: expected product name, a tab, then company")
            refs.append(Ref("product", {"name": cells[0], "company": cells[1]}, where))
        else:
            refs.append(Ref(kind, line, where))
    return refs


# Templates come with blank entries to copy; a blank entry is one not labelled yet, and is
# skipped. A case with no labelled entry at all is reported as pending, never as a score.


def student_refs(path: Path, degree: str, years: Iterable[int]) -> list[Ref]:
    """Refs from a students file: year → graduates, each with the titles to compare."""
    data = read_yaml(path) or {}
    refs = []
    for year in years:
        for position, entry in enumerate(data.get(year) or [], start=1):
            where = f"{path.name}:{year}#{position}"
            if blank(entry.get("name")):
                continue
            expected = {
                "graduation_year": year,
                **{
                    key: _optional(entry.get(key)) for key in ("thesis_title_zh", "thesis_title_en")
                },
            }
            refs.append(Ref("student", {"name": entry["name"], "degree": degree}, where, expected))
    return refs


def project_refs(path: Path) -> list[Ref]:
    refs = []
    for position, entry in enumerate(read_yaml(path) or [], start=1):
        where = f"{path.name}#{position}"
        title = _optional(entry.get("title_zh")) or _optional(entry.get("title_en"))
        if title is None:
            continue
        amount = str(entry.get("amount_ntd") or "").replace(",", "").strip()
        if not amount.isdigit():
            raise GoldError(f"{where}: amount_ntd must be a whole number, got {amount!r}")
        expected = {
            "title_zh": _optional(entry.get("title_zh")),
            "title_en": _optional(entry.get("title_en")),
            "funder_raw": _optional(entry.get("funder")),
            "period_start": _iso_date(entry.get("period_start"), where),
            "period_end": _iso_date(entry.get("period_end"), where),
            "amount_ntd": int(amount),
        }
        refs.append(Ref("project", title, where, expected))
    return refs


def speech_field_refs(path: Path) -> list[Ref]:
    refs = []
    for position, entry in enumerate(read_yaml(path) or [], start=1):
        where = f"{path.name}#{position}"
        if blank(entry.get("entry")):
            raise GoldError(f"{where}: entry is empty")
        if blank(entry.get("title")):
            continue  # every talk has a title: without one, the entry is not labelled yet
        expected = {name: _optional(entry.get(name)) for name in ("title", "event", "location")}
        refs.append(Ref("speech", entry["entry"], where, expected))
    return refs


def named_refs(path: Path, case_id: str, key: str) -> list[Ref]:
    """Refs listed under a case in retrieval.yaml or single-item-questions.yaml."""
    data = read_yaml(path) or {}
    if case_id not in data:
        raise GoldError(f"{path.name}: no entry for {case_id}")
    refs = []
    for position, entry in enumerate(data[case_id].get(key) or [], start=1):
        where = f"{path.name}:{case_id}#{position}"
        if not isinstance(entry, dict) or len(entry) != 1:
            raise GoldError(f"{where}: name one item as `kind: value`, got {entry!r}")
        ((kind, value),) = entry.items()
        if kind not in KINDS:
            raise GoldError(f"{where}: unknown kind {kind!r}, expected one of {KINDS}")
        if isinstance(value, dict) and all(map(blank, value.values())) or blank(value):
            continue
        if isinstance(value, dict) and any(map(blank, value.values())):
            raise GoldError(f"{where}: {kind} is partly empty: {value!r}")
        refs.append(Ref(kind, value, where))
    return refs


def _iso_date(value: Any, where: str) -> str | None:
    """A label's date as YYYY-MM-DD. A date the page states but that does not exist is
    expected to stay empty in the record, as ingestion keeps such typos only as text; text
    that is not written as a date is an error, never an expected empty date."""
    text = _optional(str(value or ""))
    if text is None:
        raise GoldError(f"{where}: dates are required")
    if not (parts := _DATE.fullmatch(text)):
        raise GoldError(f"{where}: write dates as YYYY-MM-DD or YYYY/MM/DD, got {text!r}")
    try:
        return date(*map(int, parts.groups())).isoformat()
    except ValueError:
        return None


def _optional(value: Any) -> Any:
    if isinstance(value, str):
        value = " ".join(value.split())
    return None if blank(value) else value


# --- Matching labels to records ---------------------------------------------------------


@dataclass
class Resolved:
    ref: Ref
    keys: list[str]  # empty when nothing matches, or when the label fits several items
    problem: str | None = None  # why it is unresolved, when there is more to say


def resolve(refs: Iterable[Ref], corpus: Corpus) -> list[Resolved]:
    index = _Index(corpus)
    return [index.resolve(ref) for ref in refs]


class _Index:
    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus
        # The text a label may copy, per kind, as (normalized text, record key).
        self._texts: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for item in corpus.of("lecture"):
            self._add("speech", item.fields["entry_text"], item)
        for item in corpus.of("list_item"):
            if item.fields["list_kind"] == "publication":
                self._add("publication", item.fields["item_text"], item)
        for item in corpus.of("section"):
            for paragraph in item.fields["text"]:
                self._add("profile", paragraph, item)

    def _add(self, kind: str, text: str, item: Item) -> None:
        self._texts[kind].append((match_text(text), item.logical_key))

    def resolve(self, ref: Ref) -> Resolved:
        match ref.kind:
            case "speech" | "publication":
                return self._by_text(ref)
            case "product":
                name, company = key_text(ref.value["name"]), key_text(ref.value["company"])
                return Resolved(
                    ref,
                    [
                        item.logical_key
                        for item in self._corpus.of("product")
                        if key_text(item.fields["product_name"]) == name
                        and key_text(item.fields["company_name"]) == company
                    ],
                )
            case "student":
                name = ref.value if isinstance(ref.value, str) else ref.value["name"]
                degree = None if isinstance(ref.value, str) else ref.value.get("degree")
                year = ref.expected.get("graduation_year")
                return Resolved(
                    ref,
                    [
                        item.logical_key
                        for item in self._corpus.of("student")
                        if key_text(item.fields["name"]) == key_text(name)
                        and degree in (None, item.fields["degree"])
                        and year in (None, item.fields["graduation_year"])
                    ],
                )
            case "project":
                title = key_text(ref.value)
                return Resolved(
                    ref,
                    [
                        item.logical_key
                        for item in self._corpus.of("project")
                        if title in _project_titles(item.fields)
                    ],
                )
            case "profile":
                return self._profile(ref)
        raise GoldError(f"{ref.where}: unknown kind {ref.kind!r}")

    def _profile(self, ref: Ref) -> Resolved:
        """A heading names its section or its list; an item names itself; any other text
        names the section that holds it."""
        label = key_text(ref.value)
        listed = [
            item
            for item in self._corpus.of("list_item")
            if item.fields["list_kind"] == "profile_item"
        ]
        keys = [
            item.logical_key
            for item in self._corpus.of("section")
            if key_text(item.fields["heading"]) == label
        ]
        keys += [item.logical_key for item in listed if key_text(item.fields["category"]) == label]
        keys += [item.logical_key for item in listed if key_text(item.fields["item_text"]) == label]
        return Resolved(ref, keys) if keys else self._by_text(ref)

    def _by_text(self, ref: Ref) -> Resolved:
        """The items whose text is the label, else the one item whose text holds it: a
        label may copy a whole line or only a piece no other item has, such as a title."""
        label = match_text(ref.value)
        texts = self._texts[ref.kind]
        if same := [key for text, key in texts if text == label]:
            return Resolved(ref, same)
        holding = sorted({key for text, key in texts if label and label in text})
        if len(holding) > 1:
            return Resolved(ref, [], f"fits {len(holding)} items; copy more of the line")
        return Resolved(ref, holding)


def _project_titles(fields: Mapping[str, Any]) -> set[str]:
    """A project's titles, alone or as the page shows them, Chinese then English."""
    zh, en = fields["title_zh"] or "", fields["title_en"] or ""
    return {key_text(zh), key_text(en), key_text(f"{zh} {en}")} - {""}
