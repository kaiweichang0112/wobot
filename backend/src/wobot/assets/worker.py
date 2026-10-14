"""The asset worker: one execution makes or removes one version (DEC-086).

The version row is the job. A worker claims it by moving it from queued to running, which
only one execution can do however many are started for it, and ends it with a
compare-and-swap that names its execution: a version ended elsewhere meanwhile, deleted
or given up as lost, is never overwritten. The provider has no transaction, so a voice is
named after its version and a retry finds the one an earlier attempt made.

Imports nothing of the chat graph, so the job starts fast.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wobot.assets.eleven import ElevenLabs, ElevenLabsError, voice_name
from wobot.assets.media import MediaStore, voice_sample_key
from wobot.models import VoiceVersion

# Versions whose provider copy and files may go: nothing uses them, or ever will.
UNUSED = ("failed", "retired", "deleted")
SAMPLE_FILENAME = "sample.m4a"

logger = logging.getLogger(__name__)


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

    async def make_voice(self, voice_id: uuid.UUID) -> str:
        """Clone the version's voice. Returns the outcome, for the log."""
        if not await self._claim(voice_id):
            return "not_queued"  # another execution has it, or it has ended
        try:
            provider_voice_id = await self._clone(voice_id)
        except WorkFailed as exc:
            await self._fail(voice_id, exc.code)
            return "failed"
        except Exception:
            await self._fail(voice_id, "INTERNAL_ERROR")
            raise
        if not await self._finish(voice_id, provider_voice_id):
            # Ended elsewhere while this ran: what was made has no version to belong to.
            logger.warning("voice version ended while it was made", extra={"voice": voice_id})
            await self._remove_provider_voice(provider_voice_id)
            return "superseded"
        await self.media.delete(voice_sample_key(voice_id))  # cloned: the sample goes
        return "ready"

    async def clean_voice(self, voice_id: uuid.UUID) -> str:
        """Remove an unused version's voice at ElevenLabs and its sample, then record it."""
        async with self.sessionmaker() as session:
            version = await session.get(VoiceVersion, voice_id)
        if version is None or version.status not in UNUSED or version.cleaned_at is not None:
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
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(VoiceVersion)
                .where(
                    VoiceVersion.voice_id == voice_id,
                    VoiceVersion.status.in_(UNUSED),
                    VoiceVersion.cleaned_at.is_(None),
                )
                .values(cleaned_at=func.now(), updated_at=func.now())
            )
        return "cleaned"

    async def _claim(self, voice_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            claimed = await session.scalar(
                update(VoiceVersion)
                .where(VoiceVersion.voice_id == voice_id, VoiceVersion.status == "queued")
                .values(
                    status="running",
                    execution=self.execution,
                    started_at=func.now(),
                    heartbeat_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(VoiceVersion.voice_id)
            )
        return claimed is not None

    async def _clone(self, voice_id: uuid.UUID) -> str:
        if self.eleven is None:
            raise WorkFailed("PROVIDER_UNAVAILABLE")
        sample = await self.media.get(voice_sample_key(voice_id))
        if sample is None:
            raise WorkFailed("SAMPLE_MISSING")
        await self._beat(voice_id)
        name = voice_name(voice_id)
        try:
            # An earlier attempt may have cloned it and died before recording the ID.
            found = await self.eleven.find_voice(name)
            return found or await self.eleven.add_voice(name, sample, SAMPLE_FILENAME)
        except ElevenLabsError as exc:
            logger.warning("cloning failed: %s", exc)
            raise WorkFailed("VOICE_REJECTED" if exc.rejected else "PROVIDER_UNAVAILABLE") from exc

    async def _beat(self, voice_id: uuid.UUID) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                self._mine(voice_id).values(heartbeat_at=func.now(), updated_at=func.now())
            )

    async def _finish(self, voice_id: uuid.UUID, provider_voice_id: str) -> bool:
        async with self.sessionmaker() as session, session.begin():
            result = await session.execute(
                self._mine(voice_id).values(
                    status="ready",
                    provider_voice_id=provider_voice_id,
                    error_code=None,
                    heartbeat_at=func.now(),
                    finished_at=func.now(),
                    updated_at=func.now(),
                )
            )
        return result.rowcount == 1

    async def _fail(self, voice_id: uuid.UUID, code: str) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                self._mine(voice_id).values(
                    status="failed", error_code=code, finished_at=func.now(), updated_at=func.now()
                )
            )

    def _mine(self, voice_id: uuid.UUID):
        """An update of the version only while this execution still runs it."""
        return update(VoiceVersion).where(
            VoiceVersion.voice_id == voice_id,
            VoiceVersion.status == "running",
            VoiceVersion.execution == self.execution,
        )

    async def _remove_provider_voice(self, provider_voice_id: str) -> None:
        try:
            await self.eleven.delete_voice(provider_voice_id)
        except ElevenLabsError:
            logger.warning("could not remove an orphaned voice", exc_info=True)


async def perform(worker: Worker, command: str, kind: str, version_id: uuid.UUID) -> str:
    """One unit of work, as the job's arguments name it: `make voice <id>`."""
    if kind != "voice":
        raise ValueError(f"unknown asset kind: {kind}")
    if command == "make":
        return await worker.make_voice(version_id)
    if command == "clean":
        return await worker.clean_voice(version_id)
    raise ValueError(f"unknown command: {command}")
