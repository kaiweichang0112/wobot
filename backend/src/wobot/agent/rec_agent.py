"""The recommendation path's agent (DEC-066): a ReAct loop that decides, from the
conversation, whether to ask the user, search the catalog or recommend.

The model calls two tools. search_products finds the catalog's products closest to a
query, each with its catalog text, so the model judges every product from what it read;
recommend_products hands code the products it judges to meet every need, need by need.
Code runs both in `rec_tools`: it keeps the products each search found as the turn's
evidence, and holds a recommendation to the guards in `recommend.py` before showing it. A
reply with no tool call is the model's own: a question, or why nothing fits.
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wobot.agent.retrieval import UNAVAILABLE, LookupFailed, record_handle, search_knowledge
from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.lists import records_by_prefix
from wobot.knowledge.repository import Database

logger = logging.getLogger(__name__)

# Bump with any change to the instructions or the tools; a test pins each version.
PROMPT_VERSION = 5

# The conversation's last messages the agent reads: enough for the needs stated over a
# few turns and the question it last asked.
RECENT_MESSAGES = 8
# Products one search returns. A product is one passage of about 750 tokens, and the
# fitting ones of the labelled questions ranked within the first 10 (V6 probe).
SEARCHED_PRODUCTS = 10
# Needs one recommendation may list: few enough to judge each against every product.
MOST_NEEDS = 8
# The fields kept of each product found: the card shows the maker's page and phone.
PRODUCT_FIELDS = (
    "product_name",
    "company_name",
    "contact_phone",
    "product_url",
)

INSTRUCTIONS = """\
You recommend smart-care products for Wobot, the assistant of the Gerontechnology \
Research Center (GRC) at Yuan Ze University; chatbot_name is what the user calls it. Help \
the user find a product of the catalog that meets their needs.

Read the conversation and decide what is missing:
- The user has not said what the product must do: ask what they need it for, in one or \
two short questions.
- You know what it must do: call search_products with the functions. Each search \
returns the closest products with their catalog text.
- The products that meet the needs differ in something only the user can tell, and it \
decides which fits, such as use at home or in a care home: ask about it.
- Some products meet every need: call recommend_products with every one of them. Code \
checks them, chooses and shows them to the user.
- None does: say plainly which need the catalog does not state, and what the user could \
change.
Never ask again what the user has said, and do not ask about budget or use cases. When \
the user answers that any of the choices you asked about will do, such as 都可以, recommend \
every product that meets the needs: count all.

Each need is a function, what the product must do, such as 離床預警, or a constraint, \
what it must be or where it is used, such as 不用穿戴 or 居家使用. Judge every product \
against every need from its catalog text alone:
- supported: the text states it, in whatever words. A nearby function is not the same: \
sleep monitoring is not a bed-exit alert, and a phone app is not viewing on a computer. \
A constraint is supported when the product's form or way of sensing settles it, such as \
a pad under the mattress for not worn.
- contradicted: the text states the opposite, or names only another place of use, such \
as wards, institutions or long-term care settings. A sickbed (病床) or nurses alone do not \
tie a product to a care home.
- unknown: the text says nothing of it.
A product meets the needs when every function is supported and no constraint is \
contradicted. Do not recommend again a product already recommended in this \
conversation: code does not show it.

Reply, and write intro and reason, in the language of the user's latest message even \
though the catalog is in Chinese: English for English, Traditional Chinese for Chinese. \
Write as in a chat: short plain text without Markdown, and no word of tools, searches, IDs \
or catalog categories. Do not introduce yourself."""


class SearchProducts(BaseModel):
    """The catalog's products closest in meaning to the functions, best first, each with
    its catalog text: name, company, features, usage and summary."""

    model_config = ConfigDict(title="search_products")

    functions: list[str] = Field(
        min_length=1,
        max_length=MOST_NEEDS,
        description="what the product must do, one function each, in Traditional Chinese, "
        "such as 離床預警. No constraint: a search cannot tell 不用穿戴 from 穿戴, so judge "
        "what it must be and where it is used from the catalog text",
    )

    @property
    def query(self) -> str:
        """The functions as one search: a product that does them all comes closest."""
        return "、".join(f.strip() for f in self.functions if f.strip())


class Need(BaseModel):
    text: str = Field(description="the need in the user's words, such as 離床預警")
    kind: Literal["function", "constraint"] = Field(
        description="function: what the product must do; constraint: what it must be or "
        "where it is used"
    )


class NeedCheck(BaseModel):
    need: int = Field(description="the need's number in needs, counting from 1")
    status: Literal["supported", "contradicted", "unknown"]


class Pick(BaseModel):
    product_id: str = Field(description="the product's ID, such as r-1a2b3c4d")
    checks: list[NeedCheck] = Field(description="one for each need")
    reason: str = Field(
        description="one or two sentences on how it meets the needs, in the language of the "
        "user's latest message"
    )


class RecommendProducts(BaseModel):
    """Shows the user the products that meet every need, as cards with the maker's page
    and phone. Code checks each against the needs, chooses among them and writes the
    cards."""

    model_config = ConfigDict(title="recommend_products")

    needs: list[Need] = Field(
        min_length=1, max_length=MOST_NEEDS, description="every need the user has stated, once"
    )
    products: list[Pick] = Field(
        min_length=1, description="every product found this turn that meets the needs"
    )
    count: int | Literal["all"] = Field(
        description="how many products the user asked for: 1 unless they named a number; "
        "all when they ask for every product that fits, or answer that any choice will do"
    )
    intro: str = Field(
        description="one sentence before the products, in the language of the user's latest "
        "message, naming none"
    )


REC_TOOLS: tuple[type[BaseModel], ...] = (SearchProducts, RecommendProducts)
TOOLS_BY_NAME = {tool.model_config["title"]: tool for tool in REC_TOOLS}


# Where a reply runs into the provider's tool-call syntax, which some models leak as text.
LEAKED_CALL = re.compile(r"[”\"」』】]*【\s*assistant|to=functions\.|<\|")


def own_words(text: str) -> str:
    """The reply up to any leaked tool-call syntax: the user never sees the residue."""
    if match := LEAKED_CALL.search(text):
        logger.warning("the agent's reply leaked tool-call syntax; cut there")
        text = text[: match.start()]
    return text.strip()


def rec_agent_prompt(messages: Sequence[BaseMessage], chatbot_name: str) -> list[BaseMessage]:
    """The rules, then the conversation's last messages; the loop's own messages follow."""
    named = f"{INSTRUCTIONS}\n\nchatbot_name: {json.dumps(chatbot_name, ensure_ascii=False)}"
    return [SystemMessage(named), *messages[-RECENT_MESSAGES:]]


def read_call(call: Mapping[str, Any]) -> BaseModel | ToolMessage:
    """A tool call's arguments as its schema, or the refusal the model reads back."""
    tool = TOOLS_BY_NAME.get(call["name"])
    if tool is None:
        return refused(call, f"there is no tool {call['name']}")
    try:
        return tool.model_validate(call["args"])
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        return refused(call, problems)


def refused(call: Mapping[str, Any], reason: str) -> ToolMessage:
    return ToolMessage(f"Not done: {reason}.", tool_call_id=call["id"], status="error")


def answered(call: Mapping[str, Any], content: Mapping[str, Any]) -> ToolMessage:
    return ToolMessage(json.dumps(content, ensure_ascii=False), tool_call_id=call["id"])


async def search_products(
    db: Database,
    embedder: Embedder,
    version_id: int,
    call: Mapping[str, Any],
    args: SearchProducts,
    evidence: Mapping[str, Any],
) -> tuple[ToolMessage, dict[str, Any]]:
    """What the model reads of the products found, and the evidence with them added: each
    product by its r- handle, with its fields for the card and its best rank this turn.
    Raises LookupFailed when the database or the embedding provider cannot be reached."""
    passages = await search_knowledge(
        db, embedder, version_id, [args.query], limit=SEARCHED_PRODUCTS, record_type="product"
    )
    # A catalog passage is one product's row (product_row@1), so each names one product.
    ranked = [
        (passage, product)
        for passage in passages
        for product in passage["records"]
        if product["type"] == "product"
    ]
    try:
        async with db.begin() as conn:
            read = await records_by_prefix(
                conn,
                version_id,
                "product",
                [product["id"].removeprefix("r-") for _, product in ranked],
            )
    except UNAVAILABLE as error:
        logger.exception("product fields failed", extra={"index_version": version_id})
        raise LookupFailed from error
    fields = {record_handle(item.record_id): item.fields for item in read}
    merged = dict(evidence)
    shown = []
    for rank, (passage, product) in enumerate(ranked, start=1):
        handle = product["id"]
        kept = merged.get(handle, {}).get("rank")
        merged[handle] = {
            "kind": "product",
            "key": product["key"],
            "rank": rank if kept is None else min(rank, kept),
            "fields": {k: v for k in PRODUCT_FIELDS if (v := fields.get(handle, {}).get(k))},
            "source": product["source"],
        }
        shown.append({"id": handle, "category": passage["header"], "text": passage["text"]})
    return answered(call, {"products": shown}), merged
