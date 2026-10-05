"""What the model reads of earlier turns: their words and calls, not their old results.

Answers rest only on what the turn's own tools returned (policy A), so an earlier turn's
results are never evidence. The last turn's are kept, since a follow-up such as "the
first one's phone number" needs the IDs it listed; older ones are cleared from what the
model is sent, not from the conversation. The calls stay, so the model knows what was
looked up and can look it up again.

Clearing follows turns rather than a token count: a count would clear a long turn's own
results, and approximate counts are poor for Chinese text.
"""

from dataclasses import dataclass

from langchain.agents.middleware import ContextEditingMiddleware
from langchain.agents.middleware.context_editing import TokenCounter
from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage

CLEARED = "[cleared: a result of an earlier turn; look it up again if it is needed]"


@dataclass(frozen=True)
class ClearEarlierResults:
    """Clears the results of every tool call made before the last `kept_turns` turns."""

    kept_turns: int = 1  # besides the turn being answered

    def apply(self, messages: list[AnyMessage], *, count_tokens: TokenCounter) -> None:
        questions = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
        if len(questions) <= self.kept_turns + 1:
            return
        first_kept = questions[-(self.kept_turns + 1)]
        for index, message in enumerate(messages[:first_kept]):
            if isinstance(message, ToolMessage) and message.content != CLEARED:
                messages[index] = message.model_copy(update={"content": CLEARED})


def earlier_turns() -> ContextEditingMiddleware:
    return ContextEditingMiddleware(edits=[ClearEarlierResults()])
