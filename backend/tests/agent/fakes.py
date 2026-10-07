"""Stand-ins for the models, so tests run without a network or a key."""

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda

from wobot.agent.graph import Models
from wobot.agent.rewrite import SearchPlan
from wobot.agent.route import Route, Routed
from wobot.agent.write_list import ListIntro


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
    list_calls: Sequence[AIMessage] = (AIMessage("done"),),
    intro: ListIntro | None = None,
) -> Models:
    """Models that route, plan, answer, call list tools and introduce lists as given."""
    plan = plan or SearchPlan(question_zh="問題", question_en="question", name=None)
    return Models(
        router=FakeRouter(route),
        chat=fake_chat(*chat),
        rewrite=FakeStructuredModel(plan),
        answer=fake_chat(*answers),
        list_agent=FakeToolModel(*list_calls),
        write_list=FakeStructuredModel(intro or ListIntro(intro="沒有找到。", lists=[])),
    )


def calls(*tools: tuple[str, dict[str, Any]]) -> AIMessage:
    """A model's message calling each tool with its arguments."""
    return AIMessage(
        "",
        tool_calls=[
            {"name": name, "args": args, "id": f"call-{n}"}
            for n, (name, args) in enumerate(tools, start=1)
        ],
    )


class FakeToolModel:
    """A model bound to tools that replies with the messages given, in order; records
    each prompt and the tool choice it was bound with."""

    def __init__(self, *replies: AIMessage):
        self.replies = iter(replies)
        self.prompts: list[tuple[str | None, list[BaseMessage]]] = []

    def bind_tools(self, tools: Any, tool_choice: str | None = None, **kwargs: Any):
        def reply(messages: list[BaseMessage]) -> AIMessage:
            self.prompts.append((tool_choice, messages))
            return next(self.replies)

        return RunnableLambda(reply)


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
