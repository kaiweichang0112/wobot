import pytest
from langchain_core.messages import AIMessage, HumanMessage

from wobot.agent import answer, chat, rec_agent, write_list
from wobot.agent.persona import PERSONA, last_opening


@pytest.mark.parametrize("node", [chat, answer, rec_agent])
def test_every_node_that_writes_has_the_persona_after_its_rules(node):
    assert PERSONA in node.INSTRUCTIONS
    rules, _, after = node.INSTRUCTIONS.partition(PERSONA)

    assert "Markdown" in rules  # after the rules its last sentence names
    assert "Markdown" not in after


def test_list_intros_are_written_without_the_persona():
    assert PERSONA not in write_list.INSTRUCTIONS


def test_the_last_opening_is_the_reply_before_the_latest_message():
    messages = [
        HumanMessage("GRC 哪年成立？"),
        AIMessage("太好了！GRC 於 2003 年成立。"),
        HumanMessage("地址呢？"),
    ]

    assert last_opening(messages) == "太好了！"


def test_a_reply_with_no_mark_gives_its_first_words():
    messages = [AIMessage("Yay found it and this makes my day"), HumanMessage("And 2022?")]

    assert last_opening(messages) == "Yay found it"


def test_a_first_message_has_no_last_opening():
    assert last_opening([HumanMessage("你好")]) is None
    # A tool call's empty text is not a reply.
    assert last_opening([AIMessage(""), HumanMessage("你好")]) is None
