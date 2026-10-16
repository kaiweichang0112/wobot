"""The account's cloned voice: record a sample, have it cloned, hear it, use it, remove it.

A version is made by the `wobot-assets` job (DEC-086), started here; the app reads its
progress with `GET`, which also starts it again if the first start was lost. Another
account's voice is answered as one that does not exist.
"""

import io
import logging
import uuid
from datetime import datetime
from typing import Annotated

import mutagen
import mutagen.mp4
from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel

from wobot.api import versions
from wobot.api.auth import Principal, current_principal
from wobot.api.errors import ApiError
from wobot.api.versions import VOICE_ASSET
from wobot.assets.eleven import ElevenLabsError
from wobot.assets.media import voice_sample_key
from wobot.config import get_settings
from wobot.models import VoiceVersion
from wobot.speech.tts import language_of

router = APIRouter(prefix="/v1/voices")

Owner = Annotated[Principal, Depends(current_principal)]

# ElevenLabs asks for one to two minutes of clean speech; a sample is recorded in the app
# as AAC in an M4A file, about 1 MB a minute.
SAMPLE_SECONDS = (60, 180)
SAMPLE_MAX_BYTES = 10 * 1024 * 1024
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
    # The version the app last saw in use, or none: a change made meanwhile on another
    # device is not overwritten unseen.
    expected_active: uuid.UUID | None = None


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


async def read(request: Request, principal: Principal, voice_id: uuid.UUID) -> VoiceResponse:
    async with request.app.state.sessionmaker() as session:
        version = await versions.owned(session, VOICE_ASSET, principal, voice_id)
    return VoiceResponse(
        voice_id=version.voice_id,
        status=version.status,
        error_code=version.error_code,
        sample_seconds=version.sample_seconds,
        created_at=version.created_at,
        finished_at=version.finished_at,
    )


@router.post("", status_code=202)
async def create_voice(
    request: Request,
    principal: Owner,
    voice_id: Annotated[uuid.UUID, Form()],
    sample: Annotated[UploadFile, File()],
) -> VoiceResponse:
    """Stores the sample and starts cloning. Sent again with the same ID, it returns the
    version the first request made."""
    if await versions.existing(request, VOICE_ASSET, principal, voice_id) is None:
        content = await sample.read(SAMPLE_MAX_BYTES + 1)
        seconds = sample_seconds(content) if len(content) <= SAMPLE_MAX_BYTES else None
        if seconds is None or not SAMPLE_SECONDS[0] <= seconds <= SAMPLE_SECONDS[1]:
            raise invalid_sample()
        # Stored before the row: a version is never queued without its sample. A sample
        # left by a refused request goes with the bucket's one-day lifecycle.
        await request.app.state.media.put(voice_sample_key(voice_id), content, "audio/mp4")
        await versions.add(
            request,
            VOICE_ASSET,
            principal,
            voice_id,
            lambda: [
                VoiceVersion(
                    voice_id=voice_id,
                    owner_id=principal.account_id,
                    status="queued",
                    sample_seconds=round(seconds, 2),
                )
            ],
        )
    return await read(request, principal, voice_id)


@router.get("/{voice_id}")
async def get_voice(voice_id: uuid.UUID, request: Request, principal: Owner) -> VoiceResponse:
    """The version's progress."""
    await versions.reconcile(request, VOICE_ASSET, principal, voice_id)
    return await read(request, principal, voice_id)


@router.post("/{voice_id}/preview")
async def preview_voice(voice_id: uuid.UUID, request: Request, principal: Owner) -> Response:
    """A Chinese and an English sentence in the voice, as MP3, before it is put to use."""
    async with request.app.state.sessionmaker() as session:
        version = await versions.owned(session, VOICE_ASSET, principal, voice_id)
    if version.status not in ("ready", "active"):
        raise VOICE_ASSET.not_ready()
    eleven = request.app.state.eleven
    if eleven is None:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", "Voice is unavailable right now.")
    try:
        audio = await eleven.speak(
            version.provider_voice_id,
            PREVIEW_TEXT,
            get_settings().tts_model,
            language=language_of(PREVIEW_TEXT),
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
    await versions.activate(request, VOICE_ASSET, principal, voice_id, body.expected_active)
    return await read(request, principal, voice_id)


@router.delete("/{voice_id}", status_code=204)
async def delete_voice(voice_id: uuid.UUID, request: Request, principal: Owner) -> None:
    """Takes the voice out of use at once, then removes it at ElevenLabs."""
    await versions.delete(request, VOICE_ASSET, principal, voice_id)
