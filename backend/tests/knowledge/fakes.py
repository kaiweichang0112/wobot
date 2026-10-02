"""Stand-ins for paid providers, so tests and CI never make a call that costs money."""

import hashlib
import math
import random
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from wobot.knowledge.embeddings import MAX_BATCH_SIZE, EmbeddingBatch
from wobot.knowledge.extraction import (
    LECTURE_FIELDS,
    PROMPT_VERSION,
    Answer,
    CachedReader,
    Question,
)
from wobot.knowledge.models import EMBEDDING_DIMENSIONS
from wobot.knowledge.vision import PROMPT_VERSION as VISION_PROMPT_VERSION
from wobot.knowledge.vision import VISUAL_CONTENT, VisualInput, visual_input


def fake_vector(text: str) -> list[float]:
    """A unit vector fixed by the text: equal texts give equal vectors, others unrelated ones."""
    rng = random.Random(hashlib.sha256(text.encode()).digest())
    values = [rng.gauss(0, 1) for _ in range(EMBEDDING_DIMENSIONS)]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class FakeEmbedder:
    # The seeded config, so fake vectors satisfy the foreign key to embedding_configs.
    config_id = f"openai/text-embedding-3-small/{EMBEDDING_DIMENSIONS}/cosine"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []  # the texts of each request, for assertions

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        if not 0 < len(texts) <= MAX_BATCH_SIZE:
            raise ValueError(f"expected 1 to {MAX_BATCH_SIZE} texts, got {len(texts)}")
        self.calls.append(list(texts))
        return EmbeddingBatch(
            vectors=[fake_vector(text) for text in texts],
            input_tokens=sum(len(text) for text in texts),
        )


_QUOTED = re.compile(r"^[\"“]?(?P<title>.+?)[,，]?[\"”]")


def quoted_title(entry: str) -> dict[str, Any]:
    """A stand-in model: the quoted title, no event, no location."""
    match = _QUOTED.match(entry)
    return {"title": match["title"] if match else None, "event": None, "location": None}


class FakeLectureReader:
    """Answers from a rule instead of a model, and counts what it was asked."""

    question = Question(LECTURE_FIELDS, "fake-model", PROMPT_VERSION)

    def __init__(self, answer: Callable[[str], Answer | dict[str, Any]] = quoted_title) -> None:
        self._answer = answer
        self.calls: list[str] = []

    async def read(self, entry: str) -> Answer:
        self.calls.append(entry)
        answer = self._answer(entry)
        if isinstance(answer, Answer):
            return answer
        return Answer(answer, None, "fake-model-2026-01-01", len(entry), 20)


class MemoryAnswerCache:
    def __init__(self) -> None:
        self.answers: dict[tuple[Question, str], Answer] = {}

    async def get(self, question: Question, input_hashes: Sequence[str]) -> dict[str, Answer]:
        return {h: self.answers[question, h] for h in input_hashes if (question, h) in self.answers}

    async def put(
        self, question: Question, input_hash: str, input: Mapping[str, Any], answer: Answer
    ) -> None:
        self.answers.setdefault((question, input_hash), answer)


def fake_lectures(reader: FakeLectureReader | None = None) -> CachedReader:
    return CachedReader(reader or FakeLectureReader(), MemoryAnswerCache())


def labelled_picture(item: VisualInput) -> dict[str, Any]:
    """A stand-in reading: the picture's bytes, or the page number, as its only text."""
    text = f"page {item.page}" if item.page is not None else item.content.decode(errors="replace")
    return {
        "contains_information": True,
        "verbatim_text": [text],
        "values": [],
        "relationships": [],
        "description": f"一張圖：{text}",
        "unreadable": [],
    }


class FakeVisionReader:
    question = Question(VISUAL_CONTENT, "fake-vision", VISION_PROMPT_VERSION)

    def __init__(
        self, answer: Callable[[VisualInput], Answer | dict[str, Any]] = labelled_picture
    ) -> None:
        self._answer = answer
        self.calls: list[VisualInput] = []

    async def read(self, item: VisualInput) -> Answer:
        self.calls.append(item)
        answer = self._answer(item)
        if isinstance(answer, Answer):
            return answer
        return Answer(answer, None, "fake-vision-2026-01-01", 800, 60)


def fake_vision(reader: FakeVisionReader | None = None) -> CachedReader[VisualInput]:
    return CachedReader(reader or FakeVisionReader(), MemoryAnswerCache(), cache_input=visual_input)
