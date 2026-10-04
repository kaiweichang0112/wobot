import pytest

from wobot.knowledge.lists import QueryError, RecordQuery, find_records, records_by_prefix


async def keys(knowledge, query, version_id=None):
    async with knowledge.db.begin() as conn:
        found = await find_records(conn, version_id or knowledge.version_id, query)
    return [item.logical_key for item in found]


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
        (project,) = await find_records(conn, knowledge.version_id, RecordQuery("project"))

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
    ],
)
async def test_a_filter_the_kind_lacks_is_refused(knowledge, query):
    with pytest.raises(QueryError):
        await keys(knowledge, query)


async def test_a_prefix_finds_the_record_only_as_its_kind_and_version(knowledge):
    async with knowledge.db.begin() as conn:
        products = await find_records(conn, knowledge.version_id, RecordQuery("product"))
        (lecture, _) = await find_records(conn, knowledge.version_id, RecordQuery("lecture"))
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
