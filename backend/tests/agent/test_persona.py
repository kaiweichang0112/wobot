import pytest

from wobot.agent import answer, chat, rec_agent, write_list
from wobot.agent.persona import PERSONA


def test_chat_has_the_persona_after_its_rules():
    assert PERSONA in chat.INSTRUCTIONS
    rules, _, after = chat.INSTRUCTIONS.partition(PERSONA)

    assert "Markdown" in rules  # after the rules its last sentence names
    assert "Markdown" not in after


@pytest.mark.parametrize("node", [answer, rec_agent, write_list])
def test_the_paths_that_say_what_sources_say_are_plain(node):
    # DEC-097: only small talk is emotional.
    assert PERSONA not in node.INSTRUCTIONS
