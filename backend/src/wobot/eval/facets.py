"""Compare models for reading product facets (DEC-069): each model reads the same dev
products' catalog text, and the values code keeps of its answer are scored value by
value against what a person read in that text.

Each model is also judged by what its facets decide: every recommendation-v2 need with a
query is matched against the dev products as DEC-069 says, and a product a person said
may never be shown that passes is the worst miss, whatever the value counts say.

Only dev products are read here. The model and the prompt are chosen on them, so the
held-out products still measure the choice on products it never saw; `wobot-eval run`
scores those on what ingestion kept.

Answers are kept in knowledge.llm_extractions as ingestion keeps them, so a model and
prompt already run are not paid for again, and a model chosen here is not paid for again
at ingestion. Writing there takes the ingestion role: DB_USER=wobot_ingest_user.
"""

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.agent.recommend import recommended_before
from wobot.eval import gold
from wobot.eval.candidates import cost_per_thousand
from wobot.eval.corpus import Corpus
from wobot.eval.dataset import Dataset
from wobot.eval.metrics import Facets, FacetScore
from wobot.knowledge.extraction import AnswerCache, CachedReader, FieldReader
from wobot.knowledge.facet_extraction import ProductText, facet_input, grounded_facets
from wobot.knowledge.facets import FacetQuery, FacetValue, passes
from wobot.knowledge.models import IndexVersionRecord, ProductFacet, ProductRecord
from wobot.knowledge.records.text import key_text

# What a facet case's check names.
FACET_CHECK = "facets"


@dataclass(frozen=True)
class Product:
    """A labelled product, with its record's fields as the version holds them."""

    name: str
    expected: Facets  # what a person read: each facet's values with their spans
    fields: dict[str, Any]

    @property
    def text(self) -> ProductText:
        return ProductText.of(self.fields)


def labelled_products(
    datasets: Sequence[Dataset], corpus: Corpus, split: str, gold_dir: Path = gold.GOLD_DIR
) -> list[Product]:
    """The split's labelled products, each matched to its record in the corpus. A label
    that matches no record is an error: it would be scored as a product read wrong."""
    products = []
    for dataset in datasets:
        for case in dataset.cases:
            if case.split != split or not case.check or case.check["kind"] != FACET_CHECK:
                continue
            path = gold_dir / case.check["gold"]["file"]
            refs = gold.product_facet_refs(path, split)
            for resolved in gold.resolve(refs, corpus):
                if len(resolved.keys) != 1:
                    raise gold.GoldError(f"{resolved.ref.where}: matches no single product")
                item = next(i for i in corpus.of("product") if i.logical_key == resolved.keys[0])
                expected = resolved.ref.expected["facets"]
                products.append(Product(item.fields["product_name"], expected, item.fields))
    return products


def as_facets(values: Sequence[FacetValue]) -> dict[str, dict[str, str]]:
    facets: dict[str, dict[str, str]] = {}
    for value in values:
        facets.setdefault(value.facet, {})[value.value] = value.span
    return facets


@dataclass(frozen=True)
class NeedCheck:
    """A recommendation case's need, with the products a person said fit it and those that
    may never be shown, by name as compared."""

    case_id: str
    query: FacetQuery
    fits: frozenset[str]
    never: frozenset[str]
    shown: frozenset[str]  # recommended earlier in the conversation: left out, as at runtime


def need_checks(datasets: Sequence[Dataset]) -> list[NeedCheck]:
    checks = []
    for dataset in datasets:
        for case in dataset.cases:
            check = case.check or {}
            if check.get("kind") != "recommendation" or "query" not in check:
                continue
            answers = (turn["answer"] for turn in case.history if turn.get("answer"))
            checks.append(
                NeedCheck(
                    case.case_id,
                    FacetQuery.of(check["query"]),
                    frozenset(map(key_text, check.get("recommends") or ())),
                    frozenset(map(key_text, check.get("never") or ())),
                    frozenset(map(key_text, recommended_before(answers))),
                )
            )
    return checks


@dataclass
class Decisions:
    never_passing: list[str] = field(default_factory=list)  # "CASE-022: <product>"
    fits_filtered: list[str] = field(default_factory=list)


def decide(
    checks: Sequence[NeedCheck], facets: Mapping[str, Mapping[str, Collection[str]]]
) -> Decisions:
    """Each need against the products whose facets are given, by product name."""
    decisions = Decisions()
    for check in checks:
        for name, product_facets in facets.items():
            key = key_text(name)
            if key in check.shown:
                continue
            fits = passes(product_facets, check.query)
            if fits and key in check.never:
                decisions.never_passing.append(f"{check.case_id}: {name}")
            if not fits and key in check.fits:
                decisions.fits_filtered.append(f"{check.case_id}: {name}")
    return decisions


@dataclass
class ModelResult:
    model: str
    score: FacetScore = field(default_factory=FacetScore)
    decisions: Decisions = field(default_factory=Decisions)
    failures: dict[str, str] = field(default_factory=dict)  # by product: why no answer
    dropped: list[str] = field(default_factory=list)  # values whose span is not the text's
    calls: int = 0  # paid for in this run; the rest were kept from earlier runs
    # Over every answer, kept or new: what reading these products costs with this model.
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_per_thousand(self) -> float | None:
        """Dollars for reading a thousand products of these mean sizes."""
        products = self.score.products or 1
        return cost_per_thousand(
            self.model, self.input_tokens / products, self.output_tokens / products
        )


async def compare_models(
    readers: Sequence[FieldReader[ProductText]],
    products: Sequence[Product],
    cache: AnswerCache,
    checks: Sequence[NeedCheck] = (),
) -> list[ModelResult]:
    results = []
    for reader in readers:
        cached = CachedReader(reader, cache, cache_input=facet_input)
        answers, stats = await cached.read_all([product.text for product in products])
        result = ModelResult(reader.question.model, calls=stats.calls)
        kept_by_product = {}
        for product in products:
            answer = answers[product.text]
            result.input_tokens += answer.input_tokens
            result.output_tokens += answer.output_tokens
            kept: list[FacetValue] = []
            if answer.output is None:
                result.failures[product.name] = answer.failure or "no output"
            else:
                kept, dropped = grounded_facets(answer.output, product.fields)
                result.dropped += [f"{product.name}: {note}" for note in dropped]
            # A product without an answer counts every labelled value as missed.
            kept_by_product[product.name] = as_facets(kept)
            result.score.add(product.name, product.expected, kept_by_product[product.name])
        result.decisions = decide(checks, kept_by_product)
        results.append(result)
    return results


async def stored_facets(
    conn: AsyncConnection, version_id: int, logical_keys: Sequence[str]
) -> dict[str, dict[str, dict[str, str]] | None]:
    """What a version keeps of each product's facets, by logical key; None for a product
    no model read, such as one ingested before facets."""
    rows = await conn.execute(
        select(
            IndexVersionRecord.logical_key,
            IndexVersionRecord.record_id,
            ProductRecord.facets_model,
        )
        .join(ProductRecord, ProductRecord.record_id == IndexVersionRecord.record_id)
        .where(
            IndexVersionRecord.index_version_id == version_id,
            IndexVersionRecord.logical_key.in_(list(logical_keys)),
        )
    )
    read: dict[uuid.UUID, str] = {}
    stored: dict[str, dict[str, dict[str, str]] | None] = {}
    for row in rows:
        stored[row.logical_key] = None if row.facets_model is None else {}
        if row.facets_model is not None:
            read[row.record_id] = row.logical_key
    values = await conn.execute(
        select(
            ProductFacet.record_id, ProductFacet.facet, ProductFacet.value, ProductFacet.span
        ).where(ProductFacet.record_id.in_(list(read)))
    )
    for value in values:
        facets = stored[read[value.record_id]]
        assert facets is not None
        facets.setdefault(value.facet, {})[value.value] = value.span
    return stored
