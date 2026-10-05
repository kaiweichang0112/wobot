import json
import uuid
from datetime import UTC, datetime

import httpx
import openai
import pytest
from langchain.tools import ToolRuntime
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from tests.knowledge.fakes import FakeEmbedder
from wobot.agent.tools import (
    PRODUCT_CATEGORIES,
    SEARCHED_CHUNKS,
    SHOWN_ITEMS,
    RecordsResult,
    ToolStatus,
    TurnContext,
    build_tools,
    records_content,
)
from wobot.knowledge.lists import KINDS, Listed, RecordQuery
from wobot.knowledge.models import Chunk, ChunkRecord, IndexVersionChunk, Record


async def call(tools, name, args, version_id):
    """Run one tool the way the agent's tool node does, with this turn's context."""
    (chosen,) = [t for t in tools if t.name == name]
    context = TurnContext("account-1", datetime(2026, 10, 3, tzinfo=UTC), version_id)
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="call-1",
        store=None,
    )
    return await chosen.ainvoke(
        {"type": "tool_call", "id": "call-1", "name": name, "args": {**args, "runtime": runtime}}
    )


class Down:
    """A database that refuses every connection."""

    def begin(self):
        raise OperationalError("select 1", {}, ConnectionRefusedError())


async def product_passage(knowledge, name):
    """The text of the version's chunk about one product: searching for it finds it first."""
    async with knowledge.db.begin() as conn:
        return await conn.scalar(
            select(Chunk.embedding_input)
            .join(IndexVersionChunk, IndexVersionChunk.chunk_id == Chunk.chunk_id)
            .join(ChunkRecord, ChunkRecord.chunk_id == Chunk.chunk_id)
            .join(Record, Record.record_id == ChunkRecord.record_id)
            .where(
                IndexVersionChunk.index_version_id == knowledge.version_id,
                Record.record_type == "product",
                Chunk.body.contains(name),
            )
        )


async def test_query_records_shows_the_model_short_ids_and_keeps_the_items(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)

    message = await call(tools, "query_records", {"record_type": "lecture"}, knowledge.version_id)

    content = json.loads(message.content)
    assert content["status"] == "found"
    assert content["count"] == content["shown"] == 2
    assert all(item["id"].startswith("r-") and len(item["id"]) == 10 for item in content["items"])
    assert len(message.artifact.items) == 2
    assert message.artifact.result_id == content["result_id"]


async def test_nothing_in_range_is_no_match(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)

    message = await call(
        tools, "query_records", {"record_type": "project", "year_from": 2030}, knowledge.version_id
    )

    assert message.artifact.status is ToolStatus.NO_MATCH
    assert json.loads(message.content)["count"] == 0


async def test_a_refused_filter_goes_back_to_the_model_as_an_error(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)

    message = await call(
        tools, "query_records", {"record_type": "product", "year_from": 2020}, knowledge.version_id
    )

    assert message.status == "error"
    assert "no year" in message.content


async def test_the_last_years_come_from_the_turns_date(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)

    message = await call(
        tools,
        "query_records",
        {"record_type": "publication", "last_years": 5},
        knowledge.version_id,
    )

    content = json.loads(message.content)
    # The turn is on 2026-10-03: every publication is of 2026, which runs past that day.
    assert content["window"] == {"from": "2021-10-03", "to": "2026-10-03"}
    assert (content["status"], content["count"], content["uncertain_count"]) == ("found", 0, 5)
    assert len(message.artifact.uncertain) == 5


async def test_an_unreachable_database_is_failed_not_no_match():
    tools = build_tools(Down(), FakeEmbedder())

    message = await call(tools, "query_records", {"record_type": "student"}, 1)

    assert message.artifact.status is ToolStatus.FAILED
    assert json.loads(message.content) == {"status": "failed", "retryable": True}


def test_a_long_list_shows_the_model_part_and_keeps_all():
    items = [
        Listed(
            uuid.uuid4(),
            f"publication:{n}",
            {"year": 2024, "category": "Patents", "item_text": str(n)},
            "",
            {},
        )
        for n in range(SHOWN_ITEMS + 10)
    ]
    result = RecordsResult(ToolStatus.FOUND, "q-1", RecordQuery("publication"), items)

    content = json.loads(records_content(result))

    assert (content["count"], content["shown"]) == (SHOWN_ITEMS + 10, SHOWN_ITEMS)


def test_the_model_sees_no_runtime_and_every_category_it_may_use():
    tools = {t.name: t for t in build_tools(db=None, embedder=None)}
    schemas = {name: t.tool_call_schema.model_json_schema() for name, t in tools.items()}

    assert all("runtime" not in schema["properties"] for schema in schemas.values())
    assert schemas["query_records"]["properties"]["record_type"]["enum"] == list(KINDS)
    assert set(PRODUCT_CATEGORIES) == KINDS["product"].categories


async def test_search_shows_passages_and_the_products_they_are_about(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)
    passage = await product_passage(knowledge, "測試地墊 TM-2")

    message = await call(tools, "search_knowledge", {"queries": [passage]}, knowledge.version_id)

    content = json.loads(message.content)
    first = content["items"][0]
    assert content["status"] == "found"
    assert len(content["items"]) == SEARCHED_CHUNKS
    assert first["id"].startswith("k-") and "測試地墊 TM-2" in first["text"]
    (member,) = message.artifact.evidence[0].members
    assert first["products"] == [f"r-{member.record_id.hex[:8]}"]
    assert knowledge.embedder.calls[-1] == [passage]  # one request, after ingestion's


async def test_each_wording_is_searched_and_the_results_fused(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)
    mat = await product_passage(knowledge, "測試地墊 TM-2")
    wordings = [mat, "smart floor mat"]

    message = await call(tools, "search_knowledge", {"queries": wordings}, knowledge.version_id)

    assert knowledge.embedder.calls[-1] == wordings  # one request for every wording
    assert message.artifact.queries == wordings
    assert len(message.artifact.evidence) == SEARCHED_CHUNKS


@pytest.mark.parametrize("queries", [[], ["a", "b", "c", "d"]])
async def test_too_few_or_too_many_wordings_are_refused(knowledge, queries):
    tools = build_tools(knowledge.db, knowledge.embedder)

    message = await call(tools, "search_knowledge", {"queries": queries}, knowledge.version_id)

    assert message.status == "error"


async def test_passages_about_no_product_list_none(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)

    message = await call(
        tools, "search_knowledge", {"queries": ["碩士畢業生"]}, knowledge.version_id
    )

    for item, evidence in zip(
        json.loads(message.content)["items"], message.artifact.evidence, strict=True
    ):
        is_product = any(m.record_type == "product" for m in evidence.members)
        assert ("products" in item) == is_product


async def test_a_version_without_passages_is_no_match(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)

    message = await call(tools, "search_knowledge", {"queries": ["GRC"]}, -1)

    assert message.artifact.status is ToolStatus.NO_MATCH
    assert json.loads(message.content) == {"status": "no_match", "items": []}


async def test_an_unreachable_provider_is_failed():
    class Unreachable(FakeEmbedder):
        async def embed(self, texts):
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://api.test"))

    tools = build_tools(Down(), Unreachable())

    message = await call(tools, "search_knowledge", {"queries": ["GRC"]}, 1)

    assert message.artifact.status is ToolStatus.FAILED
    assert json.loads(message.content) == {"status": "failed", "retryable": True}


async def test_details_show_every_field_and_name_what_is_missing(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)
    listed = await call(tools, "query_records", {"record_type": "product"}, knowledge.version_id)
    wanted = json.loads(listed.content)["items"][0]["id"]

    message = await call(
        tools, "get_product_details", {"product_ids": [wanted, "r-00000000"]}, knowledge.version_id
    )

    content = json.loads(message.content)
    (item,) = content["items"]
    assert content["status"] == "found"
    assert item["id"] == wanted and item["company_name"] and item["features_text"]
    assert content["missing"] == ["r-00000000"]


async def test_a_passage_product_can_be_detailed(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)
    passage = await product_passage(knowledge, "測試地墊 TM-2")
    searched = await call(tools, "search_knowledge", {"queries": [passage]}, knowledge.version_id)
    named = json.loads(searched.content)["items"][0]["products"]

    message = await call(tools, "get_product_details", {"product_ids": named}, knowledge.version_id)

    (product,) = message.artifact.products
    assert product.fields["product_name"] == "測試地墊 TM-2"


async def test_a_record_that_is_not_a_product_is_missing(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)
    lectures = await call(tools, "query_records", {"record_type": "lecture"}, knowledge.version_id)
    lecture = json.loads(lectures.content)["items"][0]["id"]

    message = await call(
        tools, "get_product_details", {"product_ids": [lecture]}, knowledge.version_id
    )

    assert message.artifact.status is ToolStatus.NO_MATCH
    assert json.loads(message.content)["missing"] == [lecture]


@pytest.mark.parametrize(
    "product_ids",
    [[], ["測試地墊 TM-2"], ["k-1a2b3c4d"], [f"r-{n:08x}" for n in range(6)]],
)
async def test_ids_the_model_could_not_have_been_given_are_refused(knowledge, product_ids):
    tools = build_tools(knowledge.db, knowledge.embedder)

    message = await call(
        tools, "get_product_details", {"product_ids": product_ids}, knowledge.version_id
    )

    assert message.status == "error"


async def test_details_from_an_unreachable_database_are_failed():
    tools = build_tools(Down(), FakeEmbedder())

    message = await call(tools, "get_product_details", {"product_ids": ["r-1a2b3c4d"]}, 1)

    assert message.artifact.status is ToolStatus.FAILED
    assert json.loads(message.content) == {"status": "failed", "retryable": True}


async def test_a_name_finds_its_record_and_names_its_own_result(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)
    everyone = {"record_type": "student"}

    named = await call(
        tools, "query_records", {**everyone, "contains": "王小明"}, knowledge.version_id
    )
    whole = await call(tools, "query_records", everyone, knowledge.version_id)

    content = json.loads(named.content)
    assert (content["count"], content["items"][0]["name"]) == (1, "王小明")
    assert content["result_id"] != json.loads(whole.content)["result_id"]
