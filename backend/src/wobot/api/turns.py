"""A turn: the user's message in, the reply streamed out as server-sent events (DEC-080).

Everything that can refuse the message happens before the stream opens, as an ordinary
HTTP error: a bad message, another account's conversation, a turn already running. Then
the turn is saved as running, `accepted` is sent, and the graph runs inside this
request: Cloud Run gives an instance CPU only while a request is open, so work handed to
a background task after the response could stall. No database connection is held while
the graph runs; the turn is saved again only when it ends.

A conversation's graph memory moves forward only when a turn completes: the run starts
from the checkpoint the conversation points at, and the conversation then points at the
one the run saved (DEC-079). A turn that fails or is stopped leaves it where it was, so
trying it again is the same run from the same memory, and the question is never asked
twice.

The request that runs a turn and the one that stops it may reach different instances,
so they meet only in the database: every end is a compare-and-swap on the turn's ID,
attempt and status, and whichever is written first stands. The app's closing its stream
is no stop on Cloud Run: the front end keeps its own connection to the instance open,
so the run never hears of it. A stop is the cancel request, which the running request
reads back from the database while it streams.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langchain_core.messages import HumanMessage
from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith.utils import tracing_is_enabled
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wobot.agent.streaming import Played, play
from wobot.agent.title import shortened, write_title
from wobot.api.accounts import register, settings_of
from wobot.api.auth import Principal, current_principal
from wobot.api.conversations import end_stale, owned, thread_of
from wobot.api.errors import ApiError
from wobot.config import get_settings
from wobot.knowledge.models import ActiveKnowledge
from wobot.models import Conversation, Turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/conversations")
control = APIRouter(prefix="/v1/turns")

Owner = Annotated[Principal, Depends(current_principal)]

TEXT_LIMIT = 2000  # characters; the table's CHECK holds the same
TURN_SECONDS = 90  # a turn still running then fails, and may be tried again (DEC-079)
CHECK_SECONDS = 1.0  # how often a running turn reads whether it was stopped
SHOWN_LIMIT = 20_000  # characters of a stopped reply the app may send back
TITLE_WAIT_SECONDS = 1.0  # how long a finished reply waits for its title


class TurnRequest(BaseModel):
    # Made by the app: sent again after a lost connection, it finds the same turn.
    turn_id: uuid.UUID
    text: str


class CancelRequest(BaseModel):
    attempt: int  # the attempt the app is showing; a later one is not stopped by it
    # The words the app had shown: only it knows where the reader saw the reply stop.
    shown_text: str | None = Field(default=None, max_length=SHOWN_LIMIT)


class RetryRequest(BaseModel):
    expected_attempt: int  # the attempt the app last showed, so two retries make one


class TurnState(BaseModel):
    turn_id: uuid.UUID
    attempt: int
    status: str


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
    needs_title: bool = False  # the conversation has none yet: this turn writes it


class TurnTimedOut(Exception):
    """The turn ran past TURN_SECONDS."""


class TurnStopped(Exception):
    """The turn was ended elsewhere while it ran: stopped, or its conversation deleted."""


def in_progress() -> ApiError:
    return ApiError(
        409, "TURN_IN_PROGRESS", "A reply is already being written in this chat. Try again soon."
    )


def unavailable() -> ApiError:
    return ApiError(503, "PROVIDER_UNAVAILABLE", "Chat is unavailable right now. Try again later.")


def turn_not_found() -> ApiError:
    # Also for another account's turn, as for its conversations.
    return ApiError(404, "RESOURCE_NOT_FOUND", "This message does not exist.")


async def _next_seq(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    last = await session.scalar(
        select(func.max(Turn.seq)).where(Turn.conversation_id == conversation_id)
    )
    return (last or 0) + 1


async def _active_version(session: AsyncSession) -> int:
    version = await session.scalar(select(ActiveKnowledge.index_version_id))
    if version is None:
        raise unavailable()
    return version


async def owned_turn(session: AsyncSession, principal: Principal, turn_id: uuid.UUID) -> Turn:
    """The account's turn, or 404."""
    turn = await session.scalar(
        select(Turn)
        .join(Conversation)
        .where(Turn.turn_id == turn_id, Conversation.owner_id == principal.account_id)
    )
    if turn is None:
        raise turn_not_found()
    return turn


async def start_turn(
    conversation_id: uuid.UUID, body: TurnRequest, request: Request, principal: Owner
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
            await end_stale(session, conversation_id)
            existing = await session.get(Turn, body.turn_id)
            if existing is not None:
                if existing.status == "running":
                    raise in_progress()
                raise ApiError(409, "TURN_EXISTS", "This message was already sent.")
            version = await _active_version(session)
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
        needs_title=conversation.title is None,
    )


async def restart_turn(
    turn_id: uuid.UUID, body: RetryRequest, request: Request, principal: Owner
) -> Started:
    """Saves a stopped or failed turn as running again, as its next attempt (D14): only
    the conversation's last turn, so no later turn ever answered a reply that changed."""
    if request.app.state.graph is None:
        raise unavailable()
    async with request.app.state.sessionmaker() as session, session.begin():
        found = await owned_turn(session, principal, turn_id)
        conversation = await owned(session, principal, found.conversation_id, lock=True)
        await end_stale(session, conversation.conversation_id)
        turn = await session.get(Turn, turn_id, populate_existing=True)
        if turn.status == "running":
            raise in_progress()
        if turn.attempt != body.expected_attempt:
            raise ApiError(409, "REVISION_CONFLICT", "This reply was already tried again.")
        last = await session.scalar(
            select(func.max(Turn.seq)).where(Turn.conversation_id == turn.conversation_id)
        )
        if turn.status == "completed" or turn.seq != last:
            raise ApiError(
                409,
                "TURN_NOT_RETRYABLE",
                "Only the last reply can be tried again, if it was stopped or failed.",
            )
        version = await _active_version(session)
        settings = await settings_of(session, principal.account_id)
        # The attempt before is replaced: a turn keeps only its latest reply (DEC-079).
        turn.status = "running"
        turn.attempt += 1
        turn.reply_text = turn.reply = turn.route = turn.error_code = None
        turn.index_version = version
        turn.app_version = get_settings().app_version
        turn.started_at = func.now()
        turn.finished_at = None
        conversation.updated_at = func.now()
    return Started(
        turn_id=turn.turn_id,
        conversation_id=turn.conversation_id,
        attempt=turn.attempt,
        text=turn.user_text,
        chatbot_name=settings.chatbot_name,
        index_version=version,
        head_checkpoint_id=conversation.head_checkpoint_id,
        needs_title=conversation.title is None,
    )


async def finish(
    state: Any,
    started: Started,
    status: str,
    reply_text: str | None,
    reply: dict[str, Any] | None = None,
    route: str | None = None,
    error_code: str | None = None,
    checkpoint_id: str | None = None,
) -> bool:
    """Ends the turn, if this attempt is still running: a compare-and-swap, so a late
    write never overturns an end already written, such as a stop. A completed turn moves
    the conversation's head to the checkpoint it saved. False when nothing was ended.

    A stop the app already saved is completed here with what was shown, which only this
    request knows. A conversation deleted meanwhile is held to the delete's end, and
    then the checkpoint this run saved after it is deleted too."""
    async with state.sessionmaker() as session, session.begin():
        # Held first, as a delete holds it, so the two end in one order or the other.
        kept = await session.scalar(
            select(Conversation.conversation_id)
            .where(Conversation.conversation_id == started.conversation_id)
            .with_for_update()
        )
        ended = 0
        if kept is not None:
            this = (Turn.turn_id == started.turn_id, Turn.attempt == started.attempt)
            if status == "cancelled":
                query = (
                    update(Turn)
                    .where(*this, Turn.status.in_(("running", "cancelled")))
                    .values(
                        status=status,
                        reply_text=func.coalesce(Turn.reply_text, reply_text),
                        route=func.coalesce(Turn.route, route),
                        finished_at=func.coalesce(Turn.finished_at, func.now()),
                    )
                )
            else:
                query = (
                    update(Turn)
                    .where(*this, Turn.status == "running")
                    .values(
                        status=status,
                        reply_text=reply_text,
                        reply=reply,
                        route=route,
                        error_code=error_code,
                        finished_at=func.now(),
                    )
                )
            ended = (await session.execute(query)).rowcount
            if ended == 1:
                moved: dict[str, Any] = {"updated_at": func.now()}
                if status == "completed" and checkpoint_id:
                    moved["head_checkpoint_id"] = checkpoint_id
                await session.execute(
                    update(Conversation)
                    .where(Conversation.conversation_id == started.conversation_id)
                    .values(**moved)
                )
    if kept is None:
        await state.checkpointer.adelete_thread(thread_of(started.conversation_id))
    return ended == 1


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
    async for event in run_turn(request.app.state, started):
        yield event


@control.post("/{turn_id}/retry", response_class=EventSourceResponse)
async def retry_turn(
    request: Request, started: Annotated[Started, Depends(restart_turn)]
) -> AsyncIterable[ServerSentEvent]:
    """A stopped or failed reply written again, streamed as a turn's is."""
    async for event in run_turn(request.app.state, started):
        yield event


@control.post("/{turn_id}/cancel")
async def cancel_turn(
    turn_id: uuid.UUID, body: CancelRequest, request: Request, principal: Owner
) -> TurnState:
    """Stops a reply being written, keeping the words the app had shown (CHAT-012). The
    request running the turn reads the stop within a second, stops the graph and ends
    its stream without a reply. Sent again, or for an attempt already ended, it changes
    nothing and says how the turn stands."""
    async with request.app.state.sessionmaker() as session, session.begin():
        await owned_turn(session, principal, turn_id)
        await session.execute(
            update(Turn)
            .where(
                Turn.turn_id == turn_id,
                Turn.attempt == body.attempt,
                Turn.status == "running",
            )
            .values(status="cancelled", reply_text=body.shown_text or None, finished_at=func.now())
        )
        turn = await session.get(Turn, turn_id, populate_existing=True)
    return TurnState(turn_id=turn.turn_id, attempt=turn.attempt, status=turn.status)


async def still_running(state: Any, started: Started) -> bool:
    async with state.sessionmaker() as session:
        status = await session.scalar(
            select(Turn.status).where(
                Turn.turn_id == started.turn_id, Turn.attempt == started.attempt
            )
        )
    return status == "running"


async def keep_title(state: Any, started: Started, title: str) -> bool:
    """Keeps the conversation's title, unless it has one by now. It never raises: a turn
    goes on without one."""
    try:
        async with state.sessionmaker() as session, session.begin():
            named = await session.execute(
                update(Conversation)
                .where(
                    Conversation.conversation_id == started.conversation_id,
                    Conversation.title.is_(None),
                )
                .values(title=title)
            )
    except Exception:
        logger.warning("the title could not be kept", exc_info=True)
        return False
    return named.rowcount == 1


async def run_turn(state: Any, started: Started) -> AsyncIterable[ServerSentEvent]:
    """Runs a started turn and streams it, then saves how it ended."""
    begun = time.perf_counter()
    played = Played()
    ended = "failed"
    # The title is written alongside the reply, so it costs the reply no time (D16).
    titling = (
        asyncio.create_task(write_title(state.title_model, started.text))
        if started.needs_title
        else None
    )
    told = False

    async def titled() -> AsyncIterable[ServerSentEvent]:
        """The `title` event, once the title is kept: told before the turn's last event."""
        nonlocal told
        if titling is None or told:
            return
        told = True
        try:
            title = await asyncio.wait_for(asyncio.shield(titling), TITLE_WAIT_SECONDS)
        except TimeoutError:
            # Slower than the reply: the question stands in, rather than keep it waiting.
            titling.cancel()
            title = shortened(started.text)
        if await keep_title(state, started, title):
            yield _event("title", started, title=title)

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
        # The graph runs in a task of its own, so this loop can wait for its next event
        # a second at a time, to read whether the turn was stopped, without cutting into
        # it. A timeout scope never spans a yield: it would cancel whoever iterates this.
        events: asyncio.Queue = asyncio.Queue()

        async def run_graph() -> None:
            try:
                async with aclosing(play(state.graph, turn, config, played)) as stream:
                    async for event in stream:
                        events.put_nowait(event)
            finally:
                events.put_nowait(None)

        graph = asyncio.create_task(run_graph())
        try:
            deadline = time.monotonic() + TURN_SECONDS
            checked = time.monotonic()
            while True:
                now = time.monotonic()
                if now >= deadline:
                    raise TurnTimedOut
                if now - checked >= CHECK_SECONDS:
                    checked = now
                    if not await still_running(state, started):
                        raise TurnStopped
                try:
                    wait = min(CHECK_SECONDS, deadline - now)
                    event = await asyncio.wait_for(events.get(), timeout=wait)
                except TimeoutError:
                    continue
                if event is None:
                    await graph  # raises what the graph raised
                    break
                name, data = event
                yield _event(name, started, **data)
        finally:
            if not graph.done():
                graph.cancel()
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(graph, return_exceptions=True)

        reply = {k: v for k, v in played.reply.items() if k != "retryable"}
        shown = _event("reply", started, **{k: v for k, v in reply.items() if k in SHOWN_REPLY})
        # Each end is saved before it is told: a turn stopped meanwhile sends nothing more.
        if played.reply.get("retryable"):
            # The graph said a model or a lookup failed; it may work if tried again.
            if await finish(
                state,
                started,
                "failed",
                played.reply.get("text"),
                reply,
                played.route,
                "PROVIDER_UNAVAILABLE",
            ):
                yield shown
                async for event in titled():
                    yield event
                yield _event("error", started, **_error("PROVIDER_UNAVAILABLE"))
            else:
                ended = "superseded"
        elif await finish(
            state,
            started,
            "completed",
            played.reply.get("text"),
            reply,
            played.route,
            checkpoint_id=played.checkpoint_id,
        ):
            ended = "completed"
            yield shown
            async for event in titled():
                yield event
            yield _event("completed", started)
        else:
            ended = "superseded"  # ended elsewhere meanwhile, such as stopped
    except TurnStopped:
        # The stop is saved already; the words sent are kept unless the app sent its own.
        ended = "stopped"
        with anyio.CancelScope(shield=True):
            await finish(state, started, "cancelled", played.shown_text or None, route=played.route)
    except TurnTimedOut:
        ended = "timed out"
        with anyio.CancelScope(shield=True):
            await finish(
                state,
                started,
                "failed",
                played.shown_text or None,
                route=played.route,
                error_code="TURN_TIMEOUT",
            )
        async for event in titled():
            yield event
        yield _event("error", started, **_error("TURN_TIMEOUT"))
    except Exception:
        logger.exception("turn failed")
        with anyio.CancelScope(shield=True):
            await finish(
                state,
                started,
                "failed",
                played.shown_text or None,
                route=played.route,
                error_code="INTERNAL_ERROR",
            )
        async for event in titled():
            yield event
        yield _event("error", started, **_error("INTERNAL_ERROR"))
    except BaseException:
        # The client went away, or the server is stopping: the turn is stopped, and keeps
        # what was already sent (CHAT-012). Shielded, or the write would be cancelled too.
        ended = "cancelled"
        with anyio.CancelScope(shield=True):
            await finish(
                state,
                started,
                "cancelled",
                played.shown_text or None,
                route=played.route,
            )
        raise
    finally:
        if titling is not None and not told:
            # A stopped turn still names its conversation: the request stays open till
            # then, as Cloud Run gives CPU only while it is.
            with anyio.CancelScope(shield=True):
                [title] = await asyncio.gather(titling, return_exceptions=True)
                if not isinstance(title, str):
                    title = shortened(started.text)
                await keep_title(state, started, title)
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
    "TURN_TIMEOUT": "The reply took too long. Try again.",
    "INTERNAL_ERROR": "Something went wrong. Try again.",
}


def _error(code: str) -> dict[str, Any]:
    return {"code": code, "message": ERRORS[code], "retryable": True}
