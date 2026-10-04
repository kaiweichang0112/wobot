"""Plays evaluation conversations through the chat agent and keeps what each turn did.

Recording is apart from scoring: a record holds the tools called, how each answered, the
answer and the cost, so one run can be scored by several checks and compared across
models. The tools check is the first: the tools the model chose against a person's sets.
"""

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from wobot.agent.messages import commentary, final_text
from wobot.agent.tools import TurnContext
from wobot.eval.dataset import Case, Dataset

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolUse:
    name: str
    args: dict[str, Any]
    # found, no_match or failed as the tool reported it; error when it refused the
    # arguments; missing when the call never ran.
    outcome: str


@dataclass(frozen=True)
class TurnRecord:
    answer: str
    tools: list[ToolUse]  # in the order the model called them
    # Commentary in a reply that called no tool: the model wrote a call as text instead of
    # making it, so nothing was looked up.
    unsent_calls: list[str]
    model_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float


@dataclass
class AgentCaseResult:
    dataset: str
    case_id: str
    split: str
    scenario: str
    user_input: str
    status: str  # scored, pending or error
    detail: str | None = None
    expect: list[list[str]] = field(default_factory=list)
    called: list[str] = field(default_factory=list)  # each tool once, in first-call order
    passed: bool | None = None
    record: TurnRecord | None = None


async def play(
    agent: CompiledStateGraph,
    turns: Sequence[str],
    context: TurnContext,
    history: Sequence[BaseMessage] = (),
) -> list[TurnRecord]:
    """Plays the turns in a new thread, so later turns see the earlier ones.

    The agent needs a checkpointer for that; evaluation gives it an in-memory one. The
    history, if any, enters the thread with the first turn and is not recorded.
    """
    config = {"configurable": {"thread_id": f"eval-{uuid.uuid4()}"}}
    records = []
    for number, turn in enumerate(turns):
        started = time.perf_counter()
        before = list(history) if number == 0 else []
        state = await agent.ainvoke(
            {"messages": [*before, HumanMessage(turn)]}, config=config, context=context
        )
        seconds = time.perf_counter() - started
        records.append(record_turn(_this_turn(state["messages"]), seconds))
    return records


def record_turn(messages: Sequence[BaseMessage], seconds: float) -> TurnRecord:
    replies = [m for m in messages if isinstance(m, AIMessage)]
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    tools = [
        ToolUse(call["name"], call["args"], _outcome(results.get(call["id"])))
        for reply in replies
        for call in reply.tool_calls
    ]
    usage = [reply.usage_metadata or {} for reply in replies]
    return TurnRecord(
        answer=final_text(replies[-1]),
        tools=tools,
        unsent_calls=[text for r in replies if not r.tool_calls for text in commentary(r)],
        model_calls=len(replies),
        input_tokens=sum(u.get("input_tokens", 0) for u in usage),
        output_tokens=sum(u.get("output_tokens", 0) for u in usage),
        seconds=seconds,
    )


def _this_turn(messages: Sequence[BaseMessage]) -> Sequence[BaseMessage]:
    """The messages after the turn's own question."""
    last = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    return messages[last + 1 :]


def _outcome(result: ToolMessage | None) -> str:
    if result is None:
        return "missing"
    if result.status == "error":
        return "error"
    return str(result.artifact.status)


async def scripted_history(
    tools: Sequence[BaseTool], history: Sequence[dict[str, Any]], context: TurnContext
) -> list[BaseMessage]:
    """Earlier turns as messages: the scripted calls are run, so the results are real."""
    by_name = {tool.name: tool for tool in tools}
    messages: list[BaseMessage] = []
    for number, turn in enumerate(history):
        calls = [
            {"name": call["name"], "args": call["args"], "id": f"history-{number}-{index}"}
            for index, call in enumerate(turn.get("tools", []))
        ]
        messages += [HumanMessage(turn["user"]), AIMessage("", tool_calls=calls)]
        for call in calls:
            runtime = ToolRuntime(
                state={},
                context=context,
                config={},
                stream_writer=lambda _: None,
                tool_call_id=call["id"],
                store=None,
            )
            args = {**call["args"], "runtime": runtime}
            tool_call = {**call, "args": args, "type": "tool_call"}
            messages.append(await by_name[call["name"]].ainvoke(tool_call))
        messages.append(AIMessage(turn["answer"]))
    return messages


def called_tools(record: TurnRecord) -> list[str]:
    """Each tool the turn called, once: a refused call and its correction are one choice."""
    return list(dict.fromkeys(tool.name for tool in record.tools))


def tools_passed(called: Sequence[str], expect: Sequence[Sequence[str]]) -> bool:
    return frozenset(called) in {frozenset(acceptable) for acceptable in expect}


async def run_tool_selection(
    agent: CompiledStateGraph,
    tools: Sequence[BaseTool],
    datasets: Sequence[Dataset],
    version_id: int,
    *,
    splits: Sequence[str] = ("dev", "heldout"),
) -> list[AgentCaseResult]:
    """Plays every tools case of the splits once, and scores the tools of its last turn."""
    results = []
    for dataset in datasets:
        query_time = datetime.fromisoformat(dataset.query_time)
        for case in dataset.cases:
            if case.split not in splits or not case.check or case.check["kind"] != "tools":
                continue
            result = AgentCaseResult(
                dataset.name, case.case_id, case.split, case.scenario, case.user_input, "pending"
            )
            results.append(result)
            if not case.check.get("expect"):
                result.detail = case.pending or "not labelled yet"
                continue
            result.expect = case.check["expect"]
            try:
                result.record = await _play_case(agent, tools, case, query_time, version_id)
            except Exception as error:  # one case's failure must not end the run
                logger.exception("case %s failed", case.case_id)
                result.status, result.detail = "error", f"{type(error).__name__}: {error}"
                continue
            result.called = called_tools(result.record)
            result.passed = tools_passed(result.called, result.expect)
            result.status = "scored"
    return results


async def _play_case(
    agent: CompiledStateGraph,
    tools: Sequence[BaseTool],
    case: Case,
    query_time: datetime,
    version_id: int,
) -> TurnRecord:
    context = TurnContext("eval", query_time, version_id, case.chatbot_name or "Wobot")
    history = await scripted_history(tools, case.history, context)
    (record,) = await play(agent, [case.user_input], context, history)
    return record
