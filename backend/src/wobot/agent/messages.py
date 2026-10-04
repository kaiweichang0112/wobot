"""Reading the agent's replies: what a user is shown, and what only precedes tool calls."""

from langchain_core.messages import AIMessage


def _texts(message: AIMessage, *, commentary: bool) -> list[str]:
    if isinstance(message.content, str):
        return [] if commentary else [message.content]
    return [
        block["text"]
        for block in message.content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and (block.get("phase") == "commentary") == commentary
    ]


def final_text(message: AIMessage) -> str:
    """The reply as a user is shown it. OpenAI's Responses API may add commentary, text a
    reasoning model writes before calling tools; it is not part of the answer."""
    return "".join(_texts(message, commentary=False))


def commentary(message: AIMessage) -> list[str]:
    return _texts(message, commentary=True)
