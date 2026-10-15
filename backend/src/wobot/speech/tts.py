"""The reply spoken sentence by sentence, in order, as it is written (DEC-089, P14, P15).

Each sentence is one ElevenLabs request, at most two at a time: a sentence starts while
the one before is still being spoken, and a stop wastes little. They are told in order:
the second may be ready first, and waits only for the first to be told.
"""

import asyncio
import base64
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from wobot.assets.eleven import ElevenLabsError
from wobot.speech.emotions import TAGS, Feel

logger = logging.getLogger(__name__)

CONCURRENCY = 2
# Half the preview's bitrate: a voice needs no more, and audio goes as base64 in the
# stream, a third larger. About 8 KB a second of speech.
SPEECH_FORMAT = "mp3_44100_64"
NEUTRAL = "neutral"
# The models that take audio tags, such as [excited], to speak with a feeling (DEC-091).
TAGGED_MODELS = frozenset({"eleven_v4", "eleven_v4_turbo", "eleven_v3"})

# Emit(event, data): what the turn sends its client.
Emit = Callable[[str, dict[str, Any]], None]


class Voice(Protocol):
    async def speak(
        self, provider_voice_id: str, text: str, model: str, output_format: str
    ) -> bytes: ...


class Speaker:
    """Speaks the sentences given, telling each as a `speech` event in order, or a
    `speech_error` when it could not be spoken; then `speech_end`."""

    def __init__(
        self,
        voice: Voice,
        provider_voice_id: str,
        model: str,
        emit: Emit,
        feel: Feel | None = None,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self._voice = voice
        self._voice_id = provider_voice_id
        self._model = model
        self._emit = emit
        self._feel = feel
        self._limit = asyncio.Semaphore(concurrency)
        self._made: asyncio.Queue[tuple[int, asyncio.Task] | None] = asyncio.Queue()
        self._previous: str | None = None
        self._count = 0
        self._telling = asyncio.create_task(self._tell())
        self.spoken: list[dict[str, Any]] = []  # each sentence told: index, text, emotion
        self.seconds: list[float] = []  # how long each took to be spoken
        self.stopped = False

    def say(self, text: str) -> None:
        """Starts speaking a sentence; it is told after those before it."""
        if self.stopped:
            return
        index, previous = self._count, self._previous
        self._count += 1
        self._previous = text
        self._made.put_nowait((index, asyncio.create_task(self._speak(index, text, previous))))

    def close(self) -> None:
        """No more sentences come: `speech_end` follows the last."""
        self._made.put_nowait(None)

    async def stop(self) -> None:
        """Stops at once: no sentence is told or spoken after, and none is paid for that
        has not started."""
        self.stopped = True
        self._telling.cancel()
        pending = [self._telling]
        while not self._made.empty():
            if (made := self._made.get_nowait()) is not None:
                made[1].cancel()
                pending.append(made[1])
        await asyncio.gather(*pending, return_exceptions=True)

    async def _speak(self, index: int, text: str, previous: str | None) -> dict[str, Any]:
        async with self._limit:
            emotion = await self._feel(text, previous) if self._feel else NEUTRAL
            spoken = text
            if emotion in TAGS and self._model in TAGGED_MODELS:
                spoken = f"{TAGS[emotion]} {text}"
            started = time.perf_counter()
            audio = await self._voice.speak(self._voice_id, spoken, self._model, SPEECH_FORMAT)
            self.seconds.append(round(time.perf_counter() - started, 3))
        return {"index": index, "text": text, "emotion": emotion, "audio": audio}

    async def _tell(self) -> None:
        try:
            while (made := await self._made.get()) is not None:
                index, task = made
                try:
                    said = await task
                except Exception as exc:
                    # The text is unaffected: the app shows it whether or not it is heard.
                    if isinstance(exc, ElevenLabsError):
                        logger.warning("a sentence could not be spoken: %s", exc)
                    else:
                        logger.exception("a sentence could not be spoken")
                    self._emit("speech_error", {"index": index})
                    continue
                audio = base64.b64encode(said.pop("audio")).decode()
                self.spoken.append(said)
                self._emit("speech", {**said, "audio": audio})
        finally:
            if not self.stopped:
                self._emit("speech_end", {})
