"""Complete lists from one index version's typed records: every match, never a top k."""

import calendar
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.knowledge.models import (
    IndexVersionRecord,
    LectureRecord,
    ListItemRecord,
    ProductRecord,
    ProjectRecord,
    SourceSnapshot,
    StudentRecord,
)

ListKind = Literal["lecture", "student", "project", "publication", "product"]


class QueryError(ValueError):
    """A filter the kind does not have, or a value it never takes."""


@dataclass(frozen=True)
class Window:
    """A span of dates, both ends included."""

    start: date
    end: date


def last_years_window(today: date, years: int) -> Window:
    """The last `years` years up to today, counted back by date: a 29 February with no
    match that many years earlier starts on the 28th, the month's last day."""
    year = today.year - years
    day = min(today.day, calendar.monthrange(year, today.month)[1])
    return Window(date(year, today.month, day), today)


@dataclass(frozen=True)
class RecordQuery:
    kind: ListKind
    year_from: int | None = None  # inclusive
    year_to: int | None = None  # inclusive
    degree: str | None = None
    category: str | None = None
    window: Window | None = None  # by date, unlike the years
    contains: str | None = None  # text the record's names or titles hold, as written


@dataclass(frozen=True)
class Listed:
    record_id: uuid.UUID
    logical_key: str
    fields: dict[str, Any]  # the typed table's columns
    source_url: str  # the page or file the record was read from
    locator: dict[str, Any]  # where in it, such as a row or a position


@dataclass(frozen=True)
class RecordList:
    """What a query found. With a window, items are surely in it; uncertain ones may be,
    since their date is only a year that straddles an end, or unknown (`17`)."""

    items: list[Listed] = field(default_factory=list)
    uncertain: list[Listed] = field(default_factory=list)


# The days a record may fall on, first and last; None when its date is unknown.
Span = tuple[date, date] | None


def _year_span(year: int | None) -> Span:
    return None if year is None else (date(year, 1, 1), date(year, 12, 31))


def _lecture_span(fields: Mapping[str, Any]) -> Span:
    day = fields["lecture_date"]
    if fields["date_precision"] == "day":
        return day, day
    if fields["date_precision"] == "month":
        return day.replace(day=1), day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return _year_span(fields["year"])


@dataclass(frozen=True)
class _Kind:
    table: Any
    year: Any = None  # the column year filters compare
    category: Any = None
    categories: frozenset[str] = frozenset()  # the values category may take
    order: tuple[Any, ...] = ()
    only: tuple[Any, ...] = ()  # conditions every query of the kind adds
    text: tuple[Any, ...] = ()  # the columns `contains` looks in
    span: Callable[[Mapping[str, Any]], Span] | None = None  # for windows; None: undated


# An allowlist: a filter reaches SQL only through a column named here, and a category only
# as one of the values listed. The catalog's categories as of the 2026 workbook.
KINDS: dict[str, _Kind] = {
    "lecture": _Kind(
        LectureRecord,
        year=LectureRecord.year,
        category=LectureRecord.category,
        categories=frozenset({"keynote", "invited"}),
        order=(LectureRecord.lecture_date,),
        span=_lecture_span,
        # The entry as listed holds the title, event and place.
        text=(LectureRecord.entry_text,),
    ),
    "student": _Kind(
        StudentRecord,
        year=StudentRecord.graduation_year,
        order=(StudentRecord.graduation_year,),
        span=lambda fields: _year_span(fields["graduation_year"]),
        text=(StudentRecord.name, StudentRecord.thesis_title_zh, StudentRecord.thesis_title_en),
    ),
    "project": _Kind(
        ProjectRecord,
        year=ProjectRecord.year,
        order=(ProjectRecord.year,),
        span=lambda fields: _year_span(fields["year"]),
        text=(ProjectRecord.title_zh, ProjectRecord.title_en, ProjectRecord.funder_raw),
    ),
    "publication": _Kind(
        ListItemRecord,
        year=ListItemRecord.year,
        category=ListItemRecord.category,
        categories=frozenset(
            {
                "Journal papers",
                "Conference papers",
                "Books / book chapters",
                "Patents",
                "General publications",
            }
        ),
        order=(ListItemRecord.year,),
        only=(ListItemRecord.list_kind == "publication",),
        span=lambda fields: _year_span(fields["year"]),
        text=(ListItemRecord.item_text,),
    ),
    "product": _Kind(
        ProductRecord,
        category=ProductRecord.category_l2_code,
        categories=frozenset(
            {"1-1", "1-2", "1-3", "1-4", "1-5", "2-1", "2-2", "2-3", "2-4"}
            | {"3-1", "3-2", "4-1", "4-2", "4-3", "4-4", "5-1", "5-2"}
        ),
        order=(ProductRecord.category_l2_code, ProductRecord.product_name),
        text=(ProductRecord.product_name, ProductRecord.company_name),
    ),
}
DEGREES = frozenset({"master", "phd"})
# Long enough for a title, short enough that it is not a question pasted in.
CONTAINS_CHARS = 100
# The start of a record ID, as the agent shows one: 8 of its 32 hex digits.
_PREFIX = re.compile(r"[0-9a-f]{8}")


async def find_records(conn: AsyncConnection, version_id: int, query: RecordQuery) -> RecordList:
    """Every record of the version that matches, in a stable order."""
    kind = KINDS[query.kind]
    conditions = [IndexVersionRecord.index_version_id == version_id, *kind.only]
    if query.window is not None:
        if kind.span is None:
            raise QueryError(f"{query.kind} has no date to filter by")
        if query.year_from is not None or query.year_to is not None:
            raise QueryError("give either a window or years, not both")
    if query.year_from is not None or query.year_to is not None:
        if kind.year is None:
            raise QueryError(f"{query.kind} has no year to filter by")
        if query.year_from is not None:
            conditions.append(kind.year >= query.year_from)
        if query.year_to is not None:
            conditions.append(kind.year <= query.year_to)
    if query.degree is not None:
        if query.kind != "student":
            raise QueryError("only students have a degree")
        if query.degree not in DEGREES:
            raise QueryError(f"degree is one of {sorted(DEGREES)}")
        conditions.append(StudentRecord.degree == query.degree)
    if query.category is not None:
        if kind.category is None:
            raise QueryError(f"{query.kind} has no category")
        if query.category not in kind.categories:
            raise QueryError(f"a {query.kind} category is one of {sorted(kind.categories)}")
        conditions.append(kind.category == query.category)
    if query.contains is not None:
        conditions.append(_contains(kind, query.contains))

    rows = await conn.execute(
        _listed(kind).where(*conditions).order_by(*kind.order, IndexVersionRecord.logical_key)
    )
    items = [_item(kind, row) for row in rows]
    if query.window is None:
        return RecordList(items)
    found = RecordList()
    for item in items:
        place = _place(kind.span(item.fields), query.window)
        if place == "within":
            found.items.append(item)
        elif place == "uncertain":
            found.uncertain.append(item)
    return found


def _contains(kind: _Kind, text: str) -> Any:
    """The text inside any of the kind's names or titles, ignoring case: an exact lookup
    where semantic search ranks a name no higher than any other in its list."""
    text = text.strip()
    if not text or len(text) > CONTAINS_CHARS:
        raise QueryError(f"contains takes 1 to {CONTAINS_CHARS} characters")
    # Taken as written: a % or _ in it is not a wildcard.
    pattern = "%" + re.sub(r"([\\%_])", r"\\\1", text) + "%"
    return or_(*(column.ilike(pattern, escape="\\") for column in kind.text))


def _place(span: Span, window: Window) -> Literal["within", "uncertain", "outside"]:
    """A record is in a window when every day it may fall on is; out when none is."""
    if span is None:
        return "uncertain"
    first, last = span
    if last < window.start or first > window.end:
        return "outside"
    if window.start <= first and last <= window.end:
        return "within"
    return "uncertain"


async def records_by_prefix(
    conn: AsyncConnection, version_id: int, kind_name: ListKind, prefixes: Sequence[str]
) -> list[Listed]:
    """The kind's records of the version whose IDs start with one of the prefixes.

    A prefix is the first 8 hex digits of an ID. Each is a range of the version's primary
    key, so the lookup reads the index instead of every member.
    """
    if bad := [prefix for prefix in prefixes if not _PREFIX.fullmatch(prefix)]:
        raise QueryError(f"not 8 lowercase hex digits: {bad}")
    kind = KINDS[kind_name]
    ranges = [
        IndexVersionRecord.record_id.between(
            uuid.UUID(prefix + "0" * 24), uuid.UUID(prefix + "f" * 24)
        )
        for prefix in prefixes
    ]
    rows = await conn.execute(
        _listed(kind)
        .where(IndexVersionRecord.index_version_id == version_id, *kind.only, or_(*ranges))
        .order_by(IndexVersionRecord.record_id)
    )
    return [_item(kind, row) for row in rows]


def _columns(kind: _Kind) -> list[Any]:
    return [column for column in kind.table.__table__.c if column.name != "record_id"]


def _listed(kind: _Kind) -> Select:
    """The version's members of the kind, with their typed columns and where they were read.

    Joining the typed table keeps only records of the kind.
    """
    return (
        select(
            IndexVersionRecord.record_id,
            IndexVersionRecord.logical_key,
            IndexVersionRecord.locator.label("member_locator"),
            SourceSnapshot.locator.label("source_url"),
            *_columns(kind),
        )
        .join(kind.table, kind.table.record_id == IndexVersionRecord.record_id)
        .join(SourceSnapshot, SourceSnapshot.snapshot_id == IndexVersionRecord.snapshot_id)
    )


def _item(kind: _Kind, row: Any) -> Listed:
    return Listed(
        record_id=row.record_id,
        logical_key=row.logical_key,
        fields={column.name: row._mapping[column.name] for column in _columns(kind)},
        source_url=row.source_url,
        locator=row.member_locator,
    )
