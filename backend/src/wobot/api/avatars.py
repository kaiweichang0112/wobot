"""The account's avatar: a photo turned into 21 frames, seven expressions with three mouths
each (spec 21), previewed, put to use, made again in part, or removed.

The `wobot-assets` job paints the frames (DEC-086); the app follows them appear with
`GET`. The photo is deleted once the base frame is made, so the base cannot be made again
from it: a new photo makes a new avatar.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wobot.api import versions
from wobot.api.auth import Principal, current_principal
from wobot.api.errors import ApiError
from wobot.api.versions import AVATAR_ASSET
from wobot.api.voices import ActivateRequest
from wobot.assets.avatar import PHOTO_MAX_BYTES, prepare_photo
from wobot.assets.avatar_prompts import PROMPT_VERSION, prompt_hash
from wobot.assets.frames import BASE, EXPRESSION_SCHEMA_VERSION, FRAME_KEYS, descendants
from wobot.assets.media import avatar_photo_key
from wobot.config import get_settings
from wobot.models import AvatarFrame, AvatarVersion

router = APIRouter(prefix="/v1/avatars")

Owner = Annotated[Principal, Depends(current_principal)]


class FrameResponse(BaseModel):
    key: str
    status: str  # pending, running, ready or failed
    sha256: str | None  # changes when the frame is made again: the app's cache key
    error_code: str | None


class AvatarResponse(BaseModel):
    avatar_id: uuid.UUID
    status: str
    error_code: str | None
    quality: str
    created_at: datetime
    finished_at: datetime | None
    ready_frames: int  # of 21
    frames: list[FrameResponse]  # in the canonical order


def not_editable(what: str) -> ApiError:
    return ApiError(409, "ASSET_NOT_EDITABLE", what)


async def read(request: Request, principal: Principal, avatar_id: uuid.UUID) -> AvatarResponse:
    async with request.app.state.sessionmaker() as session:
        version = await versions.owned(session, AVATAR_ASSET, principal, avatar_id)
        rows = (
            await session.scalars(select(AvatarFrame).where(AvatarFrame.avatar_id == avatar_id))
        ).all()
    by_key = {row.frame_key: row for row in rows}
    frames = [
        FrameResponse(
            key=key,
            status=by_key[key].status,
            sha256=by_key[key].sha256 if by_key[key].status == "ready" else None,
            error_code=by_key[key].error_code,
        )
        for key in FRAME_KEYS
        if key in by_key
    ]
    return AvatarResponse(
        avatar_id=version.avatar_id,
        status=version.status,
        error_code=version.error_code,
        quality=version.quality,
        created_at=version.created_at,
        finished_at=version.finished_at,
        ready_frames=sum(frame.status == "ready" for frame in frames),
        frames=frames,
    )


@router.post("", status_code=202)
async def create_avatar(
    request: Request,
    principal: Owner,
    avatar_id: Annotated[uuid.UUID, Form()],
    photo: Annotated[UploadFile, File()],
) -> AvatarResponse:
    """Stores the photo, upright and without its metadata, and starts painting the 21
    frames. Sent again with the same ID, it returns the avatar the first request made."""
    if await versions.existing(request, AVATAR_ASSET, principal, avatar_id) is None:
        content = await photo.read(PHOTO_MAX_BYTES + 1)
        if len(content) > PHOTO_MAX_BYTES:
            raise ApiError(422, "INVALID_INPUT", "The photo must be at most 10 MB.")
        try:
            prepared = await asyncio.to_thread(prepare_photo, content)
        except ValueError as exc:
            raise ApiError(422, "INVALID_INPUT", "The photo is not an image.") from exc
        await request.app.state.media.put(avatar_photo_key(avatar_id), prepared, "image/png")
        settings = get_settings()

        def rows() -> list[object]:
            version = AvatarVersion(
                avatar_id=avatar_id,
                owner_id=principal.account_id,
                status="queued",
                expression_schema_version=EXPRESSION_SCHEMA_VERSION,
                prompt_version=PROMPT_VERSION,
                prompt_hash=prompt_hash(),
                image_model=settings.avatar_image_model,
                quality=settings.avatar_quality,
            )
            return [version, *(AvatarFrame(avatar_id=avatar_id, frame_key=k) for k in FRAME_KEYS)]

        await versions.add(request, AVATAR_ASSET, principal, avatar_id, rows)
    return await read(request, principal, avatar_id)


async def _frames_lost(session: AsyncSession, avatar_id: uuid.UUID) -> None:
    """Frames a lost worker left running failed with it; a retry makes them again."""
    await session.execute(
        update(AvatarFrame)
        .where(AvatarFrame.avatar_id == avatar_id, AvatarFrame.status == "running")
        .values(status="failed", error_code="WORKER_LOST", updated_at=func.now())
    )


@router.get("/{avatar_id}")
async def get_avatar(avatar_id: uuid.UUID, request: Request, principal: Owner) -> AvatarResponse:
    """Progress, frame by frame."""
    await versions.reconcile(request, AVATAR_ASSET, principal, avatar_id, _frames_lost)
    return await read(request, principal, avatar_id)


@router.get("/{avatar_id}/frames/{frame_key}")
async def get_frame(
    avatar_id: uuid.UUID,
    frame_key: str,
    request: Request,
    principal: Owner,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """One frame as PNG. Its ETag is its SHA-256, so the app keeps it until it changes."""
    async with request.app.state.sessionmaker() as session:
        version = await versions.owned(session, AVATAR_ASSET, principal, avatar_id)
        frame = await session.get(AvatarFrame, (avatar_id, frame_key))
    if version.status == "deleted" or frame is None or frame.status != "ready":
        raise ApiError(404, "RESOURCE_NOT_FOUND", "This frame does not exist.")
    etag = f'"{frame.sha256}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    png = await request.app.state.media.get(frame.storage_key)
    if png is None:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", "The frame cannot be read right now.")
    return Response(png, media_type="image/png", headers=headers)


@router.post("/{avatar_id}/retry", status_code=202)
async def retry_avatar(avatar_id: uuid.UUID, request: Request, principal: Owner) -> AvatarResponse:
    """Makes the frames that failed, and those they would have made; ready ones stay."""
    await _remake(request, principal, avatar_id, frame_keys=None)
    return await read(request, principal, avatar_id)


@router.post("/{avatar_id}/frames/{frame_key}/redo", status_code=202)
async def redo_frame(
    avatar_id: uuid.UUID, frame_key: str, request: Request, principal: Owner
) -> AvatarResponse:
    """Makes one frame again, with every frame made from it (P9), before the avatar is put
    to use."""
    if frame_key not in FRAME_KEYS:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "This frame does not exist.")
    if frame_key == BASE:
        raise ApiError(
            422,
            "INVALID_INPUT",
            "The base frame cannot be made again: its photo is gone. Take a new photo.",
        )
    await _remake(request, principal, avatar_id, frame_keys=[frame_key, *descendants(frame_key)])
    return await read(request, principal, avatar_id)


async def _remake(
    request: Request, principal: Principal, avatar_id: uuid.UUID, frame_keys: list[str] | None
) -> None:
    """Queues the version again with the frames given set back to pending: the failed ones
    when none are given."""
    try:
        async with request.app.state.sessionmaker() as session, session.begin():
            version = await versions.owned(session, AVATAR_ASSET, principal, avatar_id)
            if frame_keys is None and version.status != "failed":
                raise not_editable("Only an avatar that failed can be tried again.")
            if frame_keys is not None and version.status not in ("ready", "failed"):
                raise not_editable("Only an avatar being previewed can have frames made again.")
            chosen = (
                AvatarFrame.status.in_(("failed", "running"))
                if frame_keys is None
                else AvatarFrame.frame_key.in_(frame_keys)
            )
            await session.execute(
                update(AvatarFrame)
                .where(AvatarFrame.avatar_id == avatar_id, chosen)
                .values(
                    status="pending",
                    storage_key=None,
                    sha256=None,
                    error_code=None,
                    updated_at=func.now(),
                )
            )
            version.status = "queued"
            version.error_code = None
            version.finished_at = None
            version.updated_at = func.now()
    except IntegrityError as exc:
        if "avatar_versions_one_making" in str(exc.orig):
            raise versions.in_progress(AVATAR_ASSET) from exc
        raise
    await versions.make(request, AVATAR_ASSET, avatar_id)


async def _all_frames_ready(session: AsyncSession, version: AvatarVersion) -> bool:
    """All 21 canonical frames made, under the current schema (AC-AVA-04). The table
    admits only canonical keys, once each."""
    ready = await session.scalar(
        select(func.count()).where(
            AvatarFrame.avatar_id == version.avatar_id, AvatarFrame.status == "ready"
        )
    )
    return (
        ready == len(FRAME_KEYS) and version.expression_schema_version == EXPRESSION_SCHEMA_VERSION
    )


@router.post("/{avatar_id}/activate")
async def activate_avatar(
    avatar_id: uuid.UUID, body: ActivateRequest, request: Request, principal: Owner
) -> AvatarResponse:
    """Puts an avatar whose 21 frames are all made to use, in place of the active one,
    whose frames are then removed."""
    await versions.activate(
        request, AVATAR_ASSET, principal, avatar_id, body.expected_active, _all_frames_ready
    )
    return await read(request, principal, avatar_id)


@router.delete("/{avatar_id}", status_code=204)
async def delete_avatar(avatar_id: uuid.UUID, request: Request, principal: Owner) -> None:
    """Takes the avatar out of use at once, then removes its frames."""
    await versions.delete(request, AVATAR_ASSET, principal, avatar_id)
