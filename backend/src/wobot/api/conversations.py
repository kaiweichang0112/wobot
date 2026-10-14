"""Each account's conversations: start one, list them, read one's turns, delete one.

A conversation lives in two places (DEC-079): its rows here, which the app reads, and
the graph's checkpoints under the same ID, its memory. Every route checks the owner;
another account's conversation is answered as one that does not exist.
"""

import base64
import binascii
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from wobot.api.accounts import register
from wobot.api.auth import Principal, current_principal
from wobot.api.errors import ApiError
from wobot.models import Conversation, Turn

router = APIRouter(prefix="/v1/conversations")

Owner = Annotated[Principal, Depends(current_principal)]
PageSize = Annotated[int, Query(ge=1, le=100)]

# A turn runs at most 90 s (DEC-079); one still running at 3 min lost its request.
STALE_AFTER = timedelta(minutes=3)


def thread_of(conversation_id: uuid.UUID) -> str:
    """The checkpointer's thread for a conversation: the graph's memory of it."""
    return str(conversation_id)


def not_found() -> ApiError:
    # Also for another account's conversation: whether it exists is not told (spec 06).
    return ApiError(404, "RESOURCE_NOT_FOUND", "This conversation does not exist.")


def invalid_cursor() -> ApiError:
    return ApiError(422, "INVALID_INPUT", "The request is not valid: cursor.")


class ConversationResponse(BaseModel):
    conversation_id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ConversationPage(BaseModel):
    conversations: list[ConversationResponse]
    next_cursor: str | None  # older conversations follow; None at the last page


class TurnResponse(BaseModel):
    turn_id: uuid.UUID
    seq: int
    user_text: str
    status: str
    attempt: int
    reply_text: str | None
    reply: dict[str, Any] | None
    route: str | None  # the path classify chose: chat, knowledge, list or recommend
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None


class TurnPage(BaseModel):
    turns: list[TurnResponse]  # oldest first, as a chat reads
    next_cursor: str | None  # earlier turns precede these; None when this is the start


def _response(conversation: Conversation) -> ConversationResponse:
    return ConversationResponse(
        conversation_id=conversation.conversation_id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


async def owned(
    session: AsyncSession, principal: Principal, conversation_id: uuid.UUID, lock: bool = False
) -> Conversation:
    """The account's conversation, or 404. With `lock`, its row is held until the
    transaction ends, so two turns cannot be numbered at once."""
    query = select(Conversation).where(
        Conversation.conversation_id == conversation_id,
        Conversation.owner_id == principal.account_id,
    )
    conversation = await session.scalar(query.with_for_update() if lock else query)
    if conversation is None:
        raise not_found()
    return conversation


async def end_stale(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Fails a turn still running long after its time limit: its request is gone, such as
    with an instance that stopped, and nothing else would ever end it. Done whenever the
    conversation is read or written, so it never stays locked (spec 06)."""
    await session.execute(
        update(Turn)
        .where(
            Turn.conversation_id == conversation_id,
            Turn.status == "running",
            Turn.started_at < func.now() - STALE_AFTER,
        )
        .values(status="failed", error_code="STREAM_INTERRUPTED", finished_at=func.now())
    )


# A cursor is opaque to clients: the last conversation's activity time and ID, so a page
# never repeats or skips one when two share a time.
def _encode(updated_at: datetime, conversation_id: uuid.UUID) -> str:
    raw = f"{updated_at.isoformat()}|{conversation_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        when, _, conversation_id = base64.urlsafe_b64decode(cursor).decode().partition("|")
        return datetime.fromisoformat(when), uuid.UUID(conversation_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise invalid_cursor() from exc


@router.post("", status_code=201)
async def start_conversation(request: Request, principal: Owner) -> ConversationResponse:
    """An empty conversation. The app starts one when the first message is sent, so the
    history never fills with empty ones."""
    async with request.app.state.sessionmaker() as session, session.begin():
        await register(session, principal)
        conversation = Conversation(owner_id=principal.account_id)
        session.add(conversation)
        await session.flush()
        await session.refresh(conversation)
    return _response(conversation)


@router.get("")
async def list_conversations(
    request: Request, principal: Owner, cursor: str | None = None, limit: PageSize = 30
) -> ConversationPage:
    """The account's conversations, the most recently active first."""
    recent = (Conversation.updated_at.desc(), Conversation.conversation_id.desc())
    query = select(Conversation).where(Conversation.owner_id == principal.account_id)
    if cursor is not None:
        query = query.where(
            tuple_(Conversation.updated_at, Conversation.conversation_id) < _decode(cursor)
        )
    async with request.app.state.sessionmaker() as session:
        rows = list(await session.scalars(query.order_by(*recent).limit(limit + 1)))
    page, more = rows[:limit], len(rows) > limit
    last = page[-1] if page else None
    return ConversationPage(
        conversations=[_response(c) for c in page],
        next_cursor=_encode(last.updated_at, last.conversation_id) if more and last else None,
    )


@router.get("/{conversation_id}/turns")
async def list_turns(
    conversation_id: uuid.UUID,
    request: Request,
    principal: Owner,
    cursor: str | None = None,
    limit: PageSize = 50,
) -> TurnPage:
    """The conversation's latest turns, stopped and failed ones too, with what was shown
    of their replies; a cursor reads the turns before them."""
    query = select(Turn).where(Turn.conversation_id == conversation_id)
    if cursor is not None:
        if not cursor.isdigit():
            raise invalid_cursor()
        query = query.where(Turn.seq < int(cursor))
    async with request.app.state.sessionmaker() as session, session.begin():
        await owned(session, principal, conversation_id)
        await end_stale(session, conversation_id)
        rows = list(await session.scalars(query.order_by(Turn.seq.desc()).limit(limit + 1)))
    page, more = rows[:limit][::-1], len(rows) > limit
    return TurnPage(
        turns=[TurnResponse.model_validate(t, from_attributes=True) for t in page],
        next_cursor=str(page[0].seq) if more else None,
    )


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID, request: Request, principal: Owner
) -> Response:
    """Deletes the conversation and its memory now, not later (DEC-079).

    The two stores share no transaction, so the order makes any failure safe to retry:
    the checkpoints go first, while the row still says they exist. Were the row deleted
    first and the checkpoints then failed, nothing would point at them any more.

    The row is held throughout, as a turn holds it to end: a turn still running ends
    either before, and its checkpoint is deleted here, or after, and finds the
    conversation gone and deletes its checkpoint itself.
    """
    async with request.app.state.sessionmaker() as session, session.begin():
        await owned(session, principal, conversation_id, lock=True)
        await request.app.state.checkpointer.adelete_thread(thread_of(conversation_id))
        await session.execute(
            delete(Conversation).where(
                Conversation.conversation_id == conversation_id,
                Conversation.owner_id == principal.account_id,
            )
        )
    return Response(status_code=204)
