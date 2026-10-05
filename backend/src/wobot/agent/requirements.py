"""What the user needs from a product, kept across a conversation's turns (B8).

The model records the needs whenever the user states or changes them, always as the whole
current set; code keeps them in the conversation's state, gives each condition an ID that
stays while its wording does, and counts versions, so a recommendation can be checked
against the needs it was made for. Nothing else changes them: a greeting keeps them, and
a new need replaces only what the model no longer lists.
"""

import json
from typing import Annotated, Any, NotRequired

from langchain.agents import AgentState
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, ToolException
from langgraph.types import Command

# Few enough to check each against every candidate.
MUST_HAVE = 8
# A recommendation names one product unless the user asks for more; "all" is a list.
MOST_PRODUCTS = 5


class ChatState(AgentState):
    # The needs as last recorded: goal, must_have [{id, text}], preferences, count,
    # version, last_id and the source message. Plain values, so checkpoints store them.
    requirements: NotRequired[dict[str, Any]]


def merge(
    current: dict[str, Any] | None,
    goal: str,
    must_have: list[str],
    preferences: list[str],
    count: int,
    source: str | None,
) -> dict[str, Any]:
    """The needs after an update: unchanged conditions keep their IDs, and the version
    moves only when something changed."""
    current = current or {}
    held = {need["text"]: need["id"] for need in current.get("must_have", [])}
    last_id = current.get("last_id", 0)
    needs = []
    for text in dict.fromkeys(t.strip() for t in must_have if t.strip()):
        if text not in held:
            last_id += 1
        needs.append({"id": held.get(text, f"n{last_id}"), "text": text})
    content = {
        "goal": goal.strip(),
        "must_have": needs,
        "preferences": list(dict.fromkeys(p.strip() for p in preferences if p.strip())),
        "count": count,
    }
    if current and all(current.get(key) == value for key, value in content.items()):
        return current
    return content | {
        "version": current.get("version", 0) + 1,
        "last_id": last_id,
        "source": source,  # the ID of the message that stated the change
    }


def shown(requirements: dict[str, Any]) -> dict[str, Any]:
    """The needs as the model reads them."""
    keys = ("version", "goal", "must_have", "preferences", "count")
    return {key: requirements[key] for key in keys}


def build_requirement_tools() -> list[BaseTool]:
    @tool
    def update_requirements(
        goal: Annotated[str, "what the product is for, in the user's words"],
        must_have: Annotated[
            list[str],
            "every condition a product must meet, the earlier ones that still hold "
            "included, each one condition as the user stated it",
        ],
        runtime: ToolRuntime[Any],  # the turn's context is not read here
        preferences: Annotated[
            list[str] | None, "what the user would like but does not require"
        ] = None,
        count: Annotated[int, "how many products the user asked for; 1 unless they said"] = 1,
    ) -> Command:
        """Record what the user needs from a product they want recommended, whenever they
        state or change it and before looking for one: always the whole current set, since
        what you leave out is dropped. Not for listing, describing or comparing products,
        and a greeting or thanks changes nothing: do not call it then."""
        if not goal.strip():
            raise ToolException("give the goal")
        if len(must_have) > MUST_HAVE:
            raise ToolException(f"at most {MUST_HAVE} conditions")
        if not 1 <= count <= MOST_PRODUCTS:
            raise ToolException(f"count is 1 to {MOST_PRODUCTS}; for every product, list them")
        messages = runtime.state.get("messages", [])
        question = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        updated = merge(
            runtime.state.get("requirements"),
            goal,
            must_have,
            preferences or [],
            count,
            question.id if question else None,
        )
        content = json.dumps(shown(updated), ensure_ascii=False)
        return Command(
            update={
                "requirements": updated,
                "messages": [ToolMessage(content, tool_call_id=runtime.tool_call_id)],
            }
        )

    update_requirements.handle_tool_error = True
    return [update_requirements]
