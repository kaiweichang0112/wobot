import pytest
from langchain_core.messages import AIMessage

from wobot.agent.models import UnreadableAnswer, last_text, read_structured
from wobot.agent.rewrite import SearchPlan

PLAN = '{"question_zh": "問題", "question_en": "question", "name": null}'


def test_a_parsed_answer_is_taken_as_it_is():
    plan = SearchPlan(question_zh="問", question_en="q", name=None)

    assert read_structured({"raw": AIMessage(""), "parsed": plan}, SearchPlan) is plan


def test_an_answer_written_after_a_commentary_is_read_from_the_last_part():
    raw = AIMessage(
        [
            {"type": "text", "text": "Let me rewrite it.", "phase": "commentary"},
            {"type": "text", "text": PLAN, "phase": "final_answer"},
        ]
    )

    plan = read_structured({"raw": raw, "parsed": None, "parsing_error": "none"}, SearchPlan)

    assert plan == SearchPlan(question_zh="問題", question_en="question", name=None)


def test_an_answer_no_part_of_which_reads_is_unreadable():
    raw = AIMessage([{"type": "text", "text": "I cannot help with that."}])

    with pytest.raises(UnreadableAnswer):
        read_structured({"raw": raw, "parsed": None, "parsing_error": "none"}, SearchPlan)


def test_the_last_text_of_a_reply():
    assert last_text(AIMessage("whole")) == "whole"
    assert last_text(AIMessage([{"type": "reasoning"}, {"type": "text", "text": "b"}])) == "b"
    assert last_text(AIMessage([])) == ""
