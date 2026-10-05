from datetime import date

import pytest

from wobot.knowledge.lists import (
    QueryError,
    RecordQuery,
    Window,
    find_records,
    last_years_window,
    records_by_prefix,
)


async def keys(knowledge, query, version_id=None):
    async with knowledge.db.begin() as conn:
        found = await find_records(conn, version_id or knowledge.version_id, query)
    return [item.logical_key for item in found.items]


async def test_a_year_range_lists_every_lecture_in_it(knowledge):
    found = await keys(knowledge, RecordQuery("lecture", year_from=2025, year_to=2025))

    assert len(found) == 2
    assert await keys(knowledge, RecordQuery("lecture", year_from=2026)) == []


async def test_filters_narrow_by_category_and_degree(knowledge):
    keynotes = await keys(knowledge, RecordQuery("lecture", category="keynote"))
    masters = await keys(knowledge, RecordQuery("student", degree="master"))
    beds = await keys(knowledge, RecordQuery("product", category="1-3"))

    assert [key.split(":")[1] for key in keynotes] == ["keynote"]
    assert masters == ["student:master:王小明"]
    assert len(beds) == 2


async def test_students_come_in_graduation_order(knowledge):
    assert await keys(knowledge, RecordQuery("student")) == [
        "student:phd:劉大同",
        "student:master:王小明",
    ]


async def test_each_item_says_where_it_was_read(knowledge):
    async with knowledge.db.begin() as conn:
        (project,) = (await find_records(conn, knowledge.version_id, RecordQuery("project"))).items

    assert project.source_url == "https://www.grc.yzu.edu.tw/projects"
    assert project.fields["amount_ntd"] == 600000


async def test_a_version_that_holds_nothing_lists_nothing(knowledge):
    assert await keys(knowledge, RecordQuery("publication"), version_id=-1) == []


@pytest.mark.parametrize(
    "query",
    [
        RecordQuery("product", year_from=2020),
        RecordQuery("lecture", degree="phd"),
        RecordQuery("student", degree="bachelor"),
        RecordQuery("project", category="ITRI"),
        RecordQuery("product", category="9-9"),
        RecordQuery("product", window=Window(date(2021, 1, 1), date(2026, 1, 1))),
        RecordQuery("student", year_from=2020, window=Window(date(2021, 1, 1), date(2026, 1, 1))),
    ],
)
async def test_a_filter_the_kind_lacks_is_refused(knowledge, query):
    with pytest.raises(QueryError):
        await keys(knowledge, query)


async def test_a_prefix_finds_the_record_only_as_its_kind_and_version(knowledge):
    async with knowledge.db.begin() as conn:
        products = (await find_records(conn, knowledge.version_id, RecordQuery("product"))).items
        (lecture, _) = (
            await find_records(conn, knowledge.version_id, RecordQuery("lecture"))
        ).items
        prefixes = [products[0].record_id.hex[:8], lecture.record_id.hex[:8]]

        found = await records_by_prefix(conn, knowledge.version_id, "product", prefixes)
        elsewhere = await records_by_prefix(conn, -1, "product", prefixes)

    assert [item.record_id for item in found] == [products[0].record_id]
    assert found[0].fields == products[0].fields
    assert elsewhere == []


async def test_a_prefix_that_is_not_hex_is_refused(knowledge):
    async with knowledge.db.begin() as conn:
        with pytest.raises(QueryError):
            await records_by_prefix(conn, knowledge.version_id, "product", ["' OR 1=1"])


@pytest.mark.parametrize(
    ("today", "years", "start"),
    [
        (date(2026, 10, 3), 5, date(2021, 10, 3)),
        (date(2028, 2, 29), 5, date(2023, 2, 28)),  # no 29 February then: the month's end
        (date(2024, 2, 29), 4, date(2020, 2, 29)),
    ],
)
def test_the_last_years_count_back_by_date(today, years, start):
    assert last_years_window(today, years) == Window(start, today)


async def found(knowledge, query):
    async with knowledge.db.begin() as conn:
        result = await find_records(conn, knowledge.version_id, query)
    return [i.logical_key for i in result.items], [i.logical_key for i in result.uncertain]


async def test_a_window_compares_days_for_records_dated_to_the_day(knowledge):
    # The two lectures are on 2025-11-12 and 2025-12-05; both ends of a window count.
    both = await found(
        knowledge, RecordQuery("lecture", window=Window(date(2025, 11, 12), date(2025, 12, 5)))
    )
    one = await found(
        knowledge, RecordQuery("lecture", window=Window(date(2025, 11, 13), date(2026, 1, 1)))
    )

    assert (len(both[0]), both[1]) == (2, [])
    assert (len(one[0]), one[1]) == (1, [])


async def test_a_year_inside_the_window_counts_and_one_across_its_end_is_uncertain(knowledge):
    window = last_years_window(date(2026, 10, 3), 5)

    students = await found(knowledge, RecordQuery("student", window=window))
    publications = await found(knowledge, RecordQuery("publication", window=window))

    # 2022 lies inside; 2014 lies outside and is left out.
    assert students == (["student:master:王小明"], [])
    # 2026 runs past today, so a publication of that year may yet be in the future.
    assert (publications[0], len(publications[1])) == ([], 5)
