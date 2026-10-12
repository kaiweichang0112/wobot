import hashlib
import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from tests.agent.test_find import bed_exit
from wobot.agent import rec_agent
from wobot.agent.find import FindProducts, find
from wobot.agent.rec_agent import (
    PRODUCTS_READ,
    REC_TOOLS,
    RECENT_MESSAGES,
    TEXTS_READ,
    TOOLS_BY_NAME,
    ShowProducts,
    found_content,
    found_evidence,
    own_words,
    read_call,
    rec_agent_prompt,
)


def call(name: str, **args) -> dict:
    return {"name": name, "args": args, "id": "call-1"}


def test_the_prompt_version_moves_with_the_prompt():
    tools = [convert_to_openai_tool(tool) for tool in REC_TOOLS]
    prompt = json.dumps([rec_agent.INSTRUCTIONS, tools], ensure_ascii=False)
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    assert (rec_agent.PROMPT_VERSION, fingerprint) == (10, "85f5fa48c899")


def test_the_agent_finds_and_shows():
    params = {
        name: set(convert_to_openai_tool(tool)["function"]["parameters"]["properties"])
        for name, tool in TOOLS_BY_NAME.items()
    }

    assert params == {
        "find_products": {"functions", "must", "must_not", "other", "count", "no_preference"},
        "show_products": {"products", "intro"},
    }
    # The agent reads the vocabulary the extraction labels with.
    assert (
        "- 離床偵測（離床預警、離床警示、離床提醒、離床通知、在床狀態）" in rec_agent.INSTRUCTIONS
    )


def test_the_agent_reads_its_name_and_the_last_messages():
    messages = [HumanMessage(f"第 {n} 句") for n in range(RECENT_MESSAGES + 2)]

    system, *shown = rec_agent_prompt(messages, "小沃")

    assert isinstance(system, SystemMessage) and system.text.endswith('chatbot_name: "小沃"')
    assert shown == messages[-RECENT_MESSAGES:]


def test_a_call_is_read_as_its_tool():
    found = read_call(
        call("find_products", functions=["離床偵測"], must_not={"form": ["worn"]}, count=1)
    )
    shown = read_call(call("show_products", products=["r-1a2b3c4d"], intro="這項符合。"))

    assert isinstance(found, FindProducts) and found.query.must_not == {"form": ("worn",)}
    assert shown == ShowProducts(products=["r-1a2b3c4d"], intro="這項符合。")


@pytest.mark.parametrize(
    ("tool_call", "reason"),
    [
        (call("find_products", functions=["離床預警"]), "functions.0"),  # an alias, not a name
        (call("find_products"), "name a function"),
        (call("show_products", products=[], intro=""), "products"),
        (call("search_products", functions=["離床偵測"]), "there is no tool search_products"),
    ],
)
def test_a_refused_call_tells_the_model_why(tool_call, reason):
    refused = read_call(tool_call)

    assert isinstance(refused, ToolMessage)
    assert refused.status == "error" and reason in refused.content


# --- What a find leaves -------------------------------------------------------------------

CATALOG = [
    bed_exit(f"P{n}", places={"home": "居家"} if n % 2 else {}, text=f"第 {n} 項")
    for n in range(12)
]


def test_the_agent_reads_the_best_fitting_products_and_what_splits_them():
    args = FindProducts(functions=["離床偵測"], must={"places": ["home"]})
    found = find(CATALOG, args.query, args.count, {})

    content = found_content(found, args)
    kept = found_evidence(found, args)

    assert content["matched"] == 12 and len(content["products"]) == PRODUCTS_READ
    first = content["products"][0]
    assert first == {
        "id": "r-P1",
        "name": "P1",
        "company": "Co",
        "needs": {"離床偵測": "離床提醒", "places=home": "居家"},
    }
    assert content["decisive"] == [] and content["gaps"] == []
    assert kept["needs"] == ["離床偵測", "places=home"] and kept["count"] is None
    assert kept["matched"] == 12
    assert kept["reads_text"] is False and len(kept["products"]) == PRODUCTS_READ
    assert kept["products"][0]["fields"] == {"product_name": "P1", "company_name": "Co"}
    json.dumps(kept)  # plain values, as the checkpointer stores them


def test_a_need_outside_the_vocabulary_has_the_agent_read_fewer_products_text():
    args = FindProducts(functions=["離床偵測"], other=["夜間靜音"])
    found = find(CATALOG, args.query, args.count, {})

    content = found_content(found, args)

    assert len(content["products"]) == TEXTS_READ
    assert content["products"][0]["text"] == "第 0 項"
    assert len(found_evidence(found, args)["products"]) == TEXTS_READ


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
