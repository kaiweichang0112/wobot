"""The chat models the graph's nodes call, each with its own model and effort (DV11)."""

from langchain_openai import ChatOpenAI

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
