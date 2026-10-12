"""The recommendation path's agent (DEC-066, DEC-069): a ReAct loop that decides, from the
conversation, whether to ask the user, find products or say why none fits.

The model only turns what the user said into a query in the facet vocabulary; it never
judges a product. find_products hands the query to code (`find.py`), which judges every
product from the facets ingestion read, ranks those that fit and, when they need no
question, shows them at once. Otherwise the model reads back what code found: the facets
that split the fitting products, to ask about, or how many each need leaves, to explain
why none fits. Only for a need the vocabulary has no word for does the model read the
products' catalog text, and name with show_products those that meet it. A reply with no
tool call is the model's own: a question, or why nothing fits.
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wobot.agent.find import CARD_FIELDS, TEXT_FIELDS, FindProducts, Found, stated
from wobot.knowledge.facets import condition_name, conditions_of, vocabulary

logger = logging.getLogger(__name__)

# Bump with any change to the instructions or the tools; a test pins each version.
PROMPT_VERSION = 11

# The conversation's last messages the agent reads: enough for the needs stated over a
# few turns and the question it last asked.
RECENT_MESSAGES = 8
# Fitting products the agent reads of a find, best first: the cards show at most 5.
PRODUCTS_READ = 10
# Products whose catalog text the agent reads for a need outside the vocabulary: each is
# about 750 tokens (DEC-069).
TEXTS_READ = 5

INSTRUCTIONS = f"""\
You recommend smart-care products for Wobot, the assistant of the Gerontechnology \
Research Center (GRC) at Yuan Ze University; chatbot_name is what the user calls it. Help \
the user find a product of the catalog that meets their needs.

Read the conversation and decide:
- The user has not said what the product must do: ask what they need it for, in one or \
two short questions.
- You know what it must do: call find_products with every need the user has stated in \
this conversation, leaving out any they dropped or changed. Code judges each product \
from its catalog and shows those that fit.
- find_products answers with decisive: the fitting products differ in what the user has \
not said. Ask about every facet it names, in one or two short questions, naming the values \
in plain words, such as at home or in a care home. That question is your reply: never \
answer it yourself with another find_products, and never pick a product yourself.
- It answers that nothing fits: say plainly which need no product meets, from gaps, and \
what the user could change, such as a need without which some products fit.
- It answers with products and their text, because you gave other needs: call \
show_products with those whose text meets the other needs; if none does, say so.
Never ask again what the user has said, and do not ask about budget or use cases. Never \
name a product in your own words: code shows them.

How to write the needs:
- functions: what the product must do, chosen from the list below by meaning, not by the \
user's words: 離床預警 is 離床偵測. A product must do every function you name, so name \
only what the user asked it to do, never a nearby one that might also help: a person \
who keeps falling needs 跌倒偵測; 跌倒預防 only when the user asks to prevent falls or \
rate their risk. A nearby function is not the same: sleep is not leaving the bed. \
復健訓練 takes any kind of rehabilitation.
- must and must_not: what it must or must not be.
  form: worn (on the person cared for), bed (a mattress, or a pad on or under it), room \
(on a wall, ceiling or bedside), device (only measures), equipment (for training), \
software, service, robot. 不用穿戴 is must_not form worn.
  places: home, institution (long-term care, nursing homes, day care), hospital, public. \
A sickbed (病床) or nurses alone name no place.
  sensing: camera, radar, wifi, pressure, fiber, infrared. 不要攝影機 is must_not sensing \
camera.
  viewing: app (a phone or tablet), computer, line, sms, phone (a voice call), device \
(the unit's own screen or a nurse station).
  power: mains (plugged in), battery. 不用插電 is must_not power mains.
  users: group, individual.
- other: a need none of these can say, in the user's words; never one a function or \
condition above already says.
- count: the number the user asked for, left out when they named none; all when they ask \
for every product that fits.
- no_preference: only when the user answers your question by saying any of its choices \
will do. all when they leave the choice to you, such as 你推薦就好 or 你決定, even when \
the question asked about one thing; otherwise 都可以 names the facets that question asked \
about and no other. Leave it out when the user did not answer a question: a request such \
as 推薦三個 leaves nothing open. A question answered with a value goes in must or \
must_not instead, and leaves every other facet out of no_preference.
When the user asks for more, such as 還有其他的嗎, call find_products with the same needs \
and the count the user gave, if any: asking for more names no number, and code leaves out \
what it showed before.

Functions, with how a user may say them and the nearby ones they are not:
{vocabulary()}

Reply in the language of the user's latest message even though the catalog is in \
Chinese: English for English, Traditional Chinese for Chinese. Write as in a chat: short \
plain text without Markdown, and no word of tools, searches, IDs or catalog categories. Do \
not introduce yourself."""


class ShowProducts(BaseModel):
    """Shows the user, as cards, the products found whose catalog text meets the needs
    the vocabulary has no word for. Code orders them and writes the cards."""

    model_config = ConfigDict(title="show_products")

    products: list[str] = Field(
        min_length=1, description="the IDs of the products to show, such as r-1a2b3c4d"
    )
    intro: str = Field(
        description="one sentence before the products, in the language of the user's latest "
        "message, naming none"
    )


REC_TOOLS: tuple[type[BaseModel], ...] = (FindProducts, ShowProducts)
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


def reads_text(args: FindProducts) -> bool:
    """Whether a need has no word in the vocabulary, so the agent reads the products' text."""
    return any(need.strip() for need in args.other)


def needs_of(args: FindProducts) -> list[str]:
    """The query's needs as code names them, functions first: places=home, form!=worn."""
    query = args.query
    conditions = [condition_name(f, *vs, must=m) for f, vs, m in conditions_of(query)]
    return [*query.functions, *conditions]


def found_evidence(found: Found, args: FindProducts) -> dict[str, Any]:
    """What the turn keeps of a find, as plain values for the checkpointer: the needs, how
    many to show, and the fitting products the agent read, best first, each with its card
    fields and the catalog's words for each need."""
    read = TEXTS_READ if reads_text(args) else PRODUCTS_READ
    return {
        "needs": needs_of(args),
        "count": args.count,
        "matched": len(found.fitting),
        "decisive": [split.facet for split in found.decisive],
        "reads_text": reads_text(args),
        "products": [
            {
                "id": r.product.handle,
                "key": r.product.key,
                "fields": {k: v for k in CARD_FIELDS if (v := r.product.fields.get(k))},
                "needs": stated(r.product, args.query),
            }
            for r in found.fitting[:read]
        ],
    }


def found_content(found: Found, args: FindProducts) -> dict[str, Any]:
    """What the agent reads of a find that code did not show at once."""
    text = reads_text(args)
    products = []
    for ranked in found.fitting[: TEXTS_READ if text else PRODUCTS_READ]:
        fields = ranked.product.fields
        item: dict[str, Any] = {
            "id": ranked.product.handle,
            "name": ranked.product.name,
            "company": fields.get("company_name", ""),
            "needs": stated(ranked.product, args.query),
        }
        if text:
            item["text"] = "\n".join(fields[f] for f in TEXT_FIELDS if fields.get(f))
        products.append(item)
    return {
        "matched": len(found.fitting),
        "products": products,
        "decisive": [asdict(split) for split in found.decisive],
        "gaps": [asdict(gap) for gap in found.gaps],
        "recommended_before": found.recommended_before,
    }
