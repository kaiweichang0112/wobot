"""Finding the catalog's products that fit a need, from their facets (DEC-069).

The agent turns what the user said into a query in the facet vocabulary; code does the
rest. A product fits when its catalog states every function and contradicts no
condition, judged by `knowledge.facets.judge`, the rules `wobot-eval facets` measures.
Those that fit are ranked: 世大智科's first, then those whose catalog states more of the
conditions, then the closest in meaning. When more fit than the user asked for, code
names the facets that split them, for the agent to ask about; when none fits, how many
each need leaves.

A version never changes, so its products and facets are read once and kept.
"""

import logging
import uuid
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from sqlalchemy import select

from wobot.agent.retrieval import UNAVAILABLE, LookupFailed, record_handle
from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.facets import (
    FACET_VALUES,
    FUNCTION_GROUPS,
    FUNCTION_NAMES,
    FacetQuery,
    Judgement,
    condition_name,
    conditions_of,
    has_function,
    judge,
)
from wobot.knowledge.models import IndexVersionRecord, ProductFacet, ProductRecord
from wobot.knowledge.records.text import key_text
from wobot.knowledge.repository import Database
from wobot.knowledge.search import chunk_members, search_chunks

logger = logging.getLogger(__name__)

# Products of this company come first among those that fit (DEC-065).
PREFERRED_COMPANY = "世大智科"
# Needs one query may name, as a recommendation may.
MOST_NEEDS = 8
# The facets worth asking the user about when they split the fitting products: sensing is
# what users raise themselves, and users concerns activities alone.
DECISIVE_FACETS = ("places", "form", "viewing", "power")
MOST_DECISIVE = 2
# Versions whose products are kept: the active one and the one before it.
KEPT_VERSIONS = 2
# What a card shows of a product.
CARD_FIELDS = ("product_name", "company_name", "contact_phone", "product_url")


# --- The version's products ---------------------------------------------------------------


@dataclass(frozen=True)
class CatalogProduct:
    handle: str  # r-…, as the evidence names records
    key: str  # the logical key
    fields: dict[str, str]  # the card fields the record has
    facets: dict[str, dict[str, str]]  # facet → value → span

    @property
    def name(self) -> str:
        return self.fields.get("product_name", "")

    @property
    def preferred(self) -> bool:
        return PREFERRED_COMPANY in self.fields.get("company_name", "")


async def product_catalog(db: Database, version_id: int) -> list[CatalogProduct]:
    """The version's products with their facets, in one query; a product no model read
    has none. Raises LookupFailed when the database cannot be reached."""
    products: dict[uuid.UUID, CatalogProduct] = {}
    try:
        async with db.begin() as conn:
            rows = await conn.execute(
                select(
                    IndexVersionRecord.record_id,
                    IndexVersionRecord.logical_key,
                    *(getattr(ProductRecord, name) for name in CARD_FIELDS),
                    ProductFacet.facet,
                    ProductFacet.value,
                    ProductFacet.span,
                )
                .join(ProductRecord, ProductRecord.record_id == IndexVersionRecord.record_id)
                .outerjoin(ProductFacet, ProductFacet.record_id == IndexVersionRecord.record_id)
                .where(IndexVersionRecord.index_version_id == version_id)
                .order_by(IndexVersionRecord.logical_key)
            )
            for row in rows:
                product = products.get(row.record_id)
                if product is None:
                    fields = {name: v for name in CARD_FIELDS if (v := getattr(row, name))}
                    product = CatalogProduct(
                        record_handle(row.record_id), row.logical_key, fields, {}
                    )
                    products[row.record_id] = product
                if row.facet is not None:
                    product.facets.setdefault(row.facet, {})[row.value] = row.span
    except UNAVAILABLE as error:
        logger.exception("product catalog failed", extra={"index_version": version_id})
        raise LookupFailed from error
    return list(products.values())


class ProductCatalogs:
    """Each version's products, read once: a published version never changes."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._kept: dict[int, list[CatalogProduct]] = {}

    async def get(self, version_id: int) -> list[CatalogProduct]:
        if (kept := self._kept.get(version_id)) is None:
            kept = self._kept[version_id] = await product_catalog(self._db, version_id)
            while len(self._kept) > KEPT_VERSIONS:
                del self._kept[next(iter(self._kept))]  # the oldest read
        return kept


# --- The tool's arguments -----------------------------------------------------------------

FunctionName = Literal[FUNCTION_NAMES + tuple(FUNCTION_GROUPS)]  # type: ignore[valid-type]

# Per facet, the values a condition names; each facet [] unless the user named it.
Conditions = create_model(
    "Conditions",
    __config__=ConfigDict(extra="forbid"),
    **{
        facet: (list[Literal[values]], Field(default_factory=list))  # type: ignore[valid-type]
        for facet, values in FACET_VALUES.items()
    },
)


class FindProducts(BaseModel):
    """The catalog's products that do every function and break no condition, best first,
    each with what its catalog states of the needs."""

    model_config = ConfigDict(title="find_products")

    functions: list[FunctionName] = Field(
        default_factory=list,
        max_length=MOST_NEEDS,
        description="what the product must do, from the vocabulary; 復健訓練 takes any kind",
    )
    must: Conditions = Field(  # type: ignore[valid-type]
        default_factory=Conditions, description="values it must have, such as places home"
    )
    must_not: Conditions = Field(  # type: ignore[valid-type]
        default_factory=Conditions, description="values it must not have, such as form worn"
    )
    other: list[str] = Field(
        default_factory=list,
        max_length=MOST_NEEDS,
        description="needs the vocabulary has no word for, in the user's words",
    )
    count: Annotated[int, Field(ge=1)] | Literal["all"] = Field(
        default=1, description="how many products the user asked for"
    )

    @model_validator(mode="after")
    def _names_a_need_once(self) -> "FindProducts":
        if not self.functions and not self.text:
            raise ValueError("name a function, or the need in other")
        both = [
            f"{facet} {value}"
            for facet in FACET_VALUES
            for value in set(getattr(self.must, facet)) & set(getattr(self.must_not, facet))
        ]
        if both:
            raise ValueError(f"both required and excluded: {', '.join(sorted(both))}")
        return self

    @property
    def query(self) -> FacetQuery:
        def part(conditions: BaseModel) -> dict[str, tuple[str, ...]]:
            return {f: tuple(dict.fromkeys(v)) for f, v in conditions.model_dump().items() if v}

        return FacetQuery(
            tuple(dict.fromkeys(self.functions)), part(self.must), part(self.must_not)
        )

    @property
    def text(self) -> str:
        """What the products are ranked by in meaning: the functions and the other needs,
        never the conditions, which a search cannot tell from their opposite (DEC-066)."""
        needs = [*self.functions, *(need.strip() for need in self.other)]
        return "、".join(dict.fromkeys(need for need in needs if need))


# --- Finding --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ranked:
    product: CatalogProduct
    judgement: Judgement
    similarity: float

    def order(self) -> tuple[bool, int, float, str]:
        """世大智科's first, then the most conditions stated, then the closest in meaning."""
        return (
            not self.product.preferred,
            -self.judgement.supported,
            -self.similarity,
            self.product.name,
        )


@dataclass(frozen=True)
class Split:
    """A facet the user has not settled that splits the fitting products."""

    facet: str
    values: dict[str, int]  # each value: the fitting products that state it
    unstated: int  # fitting products that state nothing of the facet


@dataclass(frozen=True)
class Gap:
    """One need when nothing fits: for a function, the products that state it; for a
    condition, the products that would fit without it."""

    need: str
    products: int


@dataclass(frozen=True)
class Found:
    fitting: list[Ranked]  # best first
    decisive: list[Split]  # only when more fit than the user asked for
    gaps: list[Gap]  # only when none fits
    recommended_before: int  # fitting products left out: shown earlier in the conversation


def find(
    catalog: Sequence[CatalogProduct],
    query: FacetQuery,
    count: int | Literal["all"],
    similarity: Mapping[str, float],
    shown: Collection[str] = (),
) -> Found:
    """The products that fit the query, ranked. No database: pytest runs it on the gold
    facets. `similarity` is by handle; `shown` names products recommended earlier."""
    shown_keys = {key_text(name) for name in shown}
    fresh: list[CatalogProduct] = []
    before = 0
    for product in catalog:
        if key_text(product.name) in shown_keys:
            before += judge(product.facets, query).passes
        else:
            fresh.append(product)
    fitting = sorted(
        (
            Ranked(product, judgement, similarity.get(product.handle, 0.0))
            for product in fresh
            if (judgement := judge(product.facets, query)).passes
        ),
        key=Ranked.order,
    )
    many = count != "all" and len(fitting) > count
    return Found(
        fitting,
        decisive(fitting, query) if many else [],
        [] if fitting else gaps(fresh, query),
        before,
    )


def decisive(fitting: Sequence[Ranked], query: FacetQuery) -> list[Split]:
    """The facets the user has not settled where some fitting products state a value that
    others, stating the facet, do not: an answer would leave some out. Evenest first."""
    asked = set(query.must) | set(query.must_not)
    ranked: list[tuple[int, Split]] = []
    for facet in DECISIVE_FACETS:
        if facet in asked:
            continue
        stating = [set(r.product.facets[facet]) for r in fitting if r.product.facets.get(facet)]
        counts = Counter(value for values in stating for value in values)
        evenness = max((min(n, len(stating) - n) for n in counts.values()), default=0)
        if evenness:
            split = Split(facet, dict(counts.most_common()), len(fitting) - len(stating))
            ranked.append((evenness, split))
    ranked.sort(key=lambda pair: -pair[0])
    return [split for _, split in ranked[:MOST_DECISIVE]]


def gaps(catalog: Sequence[CatalogProduct], query: FacetQuery) -> list[Gap]:
    """Why nothing fits: how many products state each function, and how many would fit
    without each condition."""
    found = [
        Gap(name, sum(has_function(p.facets.get("functions", {}), name) for p in catalog))
        for name in query.functions
    ]
    for facet, value, must in conditions_of(query):
        relaxed = without(query, facet, value, must=must)
        fit = sum(judge(p.facets, relaxed).passes for p in catalog)
        found.append(Gap(condition_name(facet, value, must=must), fit))
    return found


def without(query: FacetQuery, facet: str, value: str, *, must: bool) -> FacetQuery:
    """The query with one condition dropped."""

    def drop(part: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        kept = {f: tuple(v for v in vs if (f, v) != (facet, value)) for f, vs in part.items()}
        return {f: vs for f, vs in kept.items() if vs}

    if must:
        return FacetQuery(query.functions, drop(query.must), query.must_not)
    return FacetQuery(query.functions, query.must, drop(query.must_not))


def stated(product: CatalogProduct, query: FacetQuery) -> dict[str, str]:
    """What the agent reads of each need: the catalog's own words where it states it,
    unknown where it states nothing of the facet, contradicted where it states otherwise."""
    functions = product.facets.get("functions", {})
    words = {}
    for name in query.functions:
        member = next((f for f in FUNCTION_GROUPS.get(name, (name,)) if f in functions), None)
        words[name] = "not stated" if member is None else functions[member]
    for facet, value, must in conditions_of(query):
        values = product.facets.get(facet, {})
        if not values:
            text = "unknown"
        elif (value in values) != must:
            text = "contradicted"
        else:  # a value it must not have: what it is instead
            text = values[value] if must else next(iter(values.values()))
        words[condition_name(facet, value, must=must)] = text
    return words


# --- Ranking by meaning ---------------------------------------------------------------------


async def similarities(
    db: Database, embedder: Embedder, version_id: int, text: str, products: int
) -> dict[str, float]:
    """Each product's closeness in meaning to the text, by handle: 1 minus the cosine
    distance of its catalog passage. Raises LookupFailed when the database or the
    embedding provider cannot be reached."""
    try:
        [vector] = (await embedder.embed([text])).vectors
        async with db.begin() as conn:
            hits = await search_chunks(
                conn,
                vector,
                embedder.config_id,
                products,
                version_id=version_id,
                record_type="product",
            )
            members = await chunk_members(conn, [hit.chunk_id for hit in hits], version_id)
    except UNAVAILABLE as error:
        logger.exception("product similarity failed", extra={"index_version": version_id})
        raise LookupFailed from error
    return {
        record_handle(member.record_id): 1 - hit.distance
        for hit in hits
        for member in members[hit.chunk_id]
        if member.record_type == "product"
    }


async def find_products(
    catalogs: ProductCatalogs,
    db: Database,
    embedder: Embedder,
    version_id: int,
    args: FindProducts,
    shown: Collection[str] = (),
) -> Found:
    catalog = await catalogs.get(version_id)
    similarity = await similarities(db, embedder, version_id, args.text, len(catalog))
    return find(catalog, args.query, args.count, similarity, shown)
