import hashlib
import json

from langchain_core.messages import AIMessage, HumanMessage

from wobot.agent import rewrite
from wobot.agent.rewrite import RECENT_MESSAGES, SearchPlan, read_plan, rewrite_prompt


def test_the_prompt_version_moves_with_the_prompt():
    prompt = json.dumps([rewrite.INSTRUCTIONS, SearchPlan.model_json_schema()])
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    assert (rewrite.PROMPT_VERSION, fingerprint) == (4, "bc1bef484520")


def test_the_latest_message_is_read_apart_from_the_earlier_ones():
    messages = [HumanMessage(f"q{i}") if i % 2 == 0 else AIMessage(f"a{i}") for i in range(9)]
    messages.append(HumanMessage("那它能偵測離床嗎？"))

    _, human = rewrite_prompt(messages)
    conversation = json.loads(human.content)

    assert conversation["latest_message"] == "那它能偵測離床嗎？"
    assert len(conversation["earlier_messages"]) == RECENT_MESSAGES


def plan(zh: str, en: str, name: str | None = None) -> SearchPlan:
    return SearchPlan(question_zh=zh, question_en=en, name=name)


def test_the_whole_question_is_searched_in_chinese_and_english():
    rewritten = plan("Agicare 智能均壓床的廠商電話是多少？", "What is the phone number of …?")

    assert read_plan(rewritten, "第一個的廠商電話是多少？") == {
        "question": "Agicare 智能均壓床的廠商電話是多少？",
        "queries": ["Agicare 智能均壓床的廠商電話是多少？", "What is the phone number of …?"],
        "name": None,
    }


def test_the_answer_reads_the_question_in_the_users_language():
    rewritten = plan("WhizPad 能偵測離床嗎？", "Can WhizPad detect leaving the bed?")

    assert read_plan(rewritten, "And can it detect leaving the bed?")["question"] == (
        "Can WhizPad detect leaving the bed?"
    )


def test_one_wording_is_searched_once():
    assert read_plan(plan("WhizPad", " WhizPad "), "WhizPad?")["queries"] == ["WhizPad"]


def test_a_blank_question_or_name_counts_as_none_given():
    assert read_plan(plan(" ", "", name="  "), "那它能偵測離床嗎？") == {
        "question": "那它能偵測離床嗎？",
        "queries": ["那它能偵測離床嗎？"],
        "name": None,
    }
    # One language left blank: the other one stands for both.
    assert read_plan(plan(" ", "Is GRC in Taoyuan?"), "GRC 在桃園嗎？")["question"] == (
        "Is GRC in Taoyuan?"
    )
