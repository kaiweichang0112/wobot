"""The daily sweep (E2): it puts right what the requests left, whoever left it.

Each step looks at how the rows stand now and moves them on, never at what happened: a
start the API could not send, a job Cloud Run stopped, ElevenLabs down for a day, all
look the same to it, and are made good the next day. Every step may run again, and a
sweep cut off halfway leaves nothing the next one does not finish.

Asset work is only handed to the `wobot-assets` job, which holds the provider keys; the
sweep holds none.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, delete, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from wobot.api.conversations import STALE_AFTER, thread_of
from wobot.api.versions import AVATAR_ASSET, LOST_AFTER, RESTART_AFTER, VOICE_ASSET, Asset
from wobot.assets.dispatch import Dispatcher, DispatchError
from wobot.models import Account, Conversation, Turn

ASSETS = (VOICE_ASSET, AVATAR_ASSET)
# A deleted account's row outlives its last ID token, valid for an hour: were it gone
# sooner, that token's next request would register an empty account under its ID.
DELETED_GRACE = timedelta(hours=1)
# A failed avatar may be tried again, reusing its frames, until a newer version replaces
# it or this long has passed; only then are its files removed.
FAILED_KEPT = timedelta(days=7)
# The graph's memory of a conversation, keyed by its thread (migration 0005). Deleted
# here in the same transaction as the conversation, rather than through the
# checkpointer, whose pool the sweep would otherwise open for this alone.
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")

logger = logging.getLogger(__name__)

Sessions = async_sessionmaker[AsyncSession]


@dataclass
class Report:
    stale_turns: int = 0
    lost_workers: int = 0
    deleted_conversations: int = 0
    deleted_accounts: int = 0
    accounts_waiting: int = 0  # deleted, with files still being removed
    cleans_started: int = 0
    makes_restarted: int = 0
    not_started: int = 0  # work the job could not be asked for; tried again tomorrow
    checkpoint_bytes: dict[str, int] = field(default_factory=dict)


async def sweep(sessions: Sessions, dispatcher: Dispatcher | None) -> Report:
    """One pass. Deletions come before cleaning, so the versions they take out of use are
    handed over the same day."""
    report = Report()
    report.stale_turns = await end_stale_turns(sessions)
    for asset in ASSETS:
        report.lost_workers += await give_up_lost_workers(sessions, asset)
    await finish_deletions(sessions, report)
    for asset in ASSETS:
        await clean_unused(sessions, dispatcher, asset, report)
        await restart_lost_starts(sessions, dispatcher, asset, report)
    report.checkpoint_bytes = await checkpoint_sizes(sessions)
    return report


async def end_stale_turns(sessions: Sessions) -> int:
    """Fails turns still running long after their time limit: their request is gone, and
    only reading the conversation would otherwise end them."""
    async with sessions() as session, session.begin():
        result = await session.execute(
            update(Turn)
            .where(Turn.status == "running", Turn.started_at < func.now() - STALE_AFTER)
            .values(status="failed", error_code="STREAM_INTERRUPTED", finished_at=func.now())
        )
    return result.rowcount


async def give_up_lost_workers(sessions: Sessions, asset: Asset) -> int:
    """Fails versions whose worker stopped beating, as reading one would."""
    model = asset.model
    async with sessions() as session, session.begin():
        result = await session.execute(
            update(model)
            .where(
                model.status == "running",
                func.coalesce(model.heartbeat_at, model.started_at) < func.now() - LOST_AFTER,
            )
            .values(
                status="failed",
                error_code="WORKER_LOST",
                finished_at=func.now(),
                updated_at=func.now(),
            )
        )
    return result.rowcount


async def finish_deletions(sessions: Sessions, report: Report) -> None:
    """For each account deleted over an hour ago: its conversations and their memory go,
    such as one started while it was being deleted; its versions are taken out of use;
    and once the job has removed every version's files, its row goes, with what refers
    to it."""
    async with sessions() as session:
        accounts = (
            await session.scalars(
                select(Account.account_id).where(Account.deleted_at < func.now() - DELETED_GRACE)
            )
        ).all()
    for account_id in accounts:
        async with sessions() as session, session.begin():
            # Held as a turn holds its conversation to end: one ending meanwhile waits,
            # then finds it gone and deletes what it saved itself.
            conversations = (
                await session.scalars(
                    select(Conversation.conversation_id)
                    .where(Conversation.owner_id == account_id)
                    .with_for_update()
                )
            ).all()
            if conversations:
                threads = [thread_of(c) for c in conversations]
                for table in CHECKPOINT_TABLES:
                    await session.execute(
                        text(f"DELETE FROM app.{table} WHERE thread_id = ANY(:threads)"),
                        {"threads": threads},
                    )
                await session.execute(
                    delete(Conversation).where(Conversation.conversation_id.in_(conversations))
                )
            left = 0
            for asset in ASSETS:
                left += await _take_out_of_use(session, asset, account_id)
            if left == 0:
                await session.execute(delete(Account).where(Account.account_id == account_id))
        report.deleted_conversations += len(conversations)
        if left == 0:
            report.deleted_accounts += 1
        else:
            report.accounts_waiting += 1


async def _take_out_of_use(session: AsyncSession, asset: Asset, account_id: str) -> int:
    """Marks the account's versions not yet removed as deleted; returns how many there are.
    One still being made is marked too: its worker then finds it gone and removes what it
    made."""
    model = asset.model
    await session.execute(
        update(model)
        .where(
            model.owner_id == account_id,
            model.cleaned_at.is_(None),
            model.status != "deleted",
        )
        .values(
            status="deleted",
            finished_at=func.coalesce(model.finished_at, func.now()),
            updated_at=func.now(),
        )
    )
    return await session.scalar(
        select(func.count()).where(model.owner_id == account_id, model.cleaned_at.is_(None))
    )


async def clean_unused(
    sessions: Sessions, dispatcher: Dispatcher | None, asset: Asset, report: Report
) -> None:
    """Hands the job every version out of use whose files remain: retired and deleted ones
    whose removal failed or never started, and failed ones no retry will need."""
    model = asset.model
    newer = aliased(model)
    replaced = exists().where(newer.owner_id == model.owner_id, newer.created_at > model.created_at)
    async with sessions() as session:
        unused = (
            await session.scalars(
                select(asset.id_column()).where(
                    model.cleaned_at.is_(None),
                    or_(
                        model.status.in_(("retired", "deleted")),
                        and_(
                            model.status == "failed",
                            or_(replaced, model.finished_at < func.now() - FAILED_KEPT),
                        ),
                    ),
                )
            )
        ).all()
    for version_id in unused:
        if await _start(dispatcher, "clean", asset.noun, str(version_id), report=report):
            report.cleans_started += 1


async def restart_lost_starts(
    sessions: Sessions, dispatcher: Dispatcher | None, asset: Asset, report: Report
) -> None:
    """Starts again versions queued long ago that no execution claimed, as reading one
    would. The update makes a request reading one meanwhile leave it to the sweep."""
    model = asset.model
    async with sessions() as session, session.begin():
        queued = (
            await session.scalars(
                update(model)
                .where(model.status == "queued", model.updated_at < func.now() - RESTART_AFTER)
                .values(updated_at=func.now())
                .returning(asset.id_column())
            )
        ).all()
    for version_id in queued:
        if await _start(dispatcher, "make", asset.noun, str(version_id), report=report):
            report.makes_restarted += 1


async def _start(dispatcher: Dispatcher | None, *args: str, report: Report) -> bool:
    if dispatcher is None:  # in development, where there is no job
        report.not_started += 1
        return False
    try:
        await dispatcher.dispatch(*args)
    except DispatchError as exc:
        logger.warning("could not start the asset job: %s", exc)
        report.not_started += 1
        return False
    return True


async def checkpoint_sizes(sessions: Sessions) -> dict[str, Any]:
    """How much the graph's memory takes, table by table: abandoned branches are kept
    until their conversation goes (P14); this says whether that ever matters."""
    async with sessions() as session:
        return {
            table: await session.scalar(
                text("SELECT pg_total_relation_size(CAST(:name AS regclass))"),
                {"name": f"app.{table}"},
            )
            for table in CHECKPOINT_TABLES
        }
