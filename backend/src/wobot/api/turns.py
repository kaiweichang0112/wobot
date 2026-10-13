"""A turn: the user's message in, the reply streamed out as server-sent events (DEC-080).

Everything that can refuse the message happens before the stream opens, as an ordinary
HTTP error: a bad message, another account's conversation, a turn already running. Then
the turn is saved as running, `accepted` is sent, and the graph runs inside this
request: Cloud Run gives an instance CPU only while a request is open, so work handed to
a background task after the response could stall. No database connection is held while
the graph runs; the turn is saved again only when it ends.

A conversation's graph memory moves forward only when a turn completes: the run starts
from the checkpoint the conversation points at, and the conversation then points at the
one the run saved (DEC-079). A turn that fails or is stopped leaves it where it was.
"""

import json
import logging
import time
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langchain_core.messages import HumanMessage
from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith.utils import tracing_is_enabled
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wobot.agent.streaming import Played, play
from wobot.api.accounts import register, settings_of
from wobot.api.auth import Principal, current_principal
from wobot.api.conversations import owned, thread_of
from wobot.api.errors import ApiError
from wobot.config import get_settings
from wobot.knowledge.models import ActiveKnowledge
from wobot.models import Conversation, Turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/conversations")

TEXT_LIMIT = 2000  # characters; the table's CHECK holds the same


class TurnRequest(BaseModel):
    # Made by the app: sent again after a lost connection, it finds the same turn.
    turn_id: uuid.UUID
    text: str


@dataclass(frozen=True)
class Started:
    """A turn saved as running, with what it runs on."""

    turn_id: uuid.UUID
    conversation_id: uuid.UUID
    attempt: int
    text: str
    chatbot_name: str
    index_version: int
    head_checkpoint_id: str | None


def in_progress() -> ApiError:
    return ApiError(
        409, "TURN_IN_PROGRESS", "A reply is already being written in this chat. Try again soon."
    )


def unavailable() -> ApiError:
    return ApiError(503, "PROVIDER_UNAVAILABLE", "Chat is unavailable right now. Try again later.")


async def _next_seq(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    last = await session.scalar(
        select(func.max(Turn.seq)).where(Turn.conversation_id == conversation_id)
    )
    return (last or 0) + 1


async def start_turn(
    conversation_id: uuid.UUID,
    body: TurnRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> Started:
    """Saves the message as a running turn, or refuses it before the stream opens."""
    text = body.text.strip()
    if not 1 <= len(text) <= TEXT_LIMIT:
        raise ApiError(422, "INVALID_INPUT", f"A message must be 1 to {TEXT_LIMIT} characters.")
    if request.app.state.graph is None:
        raise unavailable()
    try:
        async with request.app.state.sessionmaker() as session, session.begin():
            await register(session, principal)
            conversation = await owned(session, principal, conversation_id, lock=True)
            existing = await session.get(Turn, body.turn_id)
            if existing is not None:
                if existing.status == "running":
                    raise in_progress()
                raise ApiError(409, "TURN_EXISTS", "This message was already sent.")
            version = await session.scalar(select(ActiveKnowledge.index_version_id))
            if version is None:
                raise unavailable()
            settings = await settings_of(session, principal.account_id)
            turn = Turn(
                turn_id=body.turn_id,
                conversation_id=conversation_id,
                seq=await _next_seq(session, conversation_id),
                user_text=text,
                status="running",
                index_version=version,
                app_version=get_settings().app_version,
            )
            session.add(turn)
            conversation.updated_at = func.now()
            await session.flush()
    except IntegrityError as exc:
        # Another device's turn got here first: the database allows one running turn.
        if "turns_one_running" in str(exc.orig):
            raise in_progress() from exc
        raise
    return Started(
        turn_id=turn.turn_id,
        conversation_id=conversation_id,
        attempt=turn.attempt,
        text=text,
        chatbot_name=settings.chatbot_name,
        index_version=version,
        head_checkpoint_id=conversation.head_checkpoint_id,
    )


async def finish(
    sessionmaker: async_sessionmaker,
    started: Started,
    status: str,
    reply_text: str | None,
    reply: dict[str, Any] | None = None,
    route: str | None = None,
    error_code: str | None = None,
    checkpoint_id: str | None = None,
) -> bool:
    """Ends the turn, if it is still this attempt's and running: a compare-and-swap, so a
    late write never overturns an end already written. A completed turn moves the
    conversation's head to the checkpoint it saved. False when nothing was ended."""
    async with sessionmaker() as session, session.begin():
        ended = await session.execute(
            update(Turn)
            .where(
                Turn.turn_id == started.turn_id,
                Turn.attempt == started.attempt,
                Turn.status == "running",
            )
            .values(
                status=status,
                reply_text=reply_text,
                reply=reply,
                route=route,
                error_code=error_code,
                finished_at=func.now(),
            )
        )
        if ended.rowcount != 1:
            return False
        moved: dict[str, Any] = {"updated_at": func.now()}
        if status == "completed" and checkpoint_id:
            moved["head_checkpoint_id"] = checkpoint_id
        await session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == started.conversation_id)
            .values(**moved)
        )
    return True


# What of the reply the client is sent: the words, and the products a card or the avatar
# shows. The lists' record keys stay in the turn's row, for later use, not on the wire.
SHOWN_REPLY = ("text", "action", "products")


def _event(name: str, started: Started, **data: Any) -> ServerSentEvent:
    data = {"turn_id": str(started.turn_id), "attempt": started.attempt, **data}
    # As UTF-8, not \u escapes: a Chinese reply would be three times the bytes.
    return ServerSentEvent(event=name, raw_data=json.dumps(data, ensure_ascii=False))


@router.post("/{conversation_id}/turns", response_class=EventSourceResponse)
async def send_turn(
    request: Request, started: Annotated[Started, Depends(start_turn)]
) -> AsyncIterable[ServerSentEvent]:
    """The reply to one message, as `accepted`, `status`, `text_delta`, `reply`, then
    `completed`, or `error` when it failed."""
    begun = time.perf_counter()
    state = request.app.state
    played = Played()
    ended = "failed"
    yield _event("accepted", started)
    try:
        thread = thread_of(started.conversation_id)
        if started.head_checkpoint_id is None:
            # No turn has completed: any checkpoint on the thread is from one that failed
            # or stopped, and must not become this turn's memory.
            await state.checkpointer.adelete_thread(thread)
        config = {"configurable": {"thread_id": thread}}
        if started.head_checkpoint_id is not None:
            config["configurable"]["checkpoint_id"] = started.head_checkpoint_id
        turn = {
            "messages": [HumanMessage(started.text)],
            "index_version": started.index_version,
            "query_time": datetime.now(UTC),
            "chatbot_name": started.chatbot_name,
        }
        async for name, data in play(state.graph, turn, config, played):
            yield _event(name, started, **data)

        reply = {k: v for k, v in played.reply.items() if k != "retryable"}
        yield _event("reply", started, **{k: v for k, v in reply.items() if k in SHOWN_REPLY})
        if played.reply.get("retryable"):
            # The graph said a model or a lookup failed; it may work if tried again.
            await finish(
                state.sessionmaker,
                started,
                "failed",
                played.reply.get("text"),
                reply,
                played.route,
                "PROVIDER_UNAVAILABLE",
            )
            yield _event("error", started, **_error("PROVIDER_UNAVAILABLE"))
        elif await finish(
            state.sessionmaker,
            started,
            "completed",
            played.reply.get("text"),
            reply,
            played.route,
            checkpoint_id=played.checkpoint_id,
        ):
            ended = "completed"
            yield _event("completed", started)
        else:
            ended = "superseded"  # ended elsewhere meanwhile, such as stopped
    except Exception:
        logger.exception("turn failed")
        with anyio.CancelScope(shield=True):
            await finish(
                state.sessionmaker,
                started,
                "failed",
                played.shown_text or None,
                route=played.route,
                error_code="INTERNAL_ERROR",
            )
        yield _event("error", started, **_error("INTERNAL_ERROR"))
    except BaseException:
        # The client went away, or the server is stopping: the turn is stopped, and keeps
        # what was already sent (CHAT-012). Shielded, or the write would be cancelled too.
        ended = "cancelled"
        with anyio.CancelScope(shield=True):
            await finish(
                state.sessionmaker,
                started,
                "cancelled",
                played.shown_text or None,
                route=played.route,
            )
        raise
    finally:
        logger.info(
            "turn finished",
            extra={
                "turn_id": str(started.turn_id),
                "attempt": started.attempt,
                "status": ended,
                "route": played.route,
                "nodes": played.nodes,
                "first_status_s": played.first_status,
                "first_token_s": played.first_token,
                "total_s": round(time.perf_counter() - begun, 3),
                "index_version": started.index_version,
            },
        )
        if tracing_is_enabled():
            # Traces leave from a background thread. Cloud Run gives an instance CPU only
            # while a request is open, so they are sent before this one closes.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(wait_for_all_tracers)


ERRORS = {
    "PROVIDER_UNAVAILABLE": "The reply could not be written just now. Try again.",
    "INTERNAL_ERROR": "Something went wrong. Try again.",
}


def _error(code: str) -> dict[str, Any]:
    return {"code": code, "message": ERRORS[code], "retryable": True}
