"""The chat models the graph's nodes call, each with its own model and effort (DV11)."""

from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from wobot.config import Settings


def openai_model(
    settings: Settings,
    model: str,
    effort: str,
    timeout: float | None = None,
    max_retries: int = 3,
) -> ChatOpenAI:
    """An OpenAI model at a reasoning effort; "default" sends none. The timeout is the
    settings' unless a node needs a shorter one."""
    return ChatOpenAI(
        model=model,
        reasoning=None if effort == "default" else {"effort": effort},
        # Explicit: without a reasoning setting ChatOpenAI would fall back to Chat
        # Completions, and a comparison of efforts would also compare two APIs.
        use_responses_api=True,
        api_key=settings.openai_api_key,
        timeout=timeout or settings.openai_timeout_seconds,
        max_retries=max_retries,
    )


class UnreadableAnswer(Exception):
    """A structured answer its schema cannot read: the model's doing, worth trying again,
    so the graph's nodes take it as they take an outage."""


def structured_reader(model: BaseChatModel, schema: type[BaseModel]) -> Runnable:
    """The model answering in the schema, as `{"raw", "parsed", "parsing_error"}`: the
    reply is kept, for `read_structured` to fall back on."""
    return model.with_structured_output(schema, method="json_schema", include_raw=True)


def read_structured[T: BaseModel](result: Mapping[str, Any], schema: type[T]) -> T:
    """The answer a `structured_reader` gave. gpt-6-luna may write a commentary before
    its JSON, each a text part of the reply (`phase` commentary, then final_answer), and
    langchain_openai then reads no answer at all: 1 turn in 75 on the deployed API (C8).
    The answer is then the reply's last text part."""
    if result["parsed"] is not None:
        return result["parsed"]
    try:
        return schema.model_validate_json(last_text(result["raw"]))
    except ValidationError as exc:
        raise UnreadableAnswer(f"no {schema.__name__}: {result['parsing_error']}") from exc


def last_text(message: BaseMessage) -> str:
    """A reply's last text part, or its text when it is a single string."""
    if isinstance(message.content, str):
        return message.content
    texts = [
        part.get("text", "")
        for part in message.content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return texts[-1] if texts else ""
