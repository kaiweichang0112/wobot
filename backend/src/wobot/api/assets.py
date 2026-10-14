"""The account's voice and avatar: which version is in use, and which is on its way."""

import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wobot.models import AvatarVersion, VoiceVersion

# Versions no longer offered: replaced by a newer one, or removed.
GONE = ("retired", "deleted")


class AssetVersionResponse(BaseModel):
    id: uuid.UUID
    status: str  # queued, running, ready, failed or active
    error_code: str | None = None


class AssetResponse(BaseModel):
    active: AssetVersionResponse | None  # the version in use
    # The newest version when it is not the one in use: being made, made and waiting to
    # be activated, or failed. The active one stays in use meanwhile (AVA-009, VOI-013).
    candidate: AssetVersionResponse | None


async def asset_of(
    session: AsyncSession, model: type[VoiceVersion] | type[AvatarVersion], owner_id: str
) -> AssetResponse:
    """Read from the database on every call: a cached flag could let a deleted voice or
    avatar into Avatar mode (spec 23)."""
    key = model.voice_id if model is VoiceVersion else model.avatar_id
    columns = (key, model.status, model.error_code)
    owned = select(*columns).where(model.owner_id == owner_id)
    active = (await session.execute(owned.where(model.status == "active"))).first()
    newest = (
        await session.execute(
            owned.where(model.status.not_in(GONE))
            .order_by(model.created_at.desc(), key.desc())
            .limit(1)
        )
    ).first()
    return AssetResponse(
        active=_version(active),
        candidate=_version(newest) if newest is not None and newest.status != "active" else None,
    )


def _version(row) -> AssetVersionResponse | None:
    if row is None:
        return None
    return AssetVersionResponse(id=row[0], status=row.status, error_code=row.error_code)
