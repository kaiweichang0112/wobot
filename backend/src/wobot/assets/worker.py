"""The asset worker: one execution makes or removes one version (DEC-086).

The version row is the job. A worker claims it by moving it from queued to running, which
only one execution can do however many are started for it, and ends it with a
compare-and-swap that names its execution: a version ended elsewhere meanwhile, deleted
or given up as lost, is never overwritten. The provider has no transaction, so a voice is
named after its version and a retry finds the one an earlier attempt made; an avatar's
frames are rows of their own, so a retry makes only those still missing.

Imports nothing of the chat graph, so the job starts fast.
"""

import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wobot.assets.avatar import Blocked, FrameFailed, Painter, paint_all
from wobot.assets.eleven import ElevenLabs, ElevenLabsError, voice_name
from wobot.assets.frames import BASE, FRAME_KEYS, parent_of
from wobot.assets.media import (
    MediaStore,
    avatar_photo_key,
    avatar_prefix,
    frame_key_of,
    voice_sample_key,
)
from wobot.models import AvatarFrame, AvatarVersion, VoiceVersion

# Versions whose provider copy and files may go: nothing uses them, or ever will.
UNUSED = ("failed", "retired", "deleted")
SAMPLE_FILENAME = "sample.m4a"
# How often a long piece of work tells the database it is alive; GET gives up on a
# version whose heartbeat is 5 minutes old.
BEAT_SECONDS = 30
# What a failed avatar tells the app first, when frames failed for different reasons.
FAILURE_ORDER = ("IMAGE_REJECTED", "PHOTO_MISSING", "PROVIDER_UNAVAILABLE", "INVALID_IMAGE")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Kind:
    model: type[VoiceVersion] | type[AvatarVersion]
    key: str  # the ID column's name

    def id_of(self):
        return getattr(self.model, self.key)


VOICE = Kind(VoiceVersion, "voice_id")
AVATAR = Kind(AvatarVersion, "avatar_id")


class WorkFailed(Exception):
    """The version cannot be made; `code` is what the app is told."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Worker:
    sessionmaker: async_sessionmaker[AsyncSession]
    media: MediaStore
    eleven: ElevenLabs | None
    execution: str  # the Cloud Run execution, recorded on what it claims
    painter: Painter | None = None
    concurrency: int = 14

    # --- Voices ---------------------------------------------------------------------------

    async def make_voice(self, voice_id: uuid.UUID) -> str:
        """Clone the version's voice. Returns the outcome, for the log."""
        if not await self._claim(VOICE, voice_id):
            return "not_queued"  # another execution has it, or it has ended
        try:
            provider_voice_id = await self._clone(voice_id)
        except WorkFailed as exc:
            await self._fail(VOICE, voice_id, exc.code)
            return "failed"
        except Exception:
            await self._fail(VOICE, voice_id, "INTERNAL_ERROR")
            raise
        if not await self._finish(VOICE, voice_id, provider_voice_id=provider_voice_id):
            # Ended elsewhere while this ran: what was made has no version to belong to.
            logger.warning("voice version ended while it was made", extra={"voice": voice_id})
            await self._remove_provider_voice(provider_voice_id)
            return "superseded"
        await self.media.delete(voice_sample_key(voice_id))  # cloned: the sample goes
        return "ready"

    async def clean_voice(self, voice_id: uuid.UUID) -> str:
        """Remove an unused version's voice at ElevenLabs and its sample, then record it."""
        version = await self._unused(VOICE, voice_id)
        if version is None:
            return "nothing_to_clean"
        if self.eleven is None:
            raise WorkFailed("PROVIDER_UNAVAILABLE")
        # A clone whose version ended before it was recorded has only its name.
        provider_voice_id = version.provider_voice_id or await self.eleven.find_voice(
            voice_name(voice_id)
        )
        if provider_voice_id is not None:
            await self.eleven.delete_voice(provider_voice_id)
        await self.media.delete(voice_sample_key(voice_id))
        await self._cleaned(VOICE, voice_id)
        return "cleaned"

    async def _clone(self, voice_id: uuid.UUID) -> str:
        if self.eleven is None:
            raise WorkFailed("PROVIDER_UNAVAILABLE")
        sample = await self.media.get(voice_sample_key(voice_id))
        if sample is None:
            raise WorkFailed("SAMPLE_MISSING")
        await self._beat(VOICE, voice_id)
        name = voice_name(voice_id)
        try:
            # An earlier attempt may have cloned it and died before recording the ID.
            found = await self.eleven.find_voice(name)
            return found or await self.eleven.add_voice(name, sample, SAMPLE_FILENAME)
        except ElevenLabsError as exc:
            logger.warning("cloning failed: %s", exc)
            raise WorkFailed("VOICE_REJECTED" if exc.rejected else "PROVIDER_UNAVAILABLE") from exc

    async def _remove_provider_voice(self, provider_voice_id: str) -> None:
        try:
            await self.eleven.delete_voice(provider_voice_id)
        except ElevenLabsError:
            logger.warning("could not remove an orphaned voice", exc_info=True)

    # --- Avatars --------------------------------------------------------------------------

    async def make_avatar(self, avatar_id: uuid.UUID) -> str:
        """Paint the frames the version still lacks; ready once all 21 are."""
        if not await self._claim(AVATAR, avatar_id):
            return "not_queued"
        try:
            failures = await self._paint_missing(avatar_id)
        except WorkFailed as exc:
            await self._fail(AVATAR, avatar_id, exc.code)
            return "failed"
        except Exception:
            await self._fail(AVATAR, avatar_id, "INTERNAL_ERROR")
            raise
        if failures:
            code = next((c for c in FAILURE_ORDER if c in failures), failures[0])
            await self._fail(AVATAR, avatar_id, code)
            return "failed"
        if not await self._finish(AVATAR, avatar_id):
            return "superseded"  # deleted meanwhile; its frames go with its cleaning
        return "ready"

    async def clean_avatar(self, avatar_id: uuid.UUID) -> str:
        """Remove an unused avatar's frames and any photo left of it, then record it."""
        if await self._unused(AVATAR, avatar_id) is None:
            return "nothing_to_clean"
        await self.media.delete_prefix(avatar_prefix(avatar_id))
        await self.media.delete(avatar_photo_key(avatar_id))
        await self._cleaned(AVATAR, avatar_id)
        return "cleaned"

    async def _paint_missing(self, avatar_id: uuid.UUID) -> list[str]:
        """Paints every frame not yet ready; returns why frames failed, if any did."""
        if self.painter is None:
            raise WorkFailed("PROVIDER_UNAVAILABLE")
        async with self.sessionmaker() as session:
            rows = (
                await session.execute(
                    select(
                        AvatarFrame.frame_key, AvatarFrame.status, AvatarFrame.storage_key
                    ).where(AvatarFrame.avatar_id == avatar_id)
                )
            ).all()
        stored = {row.frame_key: row.storage_key for row in rows if row.status == "ready"}
        missing = [key for key in FRAME_KEYS if key not in stored]
        # What the missing frames are made from: frames made before, or the photo.
        sources: dict[str | None, bytes] = {}
        for parent in {parent_of(key) for key in missing} - set(missing):
            path = avatar_photo_key(avatar_id) if parent is None else stored[parent]
            content = await self.media.get(path)
            if content is None:
                raise WorkFailed("PHOTO_MISSING" if parent is None else "FRAME_MISSING")
            sources[parent] = content

        async def paint(frame_key: str, source: bytes) -> bytes:
            return await self._paint_frame(avatar_id, frame_key, source)

        async with self._beating(AVATAR, avatar_id):
            results = await paint_all(missing, sources, paint, self.concurrency)
        if BASE in stored or isinstance(results.get(BASE), bytes):
            # The base is made: the photo is not needed again, and goes (spec 21).
            await self.media.delete(avatar_photo_key(avatar_id))
        return [
            result.code if isinstance(result, FrameFailed) else "INTERNAL_ERROR"
            for result in results.values()
            if isinstance(result, BaseException) and not isinstance(result, Blocked)
        ]

    async def _paint_frame(self, avatar_id: uuid.UUID, frame_key: str, source: bytes) -> bytes:
        await self._set_frame(
            avatar_id, frame_key, status="running", attempts=AvatarFrame.attempts + 1
        )
        try:
            painted = await self.painter.paint(frame_key, source)
            sha256 = hashlib.sha256(painted.png).hexdigest()
            storage_key = frame_key_of(avatar_id, frame_key, sha256)
            await self.media.put(storage_key, painted.png, "image/png")
        except FrameFailed as exc:
            await self._set_frame(avatar_id, frame_key, status="failed", error_code=exc.code)
            raise
        except Exception:
            await self._set_frame(
                avatar_id, frame_key, status="failed", error_code="INTERNAL_ERROR"
            )
            raise
        await self._set_frame(
            avatar_id,
            frame_key,
            status="ready",
            storage_key=storage_key,
            sha256=sha256,
            input_tokens=painted.input_tokens,
            output_tokens=painted.output_tokens,
            error_code=None,
        )
        logger.info(
            "frame painted",
            extra={
                "avatar": str(avatar_id),
                "frame": frame_key,
                "seconds": painted.seconds,
                "input_tokens": painted.input_tokens,
                "output_tokens": painted.output_tokens,
                "cost_usd": round(painted.cost, 4),
            },
        )
        return painted.png

    async def _set_frame(self, avatar_id: uuid.UUID, frame_key: str, **values) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(AvatarFrame)
                .where(AvatarFrame.avatar_id == avatar_id, AvatarFrame.frame_key == frame_key)
                .values(**values, updated_at=func.now())
            )

    # --- Any version ----------------------------------------------------------------------

    async def _claim(self, kind: Kind, version_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            claimed = await session.scalar(
                update(kind.model)
                .where(kind.id_of() == version_id, kind.model.status == "queued")
                .values(
                    status="running",
                    execution=self.execution,
                    started_at=func.now(),
                    heartbeat_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(kind.id_of())
            )
        return claimed is not None

    async def _beat(self, kind: Kind, version_id: uuid.UUID) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                self._mine(kind, version_id).values(heartbeat_at=func.now(), updated_at=func.now())
            )

    @contextlib.asynccontextmanager
    async def _beating(self, kind: Kind, version_id: uuid.UUID) -> AsyncIterator[None]:
        """Beats in the background for work that takes minutes."""

        async def beat() -> None:
            while True:
                await asyncio.sleep(BEAT_SECONDS)
                try:
                    await self._beat(kind, version_id)
                except Exception:
                    logger.warning("heartbeat failed", exc_info=True)

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _finish(self, kind: Kind, version_id: uuid.UUID, **values) -> bool:
        async with self.sessionmaker() as session, session.begin():
            result = await session.execute(
                self._mine(kind, version_id).values(
                    status="ready",
                    error_code=None,
                    heartbeat_at=func.now(),
                    finished_at=func.now(),
                    updated_at=func.now(),
                    **values,
                )
            )
        return result.rowcount == 1

    async def _fail(self, kind: Kind, version_id: uuid.UUID, code: str) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                self._mine(kind, version_id).values(
                    status="failed", error_code=code, finished_at=func.now(), updated_at=func.now()
                )
            )

    def _mine(self, kind: Kind, version_id: uuid.UUID):
        """An update of the version only while this execution still runs it."""
        return update(kind.model).where(
            kind.id_of() == version_id,
            kind.model.status == "running",
            kind.model.execution == self.execution,
        )

    async def _unused(self, kind: Kind, version_id: uuid.UUID):
        """The version, if it is unused and not yet cleaned."""
        async with self.sessionmaker() as session:
            version = await session.get(kind.model, version_id)
        if version is None or version.status not in UNUSED or version.cleaned_at is not None:
            return None
        return version

    async def _cleaned(self, kind: Kind, version_id: uuid.UUID) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(kind.model)
                .where(
                    kind.id_of() == version_id,
                    kind.model.status.in_(UNUSED),
                    kind.model.cleaned_at.is_(None),
                )
                .values(cleaned_at=func.now(), updated_at=func.now())
            )


async def perform(worker: Worker, command: str, kind: str, version_id: uuid.UUID) -> str:
    """One unit of work, as the job's arguments name it: `make voice <id>`."""
    work = {
        ("make", "voice"): worker.make_voice,
        ("clean", "voice"): worker.clean_voice,
        ("make", "avatar"): worker.make_avatar,
        ("clean", "avatar"): worker.clean_avatar,
    }.get((command, kind))
    if work is None:
        raise ValueError(f"unknown work: {command} {kind}")
    return await work(version_id)
