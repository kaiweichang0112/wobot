import json
from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from tests.agent.fakes import ScriptedChatModel, answers, calls
from wobot.agent.build import build_agent
from wobot.agent.checkpoints import checkpoint_serde
from wobot.agent.requirements import merge
from wobot.agent.tools import TurnContext, build_tools

BED_EXIT = "離床預警"
NOT_WORN = "不需配戴"
AT_HOME = "居家使用"


def needs(*must_have, goal="照顧臥床長者", count=1):
    return {"goal": goal, "must_have": list(must_have), "count": count}


def test_conditions_get_ids_and_the_first_record_is_version_one():
    first = merge(None, "照顧臥床長者", [BED_EXIT, NOT_WORN, BED_EXIT], [], 1, "m-1")

    assert first["must_have"] == [{"id": "n1", "text": BED_EXIT}, {"id": "n2", "text": NOT_WORN}]
    assert (first["version"], first["source"]) == (1, "m-1")


def test_the_same_needs_again_change_nothing():
    first = merge(None, "照顧臥床長者", [BED_EXIT], [], 1, "m-1")

    assert merge(first, "照顧臥床長者", [f" {BED_EXIT} "], [], 1, "m-2") is first


def test_a_changed_need_replaces_only_what_is_no_longer_listed():
    first = merge(None, "照顧臥床長者", [BED_EXIT, NOT_WORN], [], 1, "m-1")

    second = merge(first, "照顧臥床長者", [BED_EXIT, AT_HOME], ["價格實惠"], 1, "m-2")

    # The kept condition keeps its ID; a new one never takes a dropped one's.
    assert second["must_have"] == [{"id": "n1", "text": BED_EXIT}, {"id": "n3", "text": AT_HOME}]
    assert (second["version"], second["preferences"]) == (2, ["價格實惠"])


def asking(version):
    """A decision to ask the user something, on the needs of that version."""
    return {"action": "clarify", "requirements_version": version, "products": []}


async def converse(knowledge, script, questions):
    model = ScriptedChatModel(script=script)
    agent = build_agent(
        model,
        build_tools(knowledge.db, knowledge.embedder),
        InMemorySaver(serde=checkpoint_serde()),
    )
    config = {"configurable": {"thread_id": "t-1"}}
    states = []
    for question in questions:
        context = TurnContext("account-1", datetime(2026, 10, 3, tzinfo=UTC), knowledge.version_id)
        states.append(
            await agent.ainvoke({"messages": [HumanMessage(question)]}, config, context=context)
        )
    return model, states


async def test_needs_last_through_a_greeting_and_change_when_restated(knowledge, caplog):
    script = [
        calls("update_requirements", needs(BED_EXIT, NOT_WORN), "call-1"),
        answers("想在哪裡使用？", recommendation=asking(1)),
        answers("你好！"),
        calls("update_requirements", needs(BED_EXIT, AT_HOME), "call-2"),
        answers("想要哪種價位？", recommendation=asking(2)),
    ]

    model, (first, greeted, changed) = await converse(
        knowledge, script, ["想找離床預警、不用配戴的產品", "你好", "改成居家使用，配戴也沒關係"]
    )

    assert greeted["requirements"] == first["requirements"]
    # The greeting's turn was told the needs, as data.
    (prompt,) = [m for m in model.requests[2] if isinstance(m, SystemMessage)]
    details = json.loads(prompt.content[prompt.content.rindex("\n{") :])
    assert details["requirements"]["must_have"][1] == {"id": "n2", "text": NOT_WORN}
    assert changed["requirements"]["version"] == 2
    assert [n["id"] for n in changed["requirements"]["must_have"]] == ["n1", "n3"]
    asked = [m for m in changed["messages"] if isinstance(m, HumanMessage)][-1]
    assert changed["requirements"]["source"] == asked.id
    assert "Blocked deserialization" not in caplog.text


@pytest.mark.parametrize(
    "args", [needs(BED_EXIT, count=0), needs(BED_EXIT, goal=" "), needs(*"一二三四五六七八九")]
)
async def test_needs_it_cannot_keep_are_refused_for_the_model_to_fix(knowledge, args):
    script = [calls("update_requirements", args), answers("好。")]

    _, (state,) = await converse(knowledge, script, ["推薦產品"])

    (refused,) = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert refused.status == "error" and "requirements" not in state
