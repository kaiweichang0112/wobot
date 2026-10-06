import hashlib
import json
from typing import get_args

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_typesafe import ChoiceAnswer, ClassifierResponse, Usage

from tests.agent.fakes import FakeStructuredModel
from wobot.agent import route
from wobot.agent.route import (
    CRITERIA,
    RECENT_MESSAGES,
    JevRouter,
    OpenAIRouter,
    Route,
    RouteChoice,
    Routed,
    router_state,
)


def test_every_route_has_its_criterion():
    assert list(CRITERIA) == list(get_args(Route))


def test_the_prompt_version_moves_with_the_prompt():
    # A changed prompt with the same version would mix two prompts in one comparison.
    prompt = json.dumps([route.INSTRUCTIONS, route.CRITERIA], ensure_ascii=False)
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    assert (route.PROMPT_VERSION, fingerprint) == (1, "937161b238eb")


def test_the_router_reads_the_latest_message_apart_from_a_few_earlier_ones():
    messages = [HumanMessage(f"q{i}") if i % 2 == 0 else AIMessage(f"a{i}") for i in range(12)]
    messages.append(HumanMessage("那 2022 年的呢？"))

    state = router_state(messages, None)

    assert state["latest_message"] == "那 2022 年的呢？"
    assert len(state["earlier_messages"]) == RECENT_MESSAGES
    assert state["earlier_messages"][-1] == {"role": "ai", "content": "a11"}
    assert state["assistant_awaits_an_answer_to"] is None


def test_the_router_knows_the_question_it_answers():
    messages = [
        HumanMessage("我想找可以偵測長輩離床的產品"),
        AIMessage("請問是在家裡用，還是在照護機構用？"),
        HumanMessage("在家裡。"),
    ]

    state = router_state(messages, "請問是在家裡用，還是在照護機構用？")

    assert state["assistant_awaits_an_answer_to"] == "請問是在家裡用，還是在照護機構用？"


async def test_an_openai_router_sends_the_criteria_and_the_state_as_data():
    raw = AIMessage(
        "", usage_metadata={"input_tokens": 420, "output_tokens": 9, "total_tokens": 429}
    )
    model = FakeStructuredModel(
        {"raw": raw, "parsed": RouteChoice(route="list"), "parsing_error": None}
    )
    router = OpenAIRouter(model)

    routed = await router([HumanMessage("列出 2024 年的演講")], None)

    assert routed == Routed("list", None, 420, 9)
    system, human = model.prompts[0]
    assert all(text in system.content for text in CRITERIA.values())
    assert json.loads(human.content)["latest_message"] == "列出 2024 年的演講"


class FakeClassifier:
    def __init__(self, choice: str):
        self.choice = choice
        self.requests: list[dict] = []

    async def ainvoke(self, request: dict) -> ClassifierResponse:
        self.requests.append(request)
        answer = ChoiceAnswer(
            type="choice", choice=self.choice, probabilities={self.choice: 0.9}, confidence=0.8
        )
        return ClassifierResponse(
            model="jev-test", answers={"route": answer}, usage=Usage(input_tokens=310)
        )


async def test_jev_answers_one_choice_with_its_confidence():
    classifier = FakeClassifier("recommend")

    routed = await JevRouter(classifier)([HumanMessage("在家裡。")], "居家還是機構？")

    assert routed == Routed("recommend", 0.8, 310, 0)
    question = classifier.requests[0]["questions"]["route"]
    assert question.criteria == CRITERIA
    assert classifier.requests[0]["state"]["assistant_awaits_an_answer_to"] == "居家還是機構？"


async def test_a_choice_outside_the_routes_is_refused():
    with pytest.raises(ValueError, match="no route"):
        await JevRouter(FakeClassifier("weather"))([HumanMessage("今天天氣？")], None)
