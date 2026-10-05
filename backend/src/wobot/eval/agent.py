"""Plays evaluation conversations through the chat agent and keeps what each turn did.

Recording is apart from scoring: a record holds the tools called, how each answered, the
answer and the cost, so one run can be scored by several checks and compared across
models. Four checks read it: tools, the tools the model chose against a person's sets;
retrieval, the records its searches found against the labelled relevant ones; list, the
records the reply showed against the labelled items; and behavior, what the turn did
against what a person expects of it: whether it looked anything up, its grounding and
status, whether it cited sources, and what the reply mentions.
"""

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from wobot.agent.answers import Answer
from wobot.agent.guard import NO_ANSWER, this_turn
from wobot.agent.messages import commentary
from wobot.agent.render import turn_reply
from wobot.agent.tools import SEARCHED_CHUNKS, SearchResult, TurnContext
from wobot.eval import gold
from wobot.eval.corpus import Corpus
from wobot.eval.dataset import Case, Dataset
from wobot.eval.metrics import RetrievalScore, SetScore, retrieval_score, set_score
from wobot.eval.runner import labelled_keys

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Retrieved:
    """One chunk a search returned, in its rank."""

    chunk_id: str
    keys: list[str]  # the logical keys of the records it is built from
    tokens: int


@dataclass(frozen=True)
class ToolUse:
    name: str
    args: dict[str, Any]
    # found, no_match or failed as the tool reported it; error when it refused the
    # arguments; missing when the call never ran.
    outcome: str
    retrieved: list[Retrieved] = field(default_factory=list)  # search_knowledge only


@dataclass(frozen=True)
class ShownItems:
    """One list the reply showed, by the logical keys of its records."""

    result_id: str
    items: list[str]
    uncertain: list[str]


@dataclass(frozen=True)
class TurnRecord:
    answer: str  # as the user reads it, lists included
    tools: list[ToolUse]  # in the order the model called them
    # Commentary in a reply that called no tool: the model wrote a call as text instead of
    # making it, so nothing was looked up.
    unsent_calls: list[str]
    model_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float
    # answered, unverified when the guard held the answer back, or retryable.
    status: str = "answered"
    grounding: str | None = None  # the model's, when its answer was shown
    citations: list[str] = field(default_factory=list)
    # The tries the guard held back and asked again, with why: not in the messages.
    retries: list[dict[str, Any]] = field(default_factory=list)
    lists: list[ShownItems] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # why the guard held it back


@dataclass
class AgentCaseResult:
    dataset: str
    case_id: str
    split: str
    scenario: str
    user_input: str
    kind: str  # the check: tools, retrieval, list or behavior
    status: str  # scored, pending or error
    detail: str | None = None
    expect: list[list[str]] = field(default_factory=list)
    called: list[str] = field(default_factory=list)  # each tool once, in first-call order
    passed: bool | None = None
    # Retrieval twice: the first search alone, comparable with searching the question
    # itself; and every chunk the turn's searches returned, what the model read.
    first_search: RetrievalScore | None = None
    turn_searches: RetrievalScore | None = None
    searches: int = 0
    # The records the reply's lists showed as matches, against the labelled items.
    set: SetScore | None = None
    # What the turn did that a behavior check did not expect; passed when none.
    failures: list[str] = field(default_factory=list)
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
        answer = state.get("structured_response")
        retries = state.get("retries", [])
        records.append(record_turn(this_turn(state["messages"]), seconds, answer, retries))
    return records


def record_turn(
    messages: Sequence[BaseMessage],
    seconds: float,
    answer: Answer | None = None,
    retries: Sequence[dict[str, Any]] = (),
) -> TurnRecord:
    """What the turn did, from its messages after the question and the guard's held-back
    tries, which the messages leave out."""
    replies = [m for m in messages if isinstance(m, AIMessage) and m.response_metadata != NO_ANSWER]
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    tools = [
        ToolUse(
            call["name"],
            call["args"],
            _outcome(results.get(call["id"])),
            _retrieved(results.get(call["id"])),
        )
        for reply in replies
        for call in reply.tool_calls
    ]
    usage = [reply.usage_metadata or {} for reply in replies] + list(retries)
    unsent = [text for r in replies if not r.tool_calls for text in commentary(r)]
    unsent += [text for held in retries if held["reason"] == "unparsed" for text in held["detail"]]
    reply = turn_reply(messages, answer)
    return TurnRecord(
        answer=reply.text,
        tools=tools,
        unsent_calls=unsent,
        model_calls=len(replies) + len(retries),
        input_tokens=sum(u.get("input_tokens", 0) for u in usage),
        output_tokens=sum(u.get("output_tokens", 0) for u in usage),
        seconds=seconds,
        status=str(reply.status),
        grounding=reply.grounding,
        citations=reply.citations,
        retries=list(retries),
        lists=[
            ShownItems(
                shown.result_id,
                [item.logical_key for item in shown.items],
                [item.logical_key for item in shown.uncertain],
            )
            for shown in reply.lists
        ],
        problems=reply.problems,
    )


def _retrieved(result: ToolMessage | None) -> list[Retrieved]:
    if result is None or not isinstance(result.artifact, SearchResult):
        return []
    return [
        Retrieved(str(e.hit.chunk_id), [m.logical_key for m in e.members], e.hit.token_count)
        for e in result.artifact.evidence
    ]


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


# The checks this module scores; a case with another check is not played.
AGENT_KINDS = ("tools", "retrieval", "list", "behavior")
# What a behavior check may expect; any other key is a mistake in the dataset.
BEHAVIORS = ("status", "looks_up", "grounding", "cites", "mentions")


async def run_agent_checks(
    agent: CompiledStateGraph,
    tools: Sequence[BaseTool],
    datasets: Sequence[Dataset],
    corpus: Corpus,
    *,
    splits: Sequence[str] = ("dev", "heldout"),
    kinds: Sequence[str] = AGENT_KINDS,
    gold_dir: Path = gold.GOLD_DIR,
    gold_files: dict[str, str] | None = None,
) -> list[AgentCaseResult]:
    """Plays every case of the splits and kinds once; scores its last turn.

    `gold_files` collects the hash of each gold file the retrieval cases read.
    """
    used = gold_files if gold_files is not None else {}
    results = []
    for dataset in datasets:
        query_time = datetime.fromisoformat(dataset.query_time)
        for case in dataset.cases:
            kind = case.check["kind"] if case.check else None
            if case.split not in splits or kind not in kinds:
                continue
            result = AgentCaseResult(
                dataset.name,
                case.case_id,
                case.split,
                case.scenario,
                case.user_input,
                kind,
                "pending",
            )
            results.append(result)
            try:
                relevant = _prepare(result, case, corpus, gold_dir, used)
            except gold.GoldError as error:
                result.status, result.detail = "error", str(error)
                continue
            if result.detail:
                continue
            try:
                result.record = await _play_case(agent, tools, case, query_time, corpus.version_id)
            except Exception as error:  # one case's failure must not end the run
                logger.exception("case %s failed", case.case_id)
                result.status, result.detail = "error", f"{type(error).__name__}: {error}"
                continue
            result.called = called_tools(result.record)
            if kind == "tools":
                result.passed = tools_passed(result.called, result.expect)
            elif kind == "retrieval":
                await score_searches(result, relevant)
            elif kind == "behavior":
                result.failures = behavior_failures(case.check, result.record)
                result.passed = not result.failures
            else:
                shown = {key for s in result.record.lists for key in s.items}
                result.set = await set_score(relevant, shown)
            result.status = "scored"
    return results


def _prepare(
    result: AgentCaseResult, case: Case, corpus: Corpus, gold_dir: Path, used: dict[str, str]
) -> set[str]:
    """What the case is scored against; sets `detail` when it cannot be scored yet."""
    if result.kind == "tools":
        if not case.check.get("expect"):
            result.detail = case.pending or "not labelled yet"
        result.expect = case.check.get("expect") or []
        return set()
    if result.kind == "behavior":
        expected = {key for key in case.check if key != "kind"}
        if unknown := sorted(expected - set(BEHAVIORS)):
            raise gold.GoldError(f"{case.case_id}: unknown behaviors {unknown}")
        if not expected:
            result.detail = case.pending or "not labelled yet"
        return set()
    relevant, _ = labelled_keys(case, corpus, gold_dir, used)
    if not relevant:
        result.detail = "not labelled yet"
    return relevant


def behavior_failures(check: dict[str, Any], record: TurnRecord) -> list[str]:
    """What the turn did against what the check expects of it; empty when all holds."""
    failures = []
    status = check.get("status", "answered")
    if record.status != status:
        failures.append(f"status {record.status}, expected {status}")
    if "looks_up" in check:
        looked = any(tool.outcome in ("found", "no_match") for tool in record.tools)
        if looked != check["looks_up"]:
            failures.append("looked something up" if looked else "looked nothing up")
    if "grounding" in check and record.grounding not in check["grounding"]:
        failures.append(f"grounding {record.grounding}, expected {' or '.join(check['grounding'])}")
    if "cites" in check:
        cited = bool(record.citations or record.lists)
        if cited != check["cites"]:
            failures.append("cited sources" if cited else "cited nothing")
    failures += [
        f"does not mention {t!r}" for t in check.get("mentions", []) if t not in record.answer
    ]
    return failures


async def score_searches(result: AgentCaseResult, relevant: set[str]) -> None:
    searches = [
        tool.retrieved
        for tool in result.record.tools
        if tool.name == "search_knowledge" and tool.outcome == "found"
    ]
    first = searches[0] if searches else []
    read = list({chunk.chunk_id: chunk for search in searches for chunk in search}.values())
    result.searches = len(searches)
    result.first_search = await retrieval_score(
        relevant, [(c.keys, c.tokens) for c in first], SEARCHED_CHUNKS
    )
    result.turn_searches = await retrieval_score(
        relevant, [(c.keys, c.tokens) for c in read], max(len(read), 1)
    )


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
