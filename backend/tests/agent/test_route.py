import hashlib
import json
from typing import get_args

import httpx2
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_typesafe import ChoiceAnswer, ClassifierResponse, Usage
from langchain_typesafe.client import TypeSafeAPIError, TypeSafeInternalServerError

from tests.agent.fakes import FakeStructuredModel
from wobot.agent import route
from wobot.agent.route import (
    CRITERIA,
    RECENT_MESSAGES,
    FallbackRouter,
    JevRouter,
    OpenAIRouter,
    Route,
    RouteChoice,
    Routed,
    chat_router,
    router_state,
)
from wobot.config import Settings


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


def unavailable() -> TypeSafeInternalServerError:
    return TypeSafeInternalServerError(503, None, httpx2.Headers())


class FakeClassifier:
    """Jev, failing with the errors given before it answers."""

    def __init__(self, choice: str, *failures: Exception):
        self.choice = choice
        self.failures = list(failures)
        self.requests: list[dict] = []

    async def ainvoke(self, request: dict) -> ClassifierResponse:
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
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


async def test_jev_is_tried_once_more_when_it_is_down_a_moment():
    classifier = FakeClassifier("recommend", unavailable())

    routed = await JevRouter(classifier)([HumanMessage("下肢")], "哪一類復健？")

    assert routed.route == "recommend" and len(classifier.requests) == 2


async def test_jev_down_twice_fails():
    classifier = FakeClassifier("recommend", unavailable(), unavailable())

    with pytest.raises(TypeSafeInternalServerError):
        await JevRouter(classifier)([HumanMessage("下肢")], None)


async def test_a_refused_request_is_not_tried_again():
    classifier = FakeClassifier("chat", TypeSafeAPIError(400, None, httpx2.Headers()))

    with pytest.raises(TypeSafeAPIError):
        await JevRouter(classifier)([HumanMessage("你好")], None)
    assert len(classifier.requests) == 1


class Fixed:
    def __init__(self, routed: Routed | Exception):
        self.routed = routed
        self.calls = 0

    async def __call__(self, messages, pending_question) -> Routed:
        self.calls += 1
        if isinstance(self.routed, Exception):
            raise self.routed
        return self.routed


@pytest.mark.parametrize("failure", [unavailable(), ValueError("Jev chose no route: 'x'")])
async def test_the_fallback_routes_when_the_first_router_fails(failure):
    second = Fixed(Routed("recommend", None, 400, 5))

    routed = await FallbackRouter(Fixed(failure), second)([HumanMessage("下肢")], None)

    assert routed == Routed("recommend", None, 400, 5, fallback=True) and second.calls == 1


async def test_the_fallback_waits_while_the_first_router_answers():
    second = Fixed(Routed("chat", None))

    routed = await FallbackRouter(Fixed(Routed("list", 0.9)), second)([HumanMessage("x")], None)

    assert routed == Routed("list", 0.9) and second.calls == 0


def test_the_chat_graph_falls_back_from_jev_only():
    jev = Settings(classify_model="jev-latest", typesafe_api_key="k", openai_api_key="k")
    luna = Settings(classify_model="gpt-6-luna", openai_api_key="k")

    assert isinstance(chat_router(jev), FallbackRouter)
    assert isinstance(chat_router(jev).second, OpenAIRouter)
    assert isinstance(chat_router(luna), OpenAIRouter)
