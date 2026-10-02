"""Compare vision models on the dev transcriptions: each model reads the same pictures, and
each reading is scored against what a person transcribed from the picture.

Only dev cases are read here. The model is chosen on them, so the heldout cases still
measure the chosen model on pictures the choice never saw.

Answers are kept in knowledge.llm_extractions as ingestion keeps them, so the chosen
model's answers are not paid for again. Writing there takes the ingestion role:
DB_USER=wobot_ingest_user.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx2

from wobot.eval import gold
from wobot.eval.dataset import Dataset
from wobot.eval.metrics import TranscriptionScore, mean, transcription_score
from wobot.eval.runner import case_refs
from wobot.knowledge.extraction import AnswerCache, CachedReader, FieldReader
from wobot.knowledge.page_images import IMAGE_MAX_BYTES
from wobot.knowledge.profiles import DOCS_HOST, GTECH_DOCUMENTS
from wobot.knowledge.sources.documents import document_fetcher
from wobot.knowledge.sources.http import PageFetcher
from wobot.knowledge.sources.pdf import render_page
from wobot.knowledge.sources.wix import IMAGE_HOST
from wobot.knowledge.vision import VisualInput, visual_input


@dataclass(frozen=True)
class Fixture:
    case_id: str
    ref: gold.Ref  # the picture, with the person's transcription as its expected values
    picture: VisualInput


@dataclass
class ModelResult:
    model: str
    scores: dict[str, TranscriptionScore] = field(default_factory=dict)  # by case
    failures: dict[str, str] = field(default_factory=dict)  # by case: why there is no reading
    calls: int = 0  # paid for in this run; the rest were kept from earlier runs
    # Over every answer, kept or new: what reading these pictures costs with this model.
    input_tokens: int = 0
    output_tokens: int = 0

    def mean(self, name: str) -> float | None:
        return mean([getattr(score, name) for score in self.scores.values()])


def dev_transcriptions(
    datasets: Sequence[Dataset], gold_dir: Path = gold.GOLD_DIR
) -> list[tuple[str, gold.Ref]]:
    """Every transcribed dev case: its ID and the one picture it names."""
    labelled = []
    for dataset in datasets:
        for case in dataset.cases:
            if case.split != "dev" or not case.check or case.check["kind"] != "transcription":
                continue
            refs = case_refs(case, gold_dir, {})
            if refs:
                labelled.append((case.case_id, refs[0]))
    return labelled


async def fetch_pictures(
    labelled: Sequence[tuple[str, gold.Ref]],
    client: httpx2.AsyncClient,
    files: dict[str, Path],
) -> list[Fixture]:
    """Each picture as ingestion reads it: the image file, or the page drawn the same way."""
    hosts = {
        host: PageFetcher(client, {host}, max_bytes=IMAGE_MAX_BYTES)
        for host in (IMAGE_HOST, DOCS_HOST)
    }
    fetch_document = document_fetcher(client, files)
    documents = {document.key: document for document in GTECH_DOCUMENTS}
    fixtures = []
    for case_id, ref in labelled:
        if ref.kind == "image":
            url = gold.image_address(ref.value)
            fetcher = hosts.get(urlsplit(url).hostname or "")
            if fetcher is None:
                raise gold.GoldError(f"{ref.where}: images are read from {sorted(hosts)} only")
            content = (await fetcher.fetch(url)).content
            picture = VisualInput(hashlib.sha256(content).hexdigest(), content)
        else:
            if (document := documents.get(ref.value["document"])) is None:
                raise gold.GoldError(f"{ref.where}: no document {ref.value['document']!r}")
            snapshot = await fetch_document(document)
            page = ref.value["page"]
            picture = VisualInput(snapshot.sha256, render_page(snapshot.content, page), page=page)
        fixtures.append(Fixture(case_id, ref, picture))
    return fixtures


async def compare_models(
    readers: Sequence[FieldReader[VisualInput]], fixtures: Sequence[Fixture], cache: AnswerCache
) -> list[ModelResult]:
    results = []
    for reader in readers:
        cached = CachedReader(reader, cache, cache_input=visual_input)
        answers, stats = await cached.read_all([fixture.picture for fixture in fixtures])
        result = ModelResult(reader.question.model, calls=stats.calls)
        for fixture in fixtures:
            answer = answers[fixture.picture]
            result.input_tokens += answer.input_tokens
            result.output_tokens += answer.output_tokens
            if answer.failure is not None:
                result.failures[fixture.case_id] = answer.failure
            expected = fixture.ref.expected
            result.scores[fixture.case_id] = await transcription_score(
                expected["text"], expected["values"], answer.output
            )
        results.append(result)
    return results
