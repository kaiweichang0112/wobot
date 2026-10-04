"""Complete lists from one index version's typed records: every match, never a top k."""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
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
class RecordQuery:
    kind: ListKind
    year_from: int | None = None  # inclusive
    year_to: int | None = None  # inclusive
    degree: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class Listed:
    record_id: uuid.UUID
    logical_key: str
    fields: dict[str, Any]  # the typed table's columns
    source_url: str  # the page or file the record was read from
    locator: dict[str, Any]  # where in it, such as a row or a position


@dataclass(frozen=True)
class _Kind:
    table: Any
    year: Any = None  # the column year filters compare
    category: Any = None
    categories: frozenset[str] = frozenset()  # the values category may take
    order: tuple[Any, ...] = ()
    only: tuple[Any, ...] = ()  # conditions every query of the kind adds


# An allowlist: a filter reaches SQL only through a column named here, and a category only
# as one of the values listed. The catalog's categories as of the 2026 workbook.
KINDS: dict[str, _Kind] = {
    "lecture": _Kind(
        LectureRecord,
        year=LectureRecord.year,
        category=LectureRecord.category,
        categories=frozenset({"keynote", "invited"}),
        order=(LectureRecord.lecture_date,),
    ),
    "student": _Kind(
        StudentRecord, year=StudentRecord.graduation_year, order=(StudentRecord.graduation_year,)
    ),
    "project": _Kind(ProjectRecord, year=ProjectRecord.year, order=(ProjectRecord.year,)),
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
    ),
    "product": _Kind(
        ProductRecord,
        category=ProductRecord.category_l2_code,
        categories=frozenset(
            {"1-1", "1-2", "1-3", "1-4", "1-5", "2-1", "2-2", "2-3", "2-4"}
            | {"3-1", "3-2", "4-1", "4-2", "4-3", "4-4", "5-1", "5-2"}
        ),
        order=(ProductRecord.category_l2_code, ProductRecord.product_name),
    ),
}
DEGREES = frozenset({"master", "phd"})
# The start of a record ID, as the agent shows one: 8 of its 32 hex digits.
_PREFIX = re.compile(r"[0-9a-f]{8}")


async def find_records(conn: AsyncConnection, version_id: int, query: RecordQuery) -> list[Listed]:
    """Every record of the version that matches, in a stable order."""
    kind = KINDS[query.kind]
    conditions = [IndexVersionRecord.index_version_id == version_id, *kind.only]
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

    rows = await conn.execute(
        _listed(kind).where(*conditions).order_by(*kind.order, IndexVersionRecord.logical_key)
    )
    return [_item(kind, row) for row in rows]


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
