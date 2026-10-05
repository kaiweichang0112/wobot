"""Checks a final answer against what this turn's tools returned (DB5).

Only what code can decide is checked here: that every ID the answer gives came from this
turn's tools, and that an answer claiming to rest on sources, or claiming they hold
nothing, looked something up. Whether the words follow from the sources is measured in
evaluation instead.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain.agents.middleware.types import OmitFromInput
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.runtime import Runtime
from langgraph.types import Command

from wobot.agent.answers import Answer
from wobot.agent.messages import commentary
from wobot.agent.tools import (
    DetailsResult,
    RecordsResult,
    SearchResult,
    ToolStatus,
    chunk_handle,
    record_handle,
)


@dataclass
class TurnEvidence:
    """What this turn's tools returned, by the IDs the model was given."""

    # Each k-, r- and q- ID, with the pages or files it was read from. A result's ID may
    # be cited too, for what rests on the whole result, such as a count.
    sources: dict[str, list[str]] = field(default_factory=dict)
    # A cited lecture's PDF, by its r- ID: code links it, as a listed lecture does.
    pdfs: dict[str, str] = field(default_factory=dict)
    results: dict[str, RecordsResult] = field(default_factory=dict)  # by result_id
    looked_up: bool = False  # a tool answered, with or without a match
    failed: bool = False  # a tool could not reach the database or the provider


def this_turn(messages: Sequence[BaseMessage]) -> Sequence[BaseMessage]:
    """The messages after the turn's own question."""
    last = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    return messages[last + 1 :]


def turn_evidence(
    messages: Sequence[BaseMessage], artifacts: Mapping[str, Any] | None = None
) -> TurnEvidence:
    """The evidence among a turn's messages after the question, with the artifacts the
    turn's context keeps for them. A call whose arguments were refused returned nothing
    and counts for nothing."""
    evidence = TurnEvidence()
    for message in messages:
        if not isinstance(message, ToolMessage) or message.status == "error":
            continue
        artifact = tool_artifact(message, artifacts)
        if artifact.status is ToolStatus.FAILED:
            evidence.failed = True
            continue
        evidence.looked_up = True
        if isinstance(artifact, RecordsResult):
            evidence.results[artifact.result_id] = artifact
            evidence.sources.setdefault(artifact.result_id, [])
            for item in [*artifact.items, *artifact.uncertain]:
                _add(evidence, record_handle(item.record_id), item.source_url)
                _add(evidence, artifact.result_id, item.source_url)
                if pdf := item.fields.get("pdf_url"):
                    evidence.pdfs[record_handle(item.record_id)] = pdf
        elif isinstance(artifact, DetailsResult):
            for product in artifact.products:
                _add(evidence, record_handle(product.record_id), product.source_url)
        elif isinstance(artifact, SearchResult):
            for found in artifact.evidence:
                handle = chunk_handle(found.hit.chunk_id)
                evidence.sources.setdefault(handle, [])
                for member in found.members:
                    _add(evidence, handle, member.source_url)
                    _add(evidence, record_handle(member.record_id), member.source_url)
    return evidence


def tool_artifact(message: ToolMessage, artifacts: Mapping[str, Any] | None) -> Any:
    """A tool message's artifact: kept by the turn's context, or still on the message."""
    if message.artifact is not None:
        return message.artifact
    return (artifacts or {}).get(message.tool_call_id)


def _add(evidence: TurnEvidence, handle: str, source_url: str) -> None:
    sources = evidence.sources.setdefault(handle, [])
    if source_url not in sources:
        sources.append(source_url)


def problems(answer: Answer, evidence: TurnEvidence) -> list[str]:
    """What is wrong with the answer, in words the model can act on; empty when nothing."""
    found: list[str] = []
    if answer.grounding != "general" and not evidence.looked_up:
        found.append(
            f"grounding is {answer.grounding}, but no tool returned anything this turn: "
            "look it up first, or answer as general if it needs no sources"
        )
    if unknown := [c for c in answer.citations if c not in evidence.sources]:
        found.append(f"citations {unknown} are not IDs this turn's tools returned")
    for ref in answer.lists:
        result = evidence.results.get(ref.result_id)
        if result is None:
            found.append(f"result_id {ref.result_id} is not one query_records returned this turn")
            continue
        held = {record_handle(i.record_id) for i in [*result.items, *result.uncertain]}
        if unknown := [i for i in ref.item_ids or [] if i not in held]:
            found.append(f"item_ids {unknown} are not items of {ref.result_id}")
    # Once something was looked up, a grounded answer says what it rests on.
    if (
        answer.grounding == "grounded"
        and evidence.looked_up
        and not answer.citations
        and not answer.lists
    ):
        found.append(
            "grounding is grounded, but nothing is cited: cite the IDs the answer rests on"
        )
    return found


# Marks the empty message that ends a turn whose replies never parsed: no model wrote it.
NO_ANSWER = {"wobot_no_answer": True}

# What the model is told when its reply is held back; the user never reads it.
UNPARSED_NOTE = (
    "Your reply was neither a tool call nor an answer in the schema. Text that looks like "
    "a tool call runs nothing: call the tool, or answer in the schema."
)


def problems_note(found: Sequence[str]) -> str:
    return (
        "Code checked your answer and cannot show it: "
        + "; ".join(found)
        + ". Fix this and reply again: call a tool, or answer in the schema."
    )


class GuardState(AgentState):
    # This turn's held-back tries, each as {reason, detail, input_tokens, output_tokens}:
    # the state keeps only what was shown, so evaluation counts them and their cost here.
    # Plain values, so checkpoints store them as they are.
    retries: NotRequired[Annotated[list[dict[str, Any]], OmitFromInput]]


def _held(reason: str, detail: list[str], message: AIMessage) -> dict[str, Any]:
    usage = message.usage_metadata or {}
    return {
        "reason": reason,  # unparsed, or problems with the answer
        "detail": detail,  # the problems, or the text written instead of a call
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }


class AnswerGuard(AgentMiddleware[GuardState]):
    """Gives the model one more try when its reply does not parse or its answer fails the
    checks above.

    It wraps each model call, so the failed try and the note never enter the messages: the
    conversation keeps only what was shown. Whatever the second try answers is kept, and
    the reply checks it again. If it does not parse either, the turn ends with no answer,
    which the reply shows as a failure to try again, never as the model's words.
    """

    state_schema = GuardState

    async def abefore_agent(self, state: GuardState, runtime: Runtime) -> dict[str, Any]:
        return {"retries": []}  # each turn counts its own

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        try:
            response = await handler(request)
        except StructuredOutputValidationError as error:
            failed, note = error.ai_message, UNPARSED_NOTE
            held = [_held("unparsed", commentary(failed), failed)]
        else:
            answer = response.structured_response
            if answer is None:  # tool calls: checked once the answer comes
                return response
            artifacts = request.runtime.context.artifacts
            evidence = turn_evidence(this_turn(request.messages), artifacts)
            # After a failed tool the reply is retryable whatever the answer: no second try.
            if evidence.failed or not (found := problems(answer, evidence)):
                return response
            failed, note = response.result[-1], problems_note(found)
            held = [_held("problems", found, failed)]
        retry = request.override(messages=[*request.messages, failed, SystemMessage(note)])
        try:
            response = await handler(retry)
        except StructuredOutputValidationError as error:
            held.append(_held("unparsed", commentary(error.ai_message), error.ai_message))
            response = ModelResponse(result=[AIMessage("", response_metadata=NO_ANSWER)])
        retries = [*request.state.get("retries", []), *held]
        return ExtendedModelResponse(response, Command(update={"retries": retries}))


class TurnArtifacts(AgentMiddleware):
    """Keeps each tool's artifact in the turn's context instead of its message (D1, B7).

    The model never reads artifacts, and answers rest only on the turn's own results, so
    no later turn needs them. Kept in the messages, a complete list would be stored again
    at every step of the conversation, in types a checkpoint would have to revive.
    """

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        message = await handler(request)
        if isinstance(message, ToolMessage) and message.artifact is not None:
            request.runtime.context.artifacts[message.tool_call_id] = message.artifact
            return message.model_copy(update={"artifact": None})
        return message
