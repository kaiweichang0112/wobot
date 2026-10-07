"""Stand-ins for the models, so tests run without a network or a key."""

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda

from wobot.agent.graph import Models
from wobot.agent.rewrite import SearchPlan
from wobot.agent.route import Route, Routed


class FakeRouter:
    """Routes every message the same way, and remembers what it was asked."""

    def __init__(self, route: Route, confidence: float | None = None):
        self.route = route
        self.confidence = confidence
        self.calls: list[tuple[list[BaseMessage], str | None]] = []

    async def __call__(
        self, messages: Sequence[BaseMessage], pending_question: str | None
    ) -> Routed:
        self.calls.append((list(messages), pending_question))
        return Routed(self.route, self.confidence)


def fake_chat(*replies: str) -> GenericFakeChatModel:
    """A chat model that answers with the replies given, in order."""
    return GenericFakeChatModel(messages=iter(AIMessage(reply) for reply in replies))


def fake_models(
    route: Route = "chat",
    chat: Sequence[str] = ("你好！",),
    plan: SearchPlan | None = None,
    answers: Sequence[str] = ("答案。",),
) -> Models:
    """Models that route, plan and answer as given."""
    plan = plan or SearchPlan(question_zh="問題", question_en="question", name=None)
    return Models(
        router=FakeRouter(route),
        chat=fake_chat(*chat),
        rewrite=FakeStructuredModel(plan),
        answer=fake_chat(*answers),
    )


class FakeStructuredModel:
    """A chat model whose structured output is given: records each prompt it is sent."""

    def __init__(self, output: Any):
        self.output = output
        self.prompts: list[list[BaseMessage]] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> RunnableLambda:
        def answer(messages: list[BaseMessage]) -> Any:
            self.prompts.append(messages)
            return self.output

        return RunnableLambda(answer)
