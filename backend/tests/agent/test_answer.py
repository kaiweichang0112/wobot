import hashlib
import json

from wobot.agent import answer
from wobot.agent.answer import answer_prompt

EVIDENCE = {
    "passages": [
        {
            "id": "k-1a2b3c4d",
            "header": "GRC › About",
            "text": "Since 2003",
            "records": [{"id": "r-9f8e7d6c", "key": "section:about", "type": "section"}],
        }
    ],
    "records": [
        {
            "id": "r-11112222",
            "key": "student:master:王小明",
            "type": "student",
            "source": "https://example.org/students",
            "fields": {"name": "王小明", "graduation_year": 2024},
        }
    ],
}


def test_the_prompt_version_moves_with_the_prompt():
    fingerprint = hashlib.sha256(answer.INSTRUCTIONS.encode()).hexdigest()[:12]

    assert (answer.PROMPT_VERSION, fingerprint) == (1, "b390d4a93f9d")


def test_the_answer_reads_the_message_the_question_and_the_evidence():
    _, human = answer_prompt("嗨！GRC 哪年成立？", "GRC 是哪一年成立的？", EVIDENCE, "Wobot")
    shown = json.loads(human.content)

    assert shown["message"] == "嗨！GRC 哪年成立？"
    assert shown["question"] == "GRC 是哪一年成立的？"
    assert shown["passages"] == [{"header": "GRC › About", "text": "Since 2003"}]
    assert shown["records"] == [{"type": "student", "name": "王小明", "graduation_year": 2024}]


def test_the_answer_sees_no_handles_keys_or_links():
    # It cites nothing (DV4): IDs and URLs would only invite it to.
    _, human = answer_prompt("q", "q", EVIDENCE, "Wobot")

    for hidden in ("k-1a2b3c4d", "r-9f8e7d6c", "r-11112222", "section:about", "https://"):
        assert hidden not in human.content
