"""The agent's read-only tools: what the model may ask for, and what it is shown back.

Each tool answers twice: a compact JSON content for the model, and an artifact with every
item and full ID for code, which renders lists, checks citations and scores evaluations.
"""

import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

import openai
from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool, ToolException
from sqlalchemy.exc import DBAPIError

from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.lists import (
    Listed,
    ListKind,
    QueryError,
    RecordQuery,
    find_records,
    records_by_prefix,
)
from wobot.knowledge.repository import Database
from wobot.knowledge.search import Member, SearchHit, chunk_members, fuse, search_chunks

logger = logging.getLogger(__name__)

# The most items one result shows the model; code renders the whole list from the artifact.
SHOWN_ITEMS = 50
# Chunks one search returns: the k the phase A baseline was measured at.
SEARCHED_CHUNKS = 5
# Wordings one search may fuse: the user's language and English, and one more at most.
SEARCH_QUERIES = 3
# How deep each wording's ranking goes before fusion. Deeper lets a chunk every wording
# ranks in the middle win, but also lets wordings that agree on the wrong chunks bury a
# passage only one language finds; on two samples of the agent's queries (B4), 10 lost
# 0.10 recall@5 on average to 5.
FUSION_DEPTH = SEARCHED_CHUNKS
# Products one details call may name: enough to compare, small enough to read in full.
DETAILED_PRODUCTS = 5
# The database or the provider failed, not the code: worth trying again later. Anything
# else is a bug and is raised, never dressed up as an outage.
UNAVAILABLE = (DBAPIError, openai.APIError, TimeoutError)

# The catalog's second-level categories, as the model reads them in the tool's schema.
PRODUCT_CATEGORIES = {
    "1-1": "人員狀態判讀、跌倒偵測、安全防護",
    "1-2": "緊急求助、室內定位",
    "1-3": "臥床監測、離床預警、壓傷防護",
    "1-4": "智慧照顧環境輔助",
    "1-5": "環境品質監測與維護",
    "2-1": "生理資訊量測設備",
    "2-2": "穿戴式生理資訊量測裝置",
    "2-3": "生理資訊量測站",
    "2-4": "遠距健康管理系統",
    "3-1": "長者照顧服務",
    "3-2": "機構管理系統",
    "4-1": "肢體運動/健身",
    "4-2": "復能/復健",
    "4-3": "認知訓練/運動遊戲",
    "4-4": "體適能檢測",
    "5-1": "工作協助機器人",
    "5-2": "溝通機器人",
}
# Codes and labels apart: shown "4-3 認知訓練/運動遊戲", models passed the label too.
CATEGORY_HELP = (
    "lecture: keynote or invited. publication: Journal papers, Conference papers, "
    "Books / book chapters, Patents or General publications. product: the code alone, "
    'such as "4-3", one of '
    + "; ".join(f'"{code}" = {label}' for code, label in PRODUCT_CATEGORIES.items())
)
# What the model is shown of each item; the artifact keeps every column.
SHOWN_FIELDS: dict[str, tuple[str, ...]] = {
    "lecture": ("date_raw", "category", "title", "event", "location"),
    "student": ("name", "degree", "graduation_year", "thesis_title_zh", "thesis_title_en"),
    "project": ("year", "title_zh", "title_en", "funder_raw", "period_raw", "amount_ntd"),
    "publication": ("year", "category", "item_text"),
    "product": ("product_name", "company_name", "category_l2_label"),
}
# Every catalog field a details call shows; codes and parsed years stay in the artifact.
DETAIL_FIELDS = (
    "product_name",
    "company_name",
    "company_address",
    "contact_phone",
    "product_url",
    "category_l1_label",
    "category_l2_label",
    "features_text",
    "usage_text",
    "summary_text",
    "adoption_years_raw",
)
RECORD_HANDLE = re.compile(r"r-([0-9a-f]{8})")


@dataclass(frozen=True)
class TurnContext:
    """Bound once per turn, before the model runs; the model can neither see nor change it."""

    account_id: str
    query_time: datetime
    # Every tool call of a turn reads this version, even if another is published meanwhile.
    index_version: int
    chatbot_name: str = "Wobot"  # what the account calls its assistant


class ToolStatus(StrEnum):
    FOUND = "found"
    NO_MATCH = "no_match"  # the version holds nothing that matches
    FAILED = "failed"  # the database or the provider failed; the user may try again


@dataclass(frozen=True)
class RecordsResult:
    status: ToolStatus
    result_id: str  # the same query of the same version always gets the same ID
    query: RecordQuery
    items: list[Listed] = field(default_factory=list)


@dataclass(frozen=True)
class Evidence:
    hit: SearchHit
    members: list[Member]  # the records the chunk is built from


@dataclass(frozen=True)
class SearchResult:
    status: ToolStatus
    queries: list[str]
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(frozen=True)
class DetailsResult:
    status: ToolStatus
    products: list[Listed] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # IDs the version holds no product for


# What the model reads when a tool could not reach the database or the provider.
FAILED_CONTENT = json.dumps({"status": ToolStatus.FAILED, "retryable": True})


def record_handle(record_id: uuid.UUID) -> str:
    """A short ID the model can copy without mistakes; code finds the record by its prefix."""
    return f"r-{record_id.hex[:8]}"


def chunk_handle(chunk_id: uuid.UUID) -> str:
    return f"k-{chunk_id.hex[:8]}"


def result_handle(version_id: int, query: RecordQuery) -> str:
    key = json.dumps([version_id, asdict(query)], sort_keys=True)
    return f"q-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def records_content(result: RecordsResult) -> str:
    """The result as the model reads it: at most SHOWN_ITEMS items, and the full count."""
    if result.status is ToolStatus.FAILED:
        return FAILED_CONTENT
    shown = result.items[:SHOWN_ITEMS]
    names = SHOWN_FIELDS[result.query.kind]
    items = [
        {"id": record_handle(item.record_id)}
        | {name: item.fields[name] for name in names if item.fields[name] is not None}
        for item in shown
    ]
    return json.dumps(
        {
            "status": result.status,
            "result_id": result.result_id,
            "count": len(result.items),
            "shown": len(shown),
            "items": items,
        },
        ensure_ascii=False,
        default=str,
    )


def search_content(result: SearchResult) -> str:
    """The passages as the model reads them, with the products each one is about."""
    if result.status is ToolStatus.FAILED:
        return FAILED_CONTENT
    items = []
    for evidence in result.evidence:
        item = {
            "id": chunk_handle(evidence.hit.chunk_id),
            "header": evidence.hit.context_header,
            "text": evidence.hit.body,
        }
        # Product IDs let the model ask get_product_details about what a passage names.
        if products := [
            record_handle(m.record_id) for m in evidence.members if m.record_type == "product"
        ]:
            item["products"] = products
        items.append(item)
    return json.dumps({"status": result.status, "items": items}, ensure_ascii=False)


def details_content(result: DetailsResult) -> str:
    if result.status is ToolStatus.FAILED:
        return FAILED_CONTENT
    items = [
        {"id": record_handle(product.record_id)}
        | {name: product.fields[name] for name in DETAIL_FIELDS if product.fields[name]}
        for product in result.products
    ]
    return json.dumps(
        {"status": result.status, "items": items, "missing": result.missing},
        ensure_ascii=False,
    )


def build_tools(db: Database, embedder: Embedder) -> list[BaseTool]:
    """The tools of one process, bound to its database and embedding model."""

    @tool(response_format="content_and_artifact")
    async def search_knowledge(
        queries: Annotated[
            list[str],
            "1 to 3 wordings of what to look for: the user's own words, and the same in "
            "English, since many sources are written in English",
        ],
        runtime: ToolRuntime[TurnContext],
    ) -> tuple[str, SearchResult]:
        """Search the GRC and G-Tech websites, G-Tech's WhizToys documentation and PDFs, and
        the catalog's smart-care products for the passages closest in meaning to the queries,
        merging what each wording finds. Use it for facts, explanations, or products that fit
        a need. It returns the closest few passages, not every match: for complete lists use
        query_records."""
        if not 0 < len(queries) <= SEARCH_QUERIES:
            raise ToolException(f"give 1 to {SEARCH_QUERIES} queries")
        version_id = runtime.context.index_version
        try:
            embedded = await embedder.embed(queries)
            async with db.begin() as conn:
                rankings = [
                    await search_chunks(
                        conn, vector, embedder.config_id, FUSION_DEPTH, version_id=version_id
                    )
                    for vector in embedded.vectors
                ]
                hits = fuse(rankings, SEARCHED_CHUNKS)
                members = await chunk_members(conn, [hit.chunk_id for hit in hits], version_id)
        except UNAVAILABLE:
            logger.exception("search_knowledge failed", extra={"index_version": version_id})
            result = SearchResult(ToolStatus.FAILED, queries)
        else:
            # Nearest neighbours always exist: no_match means the version holds no passages.
            status = ToolStatus.FOUND if hits else ToolStatus.NO_MATCH
            evidence = [Evidence(hit, members[hit.chunk_id]) for hit in hits]
            result = SearchResult(status, queries, evidence)
        return search_content(result), result

    @tool(response_format="content_and_artifact")
    async def query_records(
        record_type: ListKind,
        runtime: ToolRuntime[TurnContext],
        year_from: Annotated[int | None, "the first year to include"] = None,
        year_to: Annotated[int | None, "the last year to include"] = None,
        degree: Annotated[Literal["master", "phd"] | None, "students only"] = None,
        category: Annotated[str | None, CATEGORY_HELP] = None,
    ) -> tuple[str, RecordsResult]:
        """List every record that matches: Yeh-Liang Hsu's lectures, GRC's graduated students
        with their theses, research projects or publications, or the catalog's smart-care
        products. The list is complete, never a sample: use it for "all", "list", "how many",
        or anything asked by year, degree or category. Students and projects have years,
        products do not. At most 50 items are shown; the whole list is kept for display."""
        query = RecordQuery(record_type, year_from, year_to, degree, category)
        version_id = runtime.context.index_version
        result_id = result_handle(version_id, query)
        try:
            async with db.begin() as conn:
                items = await find_records(conn, version_id, query)
        except QueryError as error:
            raise ToolException(str(error)) from error
        except UNAVAILABLE:
            logger.exception("query_records failed", extra={"index_version": version_id})
            result = RecordsResult(ToolStatus.FAILED, result_id, query)
        else:
            status = ToolStatus.FOUND if items else ToolStatus.NO_MATCH
            result = RecordsResult(status, result_id, query, items)
        return records_content(result), result

    @tool(response_format="content_and_artifact")
    async def get_product_details(
        product_ids: Annotated[list[str], "1 to 5 product IDs such as r-1a2b3c4d"],
        runtime: ToolRuntime[TurnContext],
    ) -> tuple[str, DetailsResult]:
        """Every catalog field of the named products: company, address, phone, link,
        categories, features, usage and summary. Use it once you know which products: their
        IDs come from query_records or from the products of search_knowledge's passages."""
        if not 0 < len(product_ids) <= DETAILED_PRODUCTS:
            raise ToolException(f"name 1 to {DETAILED_PRODUCTS} products")
        if bad := [h for h in product_ids if not RECORD_HANDLE.fullmatch(h)]:
            raise ToolException(f"not product IDs: {bad}; use the r- IDs tools returned")
        version_id = runtime.context.index_version
        prefixes = [handle.removeprefix("r-") for handle in product_ids]
        try:
            async with db.begin() as conn:
                found = await records_by_prefix(conn, version_id, "product", prefixes)
        except UNAVAILABLE:
            logger.exception("get_product_details failed", extra={"index_version": version_id})
            result = DetailsResult(ToolStatus.FAILED)
        else:
            requested = list(dict.fromkeys(product_ids))  # each once, in the order asked
            shown = {record_handle(product.record_id) for product in found}
            # Two products sharing a prefix are both shown, never one silently.
            products = sorted(found, key=lambda p: requested.index(record_handle(p.record_id)))
            missing = [handle for handle in requested if handle not in shown]
            status = ToolStatus.FOUND if products else ToolStatus.NO_MATCH
            result = DetailsResult(status, products, missing)
        return details_content(result), result

    # A refused argument goes back to the model as an error it can read and correct.
    search_knowledge.handle_tool_error = True
    query_records.handle_tool_error = True
    get_product_details.handle_tool_error = True
    return [search_knowledge, query_records, get_product_details]
