"""What voices and avatars share (DEC-086): each is a version the `wobot-assets` job makes,
one is in use at a time, and another account's is answered as one that does not exist.

The job is started from here. A start that fails, or whose answer is lost, leaves the
version queued, and reading it starts it again; the worker's claim makes a second start
harmless.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wobot.api.accounts import register
from wobot.api.auth import Principal
from wobot.api.errors import ApiError
from wobot.assets.dispatch import DispatchError
from wobot.assets.worker import AVATAR, VOICE, Kind

# A queued version no execution has claimed by then lost its start: start it again. Jobs
# take 10-20 s to start.
RESTART_AFTER = timedelta(seconds=60)
# A running version whose worker stopped beating this long ago has lost its worker.
LOST_AFTER = timedelta(minutes=5)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Asset:
    noun: str  # in messages and the job's arguments
    kind: Kind
    not_ready_code: str

    @property
    def model(self):
        return self.kind.model

    def id_column(self):
        return self.kind.id_of()

    def not_found(self) -> ApiError:
        return ApiError(404, "RESOURCE_NOT_FOUND", f"This {self.noun} does not exist.")

    def not_ready(self) -> ApiError:
        return ApiError(409, self.not_ready_code, f"This {self.noun} is not ready yet.")


VOICE_ASSET = Asset("voice", VOICE, "VOICE_NOT_READY")
AVATAR_ASSET = Asset("avatar", AVATAR, "AVATAR_NOT_READY")


async def owned(session: AsyncSession, asset: Asset, principal: Principal, version_id: uuid.UUID):
    version = await session.get(asset.model, version_id, populate_existing=True)
    if version is None or version.owner_id != principal.account_id:
        raise asset.not_found()
    return version


async def start(request: Request, *args: str) -> str | None:
    """Starts the job; a failed start only logs, and `GET` starts it again later."""
    try:
        return await request.app.state.dispatcher.dispatch(*args)
    except DispatchError as exc:
        logger.warning("could not start the asset job: %s", exc)
        return None


async def existing(request: Request, asset: Asset, principal: Principal, version_id: uuid.UUID):
    """The version a request sent again made, or None for a new one."""
    async with request.app.state.sessionmaker() as session:
        version = await session.get(asset.model, version_id)
    if version is not None and version.owner_id != principal.account_id:
        raise ApiError(409, "ASSET_EXISTS", f"This {asset.noun} ID is already used.")
    return version


async def add(
    request: Request,
    asset: Asset,
    principal: Principal,
    version_id: uuid.UUID,
    rows: Callable[[], Iterable[object]],
) -> None:
    """Adds the queued version, with `rows` (the version first), and starts making it."""
    sessionmaker = request.app.state.sessionmaker
    try:
        async with sessionmaker() as session, session.begin():
            await register(session, principal)
            for row in rows():
                session.add(row)
                await session.flush()  # the version before the rows that refer to it
    except IntegrityError as exc:
        async with sessionmaker() as session:
            raced = await session.get(asset.model, version_id)
        if raced is not None and raced.owner_id == principal.account_id:
            return  # the same request, sent twice at once
        if f"{asset.model.__tablename__}_one_making" in str(exc.orig):
            raise in_progress(asset) from exc
        raise
    await make(request, asset, version_id)


def in_progress(asset: Asset) -> ApiError:
    return ApiError(
        409,
        "ASSET_IN_PROGRESS",
        f"A {asset.noun} is already being made. Try again when it ends.",
    )


async def make(request: Request, asset: Asset, version_id: uuid.UUID) -> None:
    """Starts the job on a queued version and records the execution it started."""
    execution = await start(request, "make", asset.noun, str(version_id))
    if execution is None:
        return
    async with request.app.state.sessionmaker() as session, session.begin():
        await session.execute(
            update(asset.model)
            .where(asset.id_column() == version_id, asset.model.status == "queued")
            .values(execution=execution)
        )


async def reconcile(
    request: Request,
    asset: Asset,
    principal: Principal,
    version_id: uuid.UUID,
    on_lost: Callable[[AsyncSession, uuid.UUID], Awaitable[None]] | None = None,
) -> None:
    """Starts again a version that lost its start, and gives up one that lost its worker.
    Only one request does either, by a conditional update."""
    async with request.app.state.sessionmaker() as session, session.begin():
        await owned(session, asset, principal, version_id)
        restart = await session.scalar(
            update(asset.model)
            .where(
                asset.id_column() == version_id,
                asset.model.status == "queued",
                asset.model.updated_at < func.now() - RESTART_AFTER,
            )
            .values(updated_at=func.now())
            .returning(asset.id_column())
        )
        lost = await session.scalar(
            update(asset.model)
            .where(
                asset.id_column() == version_id,
                asset.model.status == "running",
                asset.model.heartbeat_at < func.now() - LOST_AFTER,
            )
            .values(
                status="failed",
                error_code="WORKER_LOST",
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(asset.id_column())
        )
        if lost is not None and on_lost is not None:
            await on_lost(session, version_id)
    if restart is not None:
        await start(request, "make", asset.noun, str(version_id))


async def activate(
    request: Request,
    asset: Asset,
    principal: Principal,
    version_id: uuid.UUID,
    expected_active: uuid.UUID | None,
    complete: Callable[[AsyncSession, object], Awaitable[bool]] | None = None,
) -> None:
    """Puts a ready version to use in place of the active one, which is retired first, in
    the same transaction: the index allows one active, checked row by row, and no reader
    sees the moment with none. The retired one is then cleaned by the job."""
    conflict = ApiError(
        409, "REVISION_CONFLICT", f"The {asset.noun} in use was changed on another device. Reload."
    )
    try:
        async with request.app.state.sessionmaker() as session, session.begin():
            version = await owned(session, asset, principal, version_id)
            if version.status == "active":
                return  # sent again
            if version.status != "ready":
                raise asset.not_ready()
            if complete is not None and not await complete(session, version):
                raise asset.not_ready()
            current = await session.scalar(
                select(asset.id_column()).where(
                    asset.model.owner_id == principal.account_id, asset.model.status == "active"
                )
            )
            if current != expected_active:
                raise conflict
            if current is not None:
                await _move(session, asset, current, "active", "retired")
            if not await _move(session, asset, version_id, "ready", "active"):
                raise conflict
    except IntegrityError as exc:
        if f"{asset.model.__tablename__}_one_active" in str(exc.orig):
            raise conflict from exc  # another device activated one at the same moment
        raise
    if current is not None:
        await start(request, "clean", asset.noun, str(current))


async def delete(request: Request, asset: Asset, principal: Principal, version_id: uuid.UUID):
    """Takes the version out of use at once, then has the job remove what it left. Avatar
    mode needs both a voice and an avatar, so deleting the active one ends it."""
    async with request.app.state.sessionmaker() as session, session.begin():
        version = await owned(session, asset, principal, version_id)
        if version.status != "deleted":
            version.status = "deleted"
            version.finished_at = version.finished_at or func.now()
            version.updated_at = func.now()
        cleaned = version.cleaned_at is not None
    if not cleaned:  # sent again before the removal finished, it starts it again
        await start(request, "clean", asset.noun, str(version_id))


async def _move(
    session: AsyncSession, asset: Asset, version_id: uuid.UUID, old: str, new: str
) -> bool:
    result = await session.execute(
        update(asset.model)
        .where(asset.id_column() == version_id, asset.model.status == old)
        .values(status=new, updated_at=func.now())
    )
    return result.rowcount == 1
