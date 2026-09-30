"""Stand-ins for paid providers, so tests and CI never make a call that costs money."""

import hashlib
import math
import random
from collections.abc import Sequence

from wobot.knowledge.embeddings import MAX_BATCH_SIZE, EmbeddingBatch
from wobot.knowledge.models import EMBEDDING_DIMENSIONS


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
