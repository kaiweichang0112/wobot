import uuid
from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage

from tests.agent.fakes import ScriptedChatModel, answers, calls
from tests.agent.test_tools import call
from wobot.agent.answers import Answer, Check, ListRef, Pick, Recommendation
from wobot.agent.build import build_agent
from wobot.agent.guard import TurnEvidence, problems, this_turn
from wobot.agent.render import UNVERIFIED_TEXT, ReplyStatus, render, turn_reply
from wobot.agent.requirements import merge
from wobot.agent.tools import TurnContext, build_tools, record_handle
from wobot.knowledge.lists import Listed

CATALOG = "smart-care-products.xlsx"
NEEDS = merge(None, "照顧臥床長者", ["離床預警", "不需配戴"], [], 1, None)


def product(name="測試床墊", url="https://example.com/p", phone="03-1234567"):
    fields = {
        "product_name": name,
        "company_name": "測試公司",
        "product_url": url,
        "contact_phone": phone,
    }
    return Listed(uuid.uuid4(), f"product:{name}", fields, CATALOG, {})


def held(*products):
    evidence = TurnEvidence(looked_up=True, sources={"k-1": ["https://example.com/page"]})
    for item in products:
        handle = record_handle(item.record_id)
        evidence.products[handle] = item
        evidence.sources[handle] = [CATALOG]
    return evidence


def pick(handle, n1="supported", n2="supported", evidence=("k-1",)):
    return Pick(
        product_id=handle,
        checks=[
            Check(requirement_id="n1", status=n1, evidence_ids=list(evidence)),
            Check(requirement_id="n2", status=n2, evidence_ids=[handle]),
        ],
    )


def decided(*picks, action="recommend", version=1):
    decision = Recommendation(action=action, requirements_version=version, products=list(picks))
    return Answer(
        answer="這項最符合。",
        grounding="grounded",
        citations=[],
        lists=[],
        recommendation=decision,
    )


def test_a_product_supported_on_every_condition_passes():
    bed = product()
    handle = record_handle(bed.record_id)

    assert problems(decided(pick(handle)), held(bed), NEEDS) == []


@pytest.mark.parametrize(
    ("change", "found"),
    [
        (lambda h: decided(pick(h), version=2), "requirements_version 2 is not the current 1"),
        (lambda h: decided(pick(h, n1="unknown")), "is unknown on n1"),
        (lambda h: decided(pick(h, n2="contradicted")), "is contradicted on n2"),
        (lambda h: decided(pick(h, evidence=())), "cites no evidence for n1"),
        (lambda h: decided(pick(h, evidence=("k-9",))), "['k-9'] for n1 is not from this turn"),
        (lambda h: decided(pick("r-00000000")), "r-00000000 is not a product whose details"),
        (lambda h: decided(pick(h), pick(h)), "asked for 1 product(s)"),
        (lambda h: decided(pick(h), action="clarify"), "clarify recommends no products"),
        (lambda h: decided(action="recommend"), "recommend names no product"),
    ],
)
def test_a_recommendation_code_cannot_stand_behind_is_held_back(change, found):
    bed = product()

    reasons = problems(change(record_handle(bed.record_id)), held(bed), NEEDS)

    assert any(found in reason for reason in reasons), reasons


def test_a_recommendation_attaches_no_list():
    bed = product()
    listing = ListRef(result_id="q-1", item_ids=None)
    answer = decided(pick(record_handle(bed.record_id))).model_copy(update={"lists": [listing]})

    reasons = problems(answer, held(bed), NEEDS)

    assert any("attaches no lists" in reason for reason in reasons), reasons


def test_a_missing_check_counts_as_unchecked():
    bed = product()
    handle = record_handle(bed.record_id)
    one = Pick(
        product_id=handle,
        checks=[Check(requirement_id="n1", status="supported", evidence_ids=["k-1"])],
    )

    (found,) = problems(decided(one), held(bed), NEEDS)

    assert "unchecked on n2" in found


def test_no_decision_stands_before_the_needs_are_recorded():
    reasons = problems(decided(action="clarify"), held(), None)

    assert any("call update_requirements" in reason for reason in reasons), reasons


def test_a_recommended_product_is_shown_by_code_with_where_each_condition_is_stated():
    bed, plain = product(), product("無網址床墊", url=None, phone=None)
    evidence = held(bed, plain)

    reply = render(decided(pick(record_handle(bed.record_id))), evidence, NEEDS)
    bare = render(decided(pick(record_handle(plain.record_id))), evidence, NEEDS)

    assert reply.text.split("\n\n")[1].splitlines() == [
        "推薦：測試床墊（測試公司）",
        "- 離床預警：依據 https://example.com/page",
        f"- 不需配戴：依據 {CATALOG}",
        "產品網頁：https://example.com/p",
        "廠商電話：03-1234567",
    ]
    assert (reply.action, reply.products) == ("recommend", [bed])
    # The card says where each condition is stated: the answer does not list it again.
    cited = render(
        decided(pick(record_handle(bed.record_id))).model_copy(update={"citations": ["k-1"]}),
        evidence,
        NEEDS,
    )
    assert "來源：" not in cited.text
    assert "產品網頁" not in bare.text and "廠商電話" not in bare.text


async def play(knowledge, script):
    agent = build_agent(
        ScriptedChatModel(script=script), build_tools(knowledge.db, knowledge.embedder)
    )
    context = TurnContext("account-1", datetime(2026, 10, 3, tzinfo=UTC), knowledge.version_id)
    state = await agent.ainvoke({"messages": [HumanMessage("推薦離床預警的床墊")]}, context=context)
    return turn_reply(
        this_turn(state["messages"]),
        state["structured_response"],
        context.artifacts,
        state.get("requirements"),
    )


async def a_product(knowledge):
    tools = build_tools(knowledge.db, knowledge.embedder)
    found = await call(tools, "query_records", {"record_type": "product"}, knowledge.version_id)
    return record_handle(found.artifact.items[0].record_id)


def recommends(handle, n1="supported"):
    checks = [
        {"requirement_id": "n1", "status": n1, "evidence_ids": [handle]},
        {"requirement_id": "n2", "status": "supported", "evidence_ids": [handle]},
    ]
    decision = {
        "action": "recommend",
        "requirements_version": 1,
        "products": [{"product_id": handle, "checks": checks}],
    }
    return answers("這項最符合。", grounding="grounded", recommendation=decision)


def turn(handle, *replies):
    needs = {"goal": "照顧臥床長者", "must_have": ["離床預警", "不需配戴"]}
    return [
        calls("update_requirements", needs, "call-1"),
        calls("get_product_details", {"product_ids": [handle]}, "call-2"),
        *replies,
    ]


async def test_a_turn_records_needs_reads_details_and_shows_the_product(knowledge):
    handle = await a_product(knowledge)

    reply = await play(knowledge, turn(handle, recommends(handle)))

    assert (reply.status, reply.action) == (ReplyStatus.ANSWERED, "recommend")
    assert "推薦：" in reply.text and [record_handle(p.record_id) for p in reply.products] == [
        handle
    ]


async def test_a_product_unknown_on_a_condition_is_never_recommended(knowledge):
    handle = await a_product(knowledge)
    unknown = recommends(handle, n1="unknown")

    reply = await play(knowledge, turn(handle, unknown, unknown))

    assert (reply.text, reply.status) == (UNVERIFIED_TEXT, ReplyStatus.UNVERIFIED)


async def test_a_turn_that_records_needs_must_give_its_decision(knowledge):
    needs = {"goal": "照顧容易跌倒的父親", "must_have": ["偵測跌倒"]}
    question = answers("想用在哪種情境？", grounding="general")
    clarify = {"action": "clarify", "requirements_version": 1, "products": []}
    script = [
        calls("update_requirements", needs),
        question,
        answers("想用在哪種情境？", grounding="general", recommendation=clarify),
    ]

    reply = await play(knowledge, script)

    # Asked once more, the second try says it is asking.
    assert (reply.status, reply.action) == (ReplyStatus.ANSWERED, "clarify")


async def test_a_bare_wish_for_a_product_needs_no_decision(knowledge):
    # Recorded without conditions, as a list question can be by mistake: answered as it is.
    script = [
        calls("update_requirements", {"goal": "看看有哪些產品", "must_have": []}),
        answers("想找哪一類的產品？", grounding="general"),
    ]

    reply = await play(knowledge, script)

    assert (reply.status, reply.action) == (ReplyStatus.ANSWERED, None)
