"""A chat model that plays a script, so agent tests never call a provider."""

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedChatModel(BaseChatModel):
    """Answers each request with the next scripted message, and keeps what it was sent."""

    script: list[AIMessage]
    requests: list[list[BaseMessage]] = []
    tool_names: list[str] = []  # the tools the agent offered

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        self.tool_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.requests.append(list(messages))
        if not self.script:
            raise AssertionError("the model was called more often than scripted")
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def answers(
    text: str,
    lists: list[dict[str, Any]] = (),
    *,
    grounding: str = "general",
    citations: list[str] = (),
    **kwargs: Any,
) -> AIMessage:
    """A final reply in the agent's answer schema, as the provider returns it: JSON text."""
    answer = {
        "answer": text,
        "grounding": grounding,
        "citations": list(citations),
        "lists": list(lists),
    }
    return AIMessage(json.dumps(answer, ensure_ascii=False), **kwargs)


def calls(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    """A model reply that calls one tool."""
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])
