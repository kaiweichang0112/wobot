"""Text embeddings behind one small interface, so tests and CI never call a provider."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from openai import AsyncOpenAI

from wobot.knowledge.models import EMBEDDING_DIMENSIONS

# Texts per request. The API allows far more; a small batch keeps a failed request cheap
# to retry, and lets the pipeline store each batch as it arrives.
MAX_BATCH_SIZE = 100


class EmbeddingError(Exception):
    """The provider answered, but not with one vector of the expected size per text."""


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]  # one per text, in the order of the texts
    input_tokens: int  # billed tokens, for the run's counts


class Embedder(Protocol):
    # The knowledge.embedding_configs row the vectors belong to. Vectors from different
    # configs live in different spaces and are never compared.
    config_id: str

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed 1 to MAX_BATCH_SIZE texts in one request."""
        ...


class OpenAIEmbedder:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model
        self.config_id = f"openai/{model}/{EMBEDDING_DIMENSIONS}/cosine"

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        if not 0 < len(texts) <= MAX_BATCH_SIZE:
            raise ValueError(f"expected 1 to {MAX_BATCH_SIZE} texts, got {len(texts)}")
        # Retries on 429 and 5xx come from the client (max_retries), with backoff.
        response = await self._client.embeddings.create(
            model=self._model,
            input=list(texts),
            # Explicit, so a changed default can never put vectors of another size here.
            dimensions=EMBEDDING_DIMENSIONS,
        )
        items = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(len(texts))):
            raise EmbeddingError(f"expected {len(texts)} vectors, got {len(items)}")
        vectors = [item.embedding for item in items]
        if wrong := [i for i, vector in enumerate(vectors) if len(vector) != EMBEDDING_DIMENSIONS]:
            raise EmbeddingError(f"vectors {wrong} do not have {EMBEDDING_DIMENSIONS} dimensions")
        return EmbeddingBatch(vectors=vectors, input_tokens=response.usage.prompt_tokens)
