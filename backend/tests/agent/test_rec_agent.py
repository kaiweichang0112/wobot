import hashlib
import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from tests.agent.test_list_agent import Unreachable
from wobot.agent import rec_agent
from wobot.agent.rec_agent import (
    REC_TOOLS,
    RECENT_MESSAGES,
    TOOLS_BY_NAME,
    RecommendProducts,
    SearchProducts,
    own_words,
    read_call,
    rec_agent_prompt,
    search_products,
)
from wobot.agent.retrieval import LookupFailed


def call(name: str, **args) -> dict:
    return {"name": name, "args": args, "id": "call-1"}


async def search(knowledge, function: str, evidence=None):
    tool_call = call("search_products", functions=[function])
    return await search_products(
        knowledge.db,
        knowledge.embedder,
        knowledge.version_id,
        tool_call,
        SearchProducts(functions=[function]),
        evidence or {},
    )


def test_the_prompt_version_moves_with_the_prompt():
    tools = [convert_to_openai_tool(tool) for tool in REC_TOOLS]
    prompt = json.dumps([rec_agent.INSTRUCTIONS, tools], ensure_ascii=False)
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    assert (rec_agent.PROMPT_VERSION, fingerprint) == (5, "d64a4b2981f8")


def test_the_agent_searches_and_recommends():
    params = {
        name: set(convert_to_openai_tool(tool)["function"]["parameters"]["properties"])
        for name, tool in TOOLS_BY_NAME.items()
    }

    assert params == {
        "search_products": {"functions"},
        "recommend_products": {"needs", "products", "count", "intro"},
    }


def test_the_agent_reads_its_name_and_the_last_messages():
    messages = [HumanMessage(f"第 {n} 句") for n in range(RECENT_MESSAGES + 2)]

    system, *shown = rec_agent_prompt(messages, "小沃")

    assert isinstance(system, SystemMessage) and system.text.endswith('chatbot_name: "小沃"')
    assert shown == messages[-RECENT_MESSAGES:]


async def test_a_search_finds_catalog_products_with_their_text(knowledge):
    answer, evidence = await search(knowledge, "離床偵測")

    shown = json.loads(answer.content)["products"]
    assert shown and all(p["id"].startswith("r-") for p in shown)  # none of GRC's site
    bed = next(p for p in shown if "測試床墊 TM-1" in p["text"])
    assert "離床偵測" in bed["text"] and "臥床監測" in bed["category"]
    kept = evidence[bed["id"]]
    assert kept["kind"] == "product" and kept["rank"] == shown.index(bed) + 1
    assert kept["fields"] == {
        "product_name": "測試床墊 TM-1",
        "company_name": "範例科技股份有限公司",
        "contact_phone": "02-0000-0000",
        "product_url": "https://example.com/tm-1",
    }


async def test_a_product_keeps_its_best_rank_of_the_turn(knowledge):
    _, first = await search(knowledge, "離床偵測")
    worse = {h: e | {"rank": 99} for h, e in first.items()}
    better = {h: e | {"rank": 1} for h, e in first.items()}

    _, after_worse = await search(knowledge, "離床偵測", worse)
    _, after_better = await search(knowledge, "離床偵測", better)

    assert {h: e["rank"] for h, e in after_worse.items()} == {
        h: e["rank"] for h, e in first.items()
    }
    assert {e["rank"] for e in after_better.values()} == {1}


def test_a_call_is_read_as_its_tool():
    found = read_call(call("search_products", functions=["離床預警", "通知照護人員"]))
    recommended = read_call(
        call(
            "recommend_products",
            needs=[{"text": "離床預警", "kind": "function"}],
            products=[
                {
                    "product_id": "r-1a2b3c4d",
                    "checks": [{"need": 1, "status": "supported"}],
                    "reason": "床墊下感測。",
                }
            ],
            count=1,
            intro="這項產品符合你的需求。",
        )
    )

    assert found == SearchProducts(functions=["離床預警", "通知照護人員"])
    assert found.query == "離床預警、通知照護人員"
    assert isinstance(recommended, RecommendProducts) and recommended.count == 1


@pytest.mark.parametrize(
    ("tool_call", "reason"),
    [
        (call("search_products", functions=[]), "functions"),
        (call("search_products", query="離床預警"), "functions"),
        (call("recommend_products", needs=[], products=[], count=1, intro=""), "needs"),
        (call("get_reviews"), "there is no tool get_reviews"),
    ],
)
def test_a_refused_call_tells_the_model_why(tool_call, reason):
    refused = read_call(tool_call)

    assert isinstance(refused, ToolMessage)
    assert refused.status == "error" and reason in refused.content


async def test_an_unreachable_database_fails_the_search(knowledge):
    with pytest.raises(LookupFailed):
        await search_products(
            Unreachable(),
            knowledge.embedder,
            knowledge.version_id,
            call("search_products", functions=["離床偵測"]),
            SearchProducts(functions=["離床偵測"]),
            {},
        )


@pytest.mark.parametrize(
    "leaked",
    [
        # As gpt-6-luna:none wrote it in a live run (2026-10-08).
        "你想找哪方面的產品？\n”】【assistant to=functions.search_products? No.你想找…",
        "你想找哪方面的產品？to=functions.search_products {}",
        "你想找哪方面的產品？<|call|>",
    ],
)
def test_a_reply_is_cut_where_tool_call_syntax_leaks(leaked):
    assert own_words(leaked) == "你想找哪方面的產品？"


def test_a_reply_without_leaks_is_kept():
    assert own_words(" 請問是在家裡用，還是在照護機構？\n") == "請問是在家裡用，還是在照護機構？"
