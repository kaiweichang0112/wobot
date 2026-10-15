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
from typing import Annotated, Any, Literal

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

from wobot.agent.rewrite import writes_chinese
from wobot.agent.streaming import Played, play
from wobot.agent.title import shortened, write_title
from wobot.api.accounts import register, settings_of
from wobot.api.auth import Principal, current_principal
from wobot.api.conversations import end_stale, owned, thread_of
from wobot.api.errors import ApiError
from wobot.config import get_settings
from wobot.knowledge.models import ActiveKnowledge
from wobot.models import AvatarVersion, Conversation, Turn, VoiceVersion
from wobot.speech.segments import Segmenter
from wobot.speech.spoken import PAGE_ITEMS, continue_from, list_page, speakable, spoken_text
from wobot.speech.tts import Speaker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/conversations")
control = APIRouter(prefix="/v1/turns")

Owner = Annotated[Principal, Depends(current_principal)]

TEXT_LIMIT = 2000  # characters; the table's CHECK holds the same
TURN_SECONDS = 90  # a turn still running then fails, and may be tried again (DEC-079)
CHECK_SECONDS = 1.0  # how often a running turn reads whether it was stopped
SHOWN_LIMIT = 20_000  # characters of a stopped reply the app may send back
TITLE_WAIT_SECONDS = 1.0  # how long a finished reply waits for its title
SPEECH_SECONDS = 60  # how long the voice may go on once the text is written


class TurnRequest(BaseModel):
    # Made by the app: sent again after a lost connection, it finds the same turn.
    turn_id: uuid.UUID
    text: str
    # Avatar mode also speaks the reply, in the account's voice (DEC-089).
    mode: Literal["text", "avatar"] = "text"


class PlaybackRequest(BaseModel):
    attempt: int
    # Sentences the app has played to the end; a smaller number than before changes nothing.
    heard: int | None = Field(default=None, ge=0)
    # The user interrupted: no sentence is spoken after. The text is written all the same.
    stopped: bool = False


class ReadOnRequest(BaseModel):
    attempt: int  # the attempt the app is showing: a retry may have written another list
    # Where to go on, as the last `completed` gave it; the same start gives the same page.
    start: int = Field(alias="from", ge=0)


class PlaybackState(BaseModel):
    turn_id: uuid.UUID
    attempt: int
    heard_upto: int | None
    speech_stopped: bool


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
class SpeakingVoice:
    """The voice version an Avatar-mode turn speaks in, as it stood when the turn began."""

    voice_id: uuid.UUID
    provider_voice_id: str


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
    mode: str = "text"
    voice: SpeakingVoice | None = None  # set in Avatar mode


@dataclass(frozen=True)
class Reading:
    """A page of a list reply to be read out, in the account's voice."""

    turn_id: uuid.UUID
    attempt: int
    start: int
    page: str
    next: int | None
    voice: SpeakingVoice


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


async def speaking_voice(
    request: Request, session: AsyncSession, principal: Principal
) -> SpeakingVoice:
    """The account's voice, when Avatar mode may be used: only with its own avatar and
    voice both in use, read from the database on every turn (MODE-004, spec 23)."""
    owner = principal.account_id
    avatar = await session.scalar(
        select(AvatarVersion.avatar_id).where(
            AvatarVersion.owner_id == owner, AvatarVersion.status == "active"
        )
    )
    if avatar is None:
        raise ApiError(409, "AVATAR_NOT_READY", "Set up your avatar to use Avatar mode.")
    voice = (
        await session.execute(
            select(VoiceVersion.voice_id, VoiceVersion.provider_voice_id).where(
                VoiceVersion.owner_id == owner, VoiceVersion.status == "active"
            )
        )
    ).first()
    if voice is None:
        raise ApiError(409, "VOICE_NOT_READY", "Set up your voice to use Avatar mode.")
    if request.app.state.eleven is None:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", "Voice is unavailable right now.")
    return SpeakingVoice(voice.voice_id, voice.provider_voice_id)


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
            voice = (
                await speaking_voice(request, session, principal) if body.mode == "avatar" else None
            )
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
                mode=body.mode,
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
        mode=body.mode,
        voice=voice,
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
        # Tried again in the mode it was asked in, so Avatar mode needs its voice still.
        voice = await speaking_voice(request, session, principal) if turn.mode == "avatar" else None
        version = await _active_version(session)
        settings = await settings_of(session, principal.account_id)
        # The attempt before is replaced: a turn keeps only its latest reply (DEC-079).
        turn.status = "running"
        turn.attempt += 1
        turn.reply_text = turn.reply = turn.route = turn.error_code = None
        turn.speech = turn.heard_upto = turn.speech_stopped_at = None
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
        mode=turn.mode,
        voice=voice,
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


def _event(name: str, started: Started | Reading, **data: Any) -> ServerSentEvent:
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


@control.post("/{turn_id}/playback")
async def playback(
    turn_id: uuid.UUID, body: PlaybackRequest, request: Request, principal: Owner
) -> PlaybackState:
    """How far the app has played the reply's speech, and whether the user stopped it.
    Only moves forward, so a late or repeated report changes nothing. A stop ends the
    speech, not the turn: the request running it reads the stop within a second and
    speaks no more, and the text is written and kept all the same (DEC-089)."""
    async with request.app.state.sessionmaker() as session, session.begin():
        await owned_turn(session, principal, turn_id)
        values: dict[str, Any] = {}
        if body.heard is not None:
            values["heard_upto"] = func.greatest(func.coalesce(Turn.heard_upto, 0), body.heard)
        if body.stopped:
            values["speech_stopped_at"] = func.coalesce(Turn.speech_stopped_at, func.now())
        if values:
            await session.execute(
                update(Turn)
                .where(Turn.turn_id == turn_id, Turn.attempt == body.attempt)
                .values(**values)
            )
        turn = await session.get(Turn, turn_id, populate_existing=True)
    return PlaybackState(
        turn_id=turn.turn_id,
        attempt=turn.attempt,
        heard_upto=turn.heard_upto,
        speech_stopped=turn.speech_stopped_at is not None,
    )


def nothing_to_read() -> ApiError:
    return ApiError(409, "NOTHING_TO_READ", "There is no more of this reply to read.")


async def start_reading(
    turn_id: uuid.UUID, body: ReadOnRequest, request: Request, principal: Owner
) -> Reading:
    """The page asked for, checked before the stream opens: the account's own completed
    list reply, the attempt the app shows, and its avatar and voice still in use."""
    async with request.app.state.sessionmaker() as session:
        turn = await owned_turn(session, principal, turn_id)
        reply = turn.reply or {}
        if turn.status != "completed" or turn.attempt != body.attempt or "lists" not in reply:
            raise nothing_to_read()
        page, after = list_page(
            reply["text"], body.start, PAGE_ITEMS, writes_chinese(turn.user_text)
        )
        if not page:
            raise nothing_to_read()
        # The voice in use now: a voice replaced since is gone from the provider.
        voice = await speaking_voice(request, session, principal)
    return Reading(turn.turn_id, turn.attempt, body.start, page, after, voice)


@control.post("/{turn_id}/continue-reading", response_class=EventSourceResponse)
async def continue_reading(
    request: Request, reading: Annotated[Reading, Depends(start_reading)]
) -> AsyncIterable[ServerSentEvent]:
    """The next items of a list reply, spoken as `speech` events, then `completed` with
    where the next page starts, null at the end (DEC-096). No graph runs and nothing is
    searched: the reply kept holds every item. It is no turn, so it does not wait on one
    running in the conversation, and the app's stopping it only stops playback: a page
    is short enough that what is still spoken costs little."""
    async for event in read_on(request.app.state, reading):
        yield event


async def read_on(state: Any, reading: Reading) -> AsyncIterable[ServerSentEvent]:
    begun = time.perf_counter()
    first_speech: float | None = None
    events: asyncio.Queue = asyncio.Queue()
    # Items are data: said without a feeling.
    speaker = Speaker(
        state.eleven,
        reading.voice.provider_voice_id,
        get_settings().tts_model,
        emit=lambda name, data: events.put_nowait((name, data)),
    )
    whole = Segmenter()
    for sentence in whole.add(reading.page) + whole.flush():
        if text := speakable(sentence):
            speaker.say(text)
    speaker.close()
    try:
        deadline = time.monotonic() + SPEECH_SECONDS
        while True:
            try:
                left = max(deadline - time.monotonic(), 0)
                name, data = await asyncio.wait_for(events.get(), timeout=left)
            except TimeoutError:
                logger.warning("the reading ran past its time")
                break
            if name == "speech_end":
                break
            if name == "speech" and first_speech is None:
                first_speech = round(time.perf_counter() - begun, 3)
            yield _event(name, reading, **data)
        yield _event("completed", reading, next=reading.next)
    finally:
        with anyio.CancelScope(shield=True):
            await speaker.stop()
        logger.info(
            "reading finished",
            extra={
                "turn_id": str(reading.turn_id),
                "attempt": reading.attempt,
                "from": reading.start,
                "next": reading.next,
                "first_speech_s": first_speech,
                "speech_s": speaker.seconds,
                "total_s": round(time.perf_counter() - begun, 3),
            },
        )


async def turn_state(state: Any, started: Started) -> tuple[bool, bool]:
    """Whether the turn is still running, and whether its speech was stopped."""
    async with state.sessionmaker() as session:
        row = (
            await session.execute(
                select(Turn.status, Turn.speech_stopped_at).where(
                    Turn.turn_id == started.turn_id, Turn.attempt == started.attempt
                )
            )
        ).first()
    if row is None:
        return False, True
    return row.status == "running", row.speech_stopped_at is not None


async def still_running(state: Any, started: Started) -> bool:
    return (await turn_state(state, started))[0]


async def keep_speech(state: Any, started: Started, speaker: Speaker) -> None:
    """Keeps what was spoken beside the reply. It never raises: the turn has ended."""
    speech = {
        "voice_id": str(started.voice.voice_id),
        "sentences": speaker.spoken,
        "stopped": speaker.stopped,
    }
    try:
        async with state.sessionmaker() as session, session.begin():
            await session.execute(
                update(Turn)
                .where(Turn.turn_id == started.turn_id, Turn.attempt == started.attempt)
                .values(speech=speech)
            )
    except Exception:
        logger.warning("the speech could not be kept", exc_info=True)


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

    # The graph's events and the voice's share one queue, told in the order they come.
    events: asyncio.Queue = asyncio.Queue()
    speaker = (
        Speaker(
            state.eleven,
            started.voice.provider_voice_id,
            get_settings().tts_model,
            emit=lambda name, data: events.put_nowait((name, data)),
            feel=(
                state.emotions.for_question(started.text)
                if getattr(state, "emotions", None) is not None
                else None
            ),
        )
        if started.mode == "avatar" and started.voice is not None
        else None
    )
    sentences = Segmenter()
    first_speech: float | None = None

    async def stop_speaking() -> None:
        if speaker is not None and not speaker.stopped:
            with anyio.CancelScope(shield=True):
                await speaker.stop()

    def relay(name: str, data: dict[str, Any]) -> ServerSentEvent:
        nonlocal first_speech
        if name == "speech" and first_speech is None:
            first_speech = round(time.perf_counter() - begun, 3)
        return _event(name, started, **data)

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
                    running, speech_stopped = await turn_state(state, started)
                    if not running:
                        raise TurnStopped
                    if speech_stopped:
                        await stop_speaking()
                try:
                    wait = min(CHECK_SECONDS, deadline - now)
                    event = await asyncio.wait_for(events.get(), timeout=wait)
                except TimeoutError:
                    continue
                if event is None:
                    await graph  # raises what the graph raised
                    break
                name, data = event
                if name == "text_delta" and speaker is not None and not speaker.stopped:
                    for sentence in sentences.add(data["text"]):
                        if text := speakable(sentence):
                            speaker.say(text)
                yield relay(name, data)
        finally:
            if not graph.done():
                graph.cancel()
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(graph, return_exceptions=True)

        reply = {k: v for k, v in played.reply.items() if k != "retryable"}
        shown = _event("reply", started, **{k: v for k, v in reply.items() if k in SHOWN_REPLY})
        if speaker is not None:
            if played.reply.get("retryable"):
                await stop_speaking()  # the error is shown, not spoken
            else:
                speak_rest(speaker, sentences, played, started.text)
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
            if speaker is not None:
                # The text is written and kept; the voice may still be speaking it. The
                # turn's end is told once the last sentence is, or the user stopped it.
                async for name, data in spoken(state, started, speaker, events):
                    yield relay(name, data)
            if speaker is not None:
                # Where reading a list goes on, if the user asks to hear more (DEC-096).
                yield _event("completed", started, next=continue_from(reply))
            else:
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
        await stop_speaking()
        if speaker is not None:
            with anyio.CancelScope(shield=True):
                await keep_speech(state, started, speaker)
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
                "first_speech_s": first_speech,
                "speech_s": speaker.seconds if speaker is not None else None,
                "total_s": round(time.perf_counter() - begun, 3),
                "index_version": started.index_version,
            },
        )
        if tracing_is_enabled():
            # Traces leave from a background thread. Cloud Run gives an instance CPU only
            # while a request is open, so they are sent before this one closes.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(wait_for_all_tracers)


def speak_rest(speaker: Speaker, sentences: Segmenter, played: Played, question: str) -> None:
    """Speaks what is left of the reply once the graph is done, and says no more come: the
    end of streamed words, or what is said of a reply code wrote (P16)."""
    if not speaker.stopped:
        rest = sentences.flush()
        said = spoken_text(played.route, played.reply, writes_chinese(question))
        if said is None and not played.shown:
            said = played.reply.get("text")  # written whole, not streamed
        if said is not None:
            whole = Segmenter()
            rest += whole.add(said) + whole.flush()
        for sentence in rest:
            if text := speakable(sentence):
                speaker.say(text)
    speaker.close()


async def spoken(
    state: Any, started: Started, speaker: Speaker, events: asyncio.Queue
) -> AsyncIterable[tuple[str, dict[str, Any]]]:
    """The speech still to come once the text is written, until its end, a stop by the
    user, or SPEECH_SECONDS: what the graph left in the queue first, in order."""
    deadline = time.monotonic() + SPEECH_SECONDS
    checked = time.monotonic()
    while not speaker.stopped:
        now = time.monotonic()
        if now >= deadline:
            logger.warning("the speech ran past its time")
            await speaker.stop()
            return
        if now - checked >= CHECK_SECONDS:
            checked = now
            if (await turn_state(state, started))[1]:
                await speaker.stop()
                return
        try:
            event = await asyncio.wait_for(events.get(), timeout=min(CHECK_SECONDS, deadline - now))
        except TimeoutError:
            continue
        if event is None:
            continue  # the graph's end, already read
        name, data = event
        if name == "speech_end":
            return
        if name in ("speech", "speech_error"):
            yield name, data


ERRORS = {
    "PROVIDER_UNAVAILABLE": "The reply could not be written just now. Try again.",
    "TURN_TIMEOUT": "The reply took too long. Try again.",
    "INTERNAL_ERROR": "Something went wrong. Try again.",
}


def _error(code: str) -> dict[str, Any]:
    return {"code": code, "message": ERRORS[code], "retryable": True}
