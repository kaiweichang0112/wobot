from contextlib import asynccontextmanager

import pytest

from wobot.agent.retrieval import (
    SEARCHED_CHUNKS,
    LookupFailed,
    evidence_keys,
    records_named,
    search_knowledge,
)


async def test_a_search_returns_the_closest_passages_with_their_records(knowledge):
    passages = await search_knowledge(
        knowledge.db, knowledge.embedder, knowledge.version_id, ["離床偵測", "bed exit"]
    )

    assert 0 < len(passages) <= SEARCHED_CHUNKS
    assert knowledge.embedder.calls[-1] == ["離床偵測", "bed exit"]  # one request, both
    first = passages[0]
    assert first["id"].startswith("k-") and len(first["id"]) == 10
    assert all(r["id"].startswith("r-") and r["source"] for r in first["records"])


@pytest.mark.parametrize(
    ("name", "keys"),
    [
        ("王小明", ["student:master:王小明"]),
        ("測試床墊", ["product:範例科技股份有限公司:測試床墊 tm-1"]),
        ("不存在的人", []),
    ],
)
async def test_a_name_finds_the_records_that_hold_it(knowledge, name, keys):
    records = await records_named(knowledge.db, knowledge.version_id, name)

    assert [r["key"] for r in records] == keys


async def test_a_named_product_shows_how_to_reach_its_company(knowledge):
    [product] = await records_named(knowledge.db, knowledge.version_id, "測試床墊")

    assert product["type"] == "product"
    assert product["fields"]["company_name"] == "範例科技股份有限公司"
    assert "features_text" in product["fields"]


async def test_a_name_held_by_too_many_records_singles_out_none(knowledge, monkeypatch):
    monkeypatch.setattr("wobot.agent.retrieval.NAMED_RECORDS", 2)

    # Three products are named 測試…: none of them is singled out.
    records = await records_named(knowledge.db, knowledge.version_id, "測試")

    assert [r for r in records if r["type"] == "product"] == []


class Unreachable:
    @asynccontextmanager
    async def begin(self):
        raise TimeoutError("the database is down")
        yield


async def test_an_unreachable_database_is_a_failure_not_an_empty_result(knowledge):
    with pytest.raises(LookupFailed):
        await search_knowledge(Unreachable(), knowledge.embedder, 1, ["床墊"])
    with pytest.raises(LookupFailed):
        await records_named(Unreachable(), 1, "王小明")


def test_the_evidence_keys_come_in_the_order_found_each_once():
    evidence = {
        "passages": [
            {"records": [{"key": "section:a"}, {"key": "product:b"}]},
            {"records": [{"key": "product:b"}]},
        ],
        "records": [{"key": "student:c"}],
    }

    assert evidence_keys(evidence) == ["section:a", "product:b", "student:c"]
