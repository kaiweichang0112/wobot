"""The account's cloned voice: record a sample, have it cloned, hear it, use it, remove it.

A version is made by the `wobot-assets` job (DEC-086), started here; the app reads its
progress with `GET`, which also starts it again if the first start was lost. Another
account's voice is answered as one that does not exist.
"""

import io
import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated

import mutagen
import mutagen.mp4
from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wobot.api.accounts import register
from wobot.api.auth import Principal, current_principal
from wobot.api.errors import ApiError
from wobot.assets.dispatch import DispatchError
from wobot.assets.eleven import ElevenLabsError
from wobot.assets.media import voice_sample_key
from wobot.config import get_settings
from wobot.models import VoiceVersion

router = APIRouter(prefix="/v1/voices")

Owner = Annotated[Principal, Depends(current_principal)]

# ElevenLabs asks for one to two minutes of clean speech; a sample is recorded in the app
# as AAC in an M4A file, about 1 MB a minute.
SAMPLE_SECONDS = (60, 180)
SAMPLE_MAX_BYTES = 10 * 1024 * 1024
# A queued version no execution has claimed by then lost its start: start it again. Jobs
# take 10-20 s to start.
RESTART_AFTER = timedelta(seconds=60)
# A running version whose worker stopped beating this long ago has lost its worker.
LOST_AFTER = timedelta(minutes=5)
PREVIEW_TEXT = "你好，這是我的聲音。Hello, this is my voice."

logger = logging.getLogger(__name__)


class VoiceResponse(BaseModel):
    voice_id: uuid.UUID
    status: str
    error_code: str | None
    sample_seconds: float
    created_at: datetime
    finished_at: datetime | None


class ActivateRequest(BaseModel):
    # The voice the app last saw in use, or none: a change made meanwhile on another
    # device is not overwritten unseen.
    expected_active: uuid.UUID | None = None


def not_found() -> ApiError:
    return ApiError(404, "RESOURCE_NOT_FOUND", "This voice does not exist.")


def invalid_sample() -> ApiError:
    low, high = SAMPLE_SECONDS
    return ApiError(
        422, "INVALID_INPUT", f"The sample must be an M4A recording of {low} to {high} seconds."
    )


def sample_seconds(content: bytes) -> float | None:
    """The recording's length, or None when it is not an M4A file that can be read."""
    try:
        audio = mutagen.File(io.BytesIO(content))
    except mutagen.MutagenError:
        return None
    if not isinstance(audio, mutagen.mp4.MP4):
        return None
    return audio.info.length


def response_of(version: VoiceVersion) -> VoiceResponse:
    return VoiceResponse(
        voice_id=version.voice_id,
        status=version.status,
        error_code=version.error_code,
        sample_seconds=version.sample_seconds,
        created_at=version.created_at,
        finished_at=version.finished_at,
    )


async def owned_voice(
    session: AsyncSession, principal: Principal, voice_id: uuid.UUID
) -> VoiceVersion:
    version = await session.get(VoiceVersion, voice_id, populate_existing=True)
    if version is None or version.owner_id != principal.account_id:
        raise not_found()
    return version


async def start(request: Request, *args: str) -> str | None:
    """Starts the job; a failed start only logs, and `GET` starts it again later."""
    try:
        return await request.app.state.dispatcher.dispatch(*args)
    except DispatchError as exc:
        logger.warning("could not start the asset job: %s", exc)
        return None


@router.post("", status_code=202)
async def create_voice(
    request: Request,
    principal: Owner,
    voice_id: Annotated[uuid.UUID, Form()],
    sample: Annotated[UploadFile, File()],
) -> VoiceResponse:
    """Stores the sample and starts cloning. Sent again with the same ID, it returns the
    version the first request made."""
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        existing = await session.get(VoiceVersion, voice_id)
    if existing is not None:
        if existing.owner_id != principal.account_id:
            raise ApiError(409, "ASSET_EXISTS", "This voice ID is already used.")
        return response_of(existing)

    content = await sample.read(SAMPLE_MAX_BYTES + 1)
    seconds = sample_seconds(content) if len(content) <= SAMPLE_MAX_BYTES else None
    if seconds is None or not SAMPLE_SECONDS[0] <= seconds <= SAMPLE_SECONDS[1]:
        raise invalid_sample()
    # Stored before the row: a version is never queued without its sample. A sample left
    # by a refused request goes with the bucket's one-day lifecycle.
    await request.app.state.media.put(voice_sample_key(voice_id), content, "audio/mp4")
    try:
        async with sessionmaker() as session, session.begin():
            await register(session, principal)
            version = VoiceVersion(
                voice_id=voice_id,
                owner_id=principal.account_id,
                status="queued",
                sample_seconds=round(seconds, 2),
            )
            session.add(version)
    except IntegrityError as exc:
        async with sessionmaker() as session:
            raced = await session.get(VoiceVersion, voice_id)
        if raced is not None and raced.owner_id == principal.account_id:
            return response_of(raced)  # the same request, sent twice at once
        if "voice_versions_one_making" in str(exc.orig):
            raise ApiError(
                409, "ASSET_IN_PROGRESS", "A voice is already being made. Try again when it ends."
            ) from exc
        raise
    execution = await start(request, "make", "voice", str(voice_id))
    if execution is not None:
        async with sessionmaker() as session, session.begin():
            await session.execute(
                update(VoiceVersion)
                .where(VoiceVersion.voice_id == voice_id, VoiceVersion.status == "queued")
                .values(execution=execution)
            )
    async with sessionmaker() as session:
        return response_of(await owned_voice(session, principal, voice_id))


@router.get("/{voice_id}")
async def get_voice(voice_id: uuid.UUID, request: Request, principal: Owner) -> VoiceResponse:
    """The version's progress. One that lost its start is started again, and one that lost
    its worker is given up; only one request does either, by a conditional update."""
    async with request.app.state.sessionmaker() as session, session.begin():
        await owned_voice(session, principal, voice_id)
        restart = await session.scalar(
            update(VoiceVersion)
            .where(
                VoiceVersion.voice_id == voice_id,
                VoiceVersion.status == "queued",
                VoiceVersion.updated_at < func.now() - RESTART_AFTER,
            )
            .values(updated_at=func.now())
            .returning(VoiceVersion.voice_id)
        )
        await session.execute(
            update(VoiceVersion)
            .where(
                VoiceVersion.voice_id == voice_id,
                VoiceVersion.status == "running",
                VoiceVersion.heartbeat_at < func.now() - LOST_AFTER,
            )
            .values(
                status="failed",
                error_code="WORKER_LOST",
                finished_at=func.now(),
                updated_at=func.now(),
            )
        )
    if restart is not None:
        await start(request, "make", "voice", str(voice_id))
    async with request.app.state.sessionmaker() as session:
        return response_of(await owned_voice(session, principal, voice_id))


@router.post("/{voice_id}/preview")
async def preview_voice(voice_id: uuid.UUID, request: Request, principal: Owner) -> Response:
    """A Chinese and an English sentence in the voice, as MP3, before it is put to use."""
    async with request.app.state.sessionmaker() as session:
        version = await owned_voice(session, principal, voice_id)
    if version.status not in ("ready", "active"):
        raise ApiError(409, "VOICE_NOT_READY", "This voice is not ready yet.")
    eleven = request.app.state.eleven
    if eleven is None:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", "Voice is unavailable right now.")
    try:
        audio = await eleven.speak(
            version.provider_voice_id, PREVIEW_TEXT, get_settings().tts_model
        )
    except ElevenLabsError as exc:
        logger.warning("preview failed: %s", exc)
        raise ApiError(503, "PROVIDER_UNAVAILABLE", "Voice is unavailable right now.") from exc
    return Response(audio, media_type="audio/mpeg")


@router.post("/{voice_id}/activate")
async def activate_voice(
    voice_id: uuid.UUID, body: ActivateRequest, request: Request, principal: Owner
) -> VoiceResponse:
    """Puts a ready voice to use in place of the active one, which is retired and then
    removed at ElevenLabs."""
    conflict = ApiError(
        409, "REVISION_CONFLICT", "The voice in use was changed on another device. Reload."
    )
    try:
        async with request.app.state.sessionmaker() as session, session.begin():
            version = await owned_voice(session, principal, voice_id)
            if version.status == "active":
                return response_of(version)  # sent again
            if version.status != "ready":
                raise ApiError(409, "VOICE_NOT_READY", "This voice is not ready yet.")
            current = await session.scalar(
                select(VoiceVersion.voice_id).where(
                    VoiceVersion.owner_id == principal.account_id,
                    VoiceVersion.status == "active",
                )
            )
            if current != body.expected_active:
                raise conflict
            # The old one first: the index allows one active, checked row by row. In one
            # transaction, no reader sees the moment with none.
            if current is not None:
                await _move(session, current, "active", "retired")
            if not await _move(session, voice_id, "ready", "active"):
                raise conflict
    except IntegrityError as exc:
        if "voice_versions_one_active" in str(exc.orig):
            raise conflict from exc  # another device activated one at the same moment
        raise
    if current is not None:
        await start(request, "clean", "voice", str(current))
    async with request.app.state.sessionmaker() as session:
        return response_of(await owned_voice(session, principal, voice_id))


@router.delete("/{voice_id}", status_code=204)
async def delete_voice(voice_id: uuid.UUID, request: Request, principal: Owner) -> None:
    """Takes the voice out of use at once, then removes it at ElevenLabs. Avatar mode needs
    a voice, so deleting the active one ends it (MODE-004)."""
    async with request.app.state.sessionmaker() as session, session.begin():
        version = await owned_voice(session, principal, voice_id)
        if version.status != "deleted":
            version.status = "deleted"
            version.finished_at = version.finished_at or func.now()
            version.updated_at = func.now()
        cleaned = version.cleaned_at is not None
    if not cleaned:  # sent again before the removal finished, it starts it again
        await start(request, "clean", "voice", str(voice_id))


async def _move(session: AsyncSession, voice_id: uuid.UUID, old: str, new: str) -> bool:
    result = await session.execute(
        update(VoiceVersion)
        .where(VoiceVersion.voice_id == voice_id, VoiceVersion.status == old)
        .values(status=new, updated_at=func.now())
    )
    return result.rowcount == 1
