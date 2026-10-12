"""What code shows of a recommendation (DEC-065, DEC-069): which products, and their cards.

find_products judges and ranks every product (`find.py`); a model never does. When the
user has said enough, code shows the first that fit at once, up to five unless the user
asked for fewer, and how many more fit. When the agent read products' text for a need
the vocabulary has no word for, it names those that meet it, and code shows them in its
own order. Code writes each card
from the record: every need with the catalog's own words for it, or that the catalog
does not state it, and how to reach the maker, so none of it is the model's. A product
recommended earlier in the conversation is never found again, so never shown twice.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage

from wobot.agent.find import FindProducts, Found
from wobot.agent.rec_agent import ShowProducts, reads_text
from wobot.knowledge.facets import ENGLISH_NAMES

# A recommendation shows up to five, or as many as the user asked for; "all" stops here too.
MOST_PRODUCTS = 5


# A card's first line, as the labels below write it: the product's name follows.
CARD_NAME = re.compile(r"^(?:推薦：(?P<zh>.+)（|Recommended: (?P<en>.+) \()", re.MULTILINE)


def recommended_before(replies: Iterable[str]) -> set[str]:
    """The products the assistant's earlier replies recommended, by name."""
    return {match["zh"] or match["en"] for reply in replies for match in CARD_NAME.finditer(reply)}


def heeded(args: FindProducts, messages: Sequence[BaseMessage]) -> FindProducts:
    """The find as code runs it: what the user left open counts only when their message
    answers a question, the assistant's last message being no recommendation (DEC-075)."""
    earlier = [m for m in messages[:-1] if isinstance(m, AIMessage)]
    answering = bool(earlier) and not CARD_NAME.search(earlier[-1].text)
    if answering or not args.no_preference:
        return args
    return args.model_copy(update={"no_preference": []})


def shown_count(count: int | Literal["all"] | None) -> int:
    """How many products a recommendation shows: up to five unless the user named fewer."""
    if count is None or count == "all":
        return MOST_PRODUCTS
    return min(count, MOST_PRODUCTS)


def shows_at_once(found: Found, args: FindProducts) -> bool:
    """Whether code shows a find's products without the agent: some fit, there is nothing
    to ask, and no need waits for the agent to read the products' text."""
    return bool(found.fitting) and not found.decisive and not reads_text(args)


def first_fitting(found: Mapping[str, Any]) -> list[str]:
    """The products a find shows at once, by ID: the best ranked, as many as asked for."""
    return [p["id"] for p in found["products"]][: shown_count(found["count"])]


def picked(args: ShowProducts, found: Mapping[str, Any] | None) -> tuple[list[str], str | None]:
    """The products the agent named that this turn's find found, in code's order and as
    many as asked for, or why none can be shown."""
    if not found or not found["products"]:
        return [], "call find_products first: this turn found no product"
    known = [p["id"] for p in found["products"]]
    if unknown := [handle for handle in args.products if handle not in known]:
        return [], f"{', '.join(unknown)} not among the products find_products found"
    named = set(args.products)
    return [h for h in known if h in named][: shown_count(found["count"])], None


# --- Cards ----------------------------------------------------------------------------------

# What a card calls each facet and value, in Chinese and in English.
FACET_LABELS = {
    "form": ("型態", "Form"),
    "places": ("使用場所", "Place of use"),
    "sensing": ("感測方式", "Sensing"),
    "viewing": ("查看方式", "Viewed on"),
    "power": ("電源", "Power"),
    "users": ("使用人數", "Users"),
}
VALUE_LABELS = {
    "form": {
        "worn": ("穿戴式", "worn"),
        "bed": ("床墊型", "bed"),
        "room": ("室內安裝", "installed in the room"),
        "device": ("量測設備", "measuring device"),
        "equipment": ("訓練設備", "training equipment"),
        "software": ("軟體", "software"),
        "service": ("服務", "service"),
        "robot": ("機器人", "robot"),
    },
    "places": {
        "home": ("居家", "home"),
        "institution": ("長照機構", "care home"),
        "hospital": ("醫院", "hospital"),
        "public": ("公共場所", "public places"),
    },
    "sensing": {
        "camera": ("攝影機", "camera"),
        "radar": ("雷達", "radar"),
        "wifi": ("Wi-Fi 感測", "Wi-Fi sensing"),
        "pressure": ("壓力感測", "pressure"),
        "fiber": ("光纖", "optical fiber"),
        "infrared": ("紅外線", "infrared"),
    },
    "viewing": {
        "app": ("手機 App", "phone app"),
        "computer": ("電腦", "computer"),
        "line": ("LINE", "LINE"),
        "sms": ("簡訊", "text message"),
        "phone": ("電話", "phone call"),
        "device": ("裝置本身", "the device itself"),
    },
    "power": {"mains": ("插電", "mains"), "battery": ("電池", "battery")},
    "users": {"group": ("多人", "group"), "individual": ("單人", "one person")},
}

LABELS = {
    "zh": {
        "pick": "推薦：{name}（{company}）",
        "supported": "- {need}：符合（目錄：「{words}」）",
        "unknown": "- {need}：目錄未註明",
        "url": "產品網頁：{url}",
        "phone": "廠商電話：{phone}",
        "one": "這是符合你需求的產品：",
        "many": "這些是符合你需求的產品：",
        "is": "為{value}",
        "or": "或",
        "not": "不是{value}",
        "condition": "{facet}{value}",
        "fallback": (
            "我找到一些可能相關的產品，但沒辦法用資料確認它們符合你所有的條件，所以這次先不推薦。"
        ),
        "seen": "以下是這次找到的產品，供你參考：",
        "item": "- {name}（{company}）",
        "more": "可以再多告訴我一些你的需求，我再幫你找找看。",
        "none": "我目前沒辦法用資料確認適合的產品。",
        "others": "另有 {n} 項產品也符合，想看的話再跟我說。",
        "gap": "",  # between sentences
    },
    "en": {
        "pick": "Recommended: {name} ({company})",
        "supported": "- {need}: met (catalog: 「{words}」)",
        "unknown": "- {need}: not stated in the catalog",
        "url": "Product page: {url}",
        "phone": "Phone: {phone}",
        "one": "Here is a product that fits your needs:",
        "many": "Here are products that fit your needs:",
        "is": "is {value}",
        "or": " or ",
        "not": "is not {value}",
        "condition": "{facet} {value}",
        "fallback": (
            "I found some products that may be related, but I could not confirm from the "
            "sources that they meet all your needs, so I am not recommending one this time."
        ),
        "seen": "These are the products I found, for your reference:",
        "item": "- {name} ({company})",
        "more": "Tell me a little more about what you need, and I will look again.",
        "none": "I could not confirm a product that fits from the sources.",
        "others": "Products that also fit but are not shown: {n}. Ask me to see them.",
        "gap": " ",
    },
}


def need_label(need: str, chinese: bool) -> str:
    """A need as a card names it: a function by its name, a condition such as form!=worn
    or places=home|institution by its facet and values."""
    match = re.fullmatch(r"(\w+)(!?=)([\w|]+)", need)
    if match is None:  # a function
        return need if chinese else ENGLISH_NAMES.get(need, need)
    facet, sign, values = match.groups()
    language = 0 if chinese else 1
    label = LABELS["zh" if chinese else "en"]
    value_label = label["or"].join(VALUE_LABELS[facet][v][language] for v in values.split("|"))
    words = label["not" if sign == "!=" else "is"].format(value=value_label)
    return label["condition"].format(facet=FACET_LABELS[facet][language], value=words)


def name_of(product: Mapping[str, Any]) -> tuple[str, str]:
    fields = product.get("fields", {})
    return fields.get("product_name", ""), fields.get("company_name", "")


def card(product: Mapping[str, Any], needs: Sequence[str], chinese: bool) -> str:
    """A product as code shows it: each need with the catalog's words for it, or that the
    catalog does not state it, and how to reach the maker. A field the catalog does not
    give is left out (AC-REC-06)."""
    label = LABELS["zh" if chinese else "en"]
    name, company = name_of(product)
    lines = [label["pick"].format(name=name, company=company)]
    for need in needs:
        words = product["needs"].get(need, "unknown")
        shown = need_label(need, chinese)
        if words == "unknown":
            lines.append(label["unknown"].format(need=shown))
        else:
            lines.append(label["supported"].format(need=shown, words=words))
    fields = product["fields"]
    if fields.get("product_url"):
        lines.append(label["url"].format(url=fields["product_url"]))
    if fields.get("contact_phone"):
        lines.append(label["phone"].format(phone=fields["contact_phone"]))
    return "\n".join(lines)


def recommended_reply(
    found: Mapping[str, Any],
    shown: Sequence[str],
    chinese: bool,
    intro: str = "",
    fitting: int | None = None,
) -> dict[str, Any]:
    """The reply showing the products, by ID, in order: an intro, a card each and, when
    more fit than are shown, how many (DEC-075). `fitting` is how many fit: the find's
    count unless the agent judged them from their text."""
    label = LABELS["zh" if chinese else "en"]
    by_id = {p["id"]: p for p in found["products"]}
    products = [by_id[handle] for handle in shown]
    opening = intro.strip() or label["one" if len(products) == 1 else "many"]
    cards = [card(product, found["needs"], chinese) for product in products]
    others = (found["matched"] if fitting is None else fitting) - len(products)
    tail = [label["others"].format(n=others)] if others > 0 else []
    return {
        "text": "\n\n".join([opening, *cards, *tail]),
        "action": "recommend",
        "products": [
            {
                "id": product["id"],
                "name": name_of(product)[0],
                "company": name_of(product)[1],
                "checks": {
                    need: "unknown" if product["needs"].get(need) == "unknown" else "supported"
                    for need in found["needs"]
                },
            }
            for product in products
        ],
    }


def fallback_reply(evidence: Mapping[str, Any], chinese: bool) -> dict[str, Any]:
    """What code says when the agent's rounds ran out with nothing shown. Products the
    last find judged to fit are shown; those found for a need only the agent could judge
    from their text are named, unconfirmed."""
    found = evidence.get("found")
    if found and found["products"] and not found["reads_text"]:
        return recommended_reply(found, first_fitting(found), chinese)
    label = LABELS["zh" if chinese else "en"]
    if not found or not found["products"]:
        text = label["none"] + label["gap"] + label["more"]
        return {"text": text, "action": "unverified", "products": []}
    items = [
        label["item"].format(name=n, company=c)
        for n, c in map(name_of, found["products"][:MOST_PRODUCTS])
    ]
    text = "\n".join([label["fallback"], label["seen"], *items, label["more"]])
    return {"text": text, "action": "unverified", "products": []}
