import json

import httpx2
import pytest
from openai import AsyncOpenAI

from tests.knowledge.fakes import FakeEmbedder
from wobot.knowledge.embeddings import EmbeddingError, OpenAIEmbedder
from wobot.knowledge.models import EMBEDDING_DIMENSIONS

MODEL = "text-embedding-3-small"


def embedder_answering(vectors_by_index, requests=None) -> OpenAIEmbedder:
    """An embedder whose client talks to a stub that answers with the given vectors."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(json.loads(request.content))
        data = [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in vectors_by_index
        ]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": MODEL,
                "data": data,
                "usage": {"prompt_tokens": 12, "total_tokens": 12},
            },
        )

    client = AsyncOpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return OpenAIEmbedder(client, MODEL)


def vector(value: float, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    return [value] * dimensions


async def test_asks_for_the_configured_space_and_keeps_input_order():
    requests = []
    embedder = embedder_answering([(1, vector(0.2)), (0, vector(0.1))], requests)

    batch = await embedder.embed(["第一段", "第二段"])

    assert requests[0]["model"] == MODEL
    assert requests[0]["dimensions"] == EMBEDDING_DIMENSIONS
    assert requests[0]["input"] == ["第一段", "第二段"]
    assert [v[0] for v in batch.vectors] == pytest.approx([0.1, 0.2])
    assert batch.input_tokens == 12
    assert embedder.config_id == "openai/text-embedding-3-small/1536/cosine"


async def test_rejects_a_missing_vector():
    embedder = embedder_answering([(0, vector(0.1))])

    with pytest.raises(EmbeddingError, match="expected 2 vectors"):
        await embedder.embed(["第一段", "第二段"])


async def test_rejects_a_vector_of_another_size():
    embedder = embedder_answering([(0, vector(0.1, dimensions=3))])

    with pytest.raises(EmbeddingError, match="dimensions"):
        await embedder.embed(["第一段"])


async def test_rejects_an_empty_or_oversized_batch_before_calling():
    requests = []
    embedder = embedder_answering([], requests)

    for texts in ([], ["段落"] * 101):
        with pytest.raises(ValueError, match="texts"):
            await embedder.embed(texts)
    assert requests == []


async def test_fake_vectors_are_fixed_by_the_text():
    embedder = FakeEmbedder()

    first = await embedder.embed(["離床預警", "跌倒偵測"])
    again = await embedder.embed(["離床預警"])

    assert first.vectors[0] == again.vectors[0]
    assert first.vectors[0] != first.vectors[1]
    assert sum(v * v for v in first.vectors[0]) == pytest.approx(1)
    assert embedder.calls == [["離床預警", "跌倒偵測"], ["離床預警"]]
