"""What code does with the agent's recommendation: the guards, the choice and the cards.

The agent judges; code chooses (DEC-065, DEC-066). A product passes when this turn's
search found it and the agent judged it against every need: every function supported, no
constraint contradicted. A constraint the catalog does not state passes, and its card says
so. A product already recommended in the conversation is not shown again. Among those
that pass, 世大智科's come first, then the best ranked by the turn's searches, as many as
the user asked for. Code writes the cards from the product's record,
so the maker's page and phone are never the model's.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from wobot.agent.rec_agent import Pick, RecommendProducts

# Products of this company come first among those that pass (DEC-065).
PREFERRED_COMPANY = "世大智科"
# A recommendation names one product unless the user asks for more; "all" stops here too.
MOST_PRODUCTS = 5


# A card's first line, as the labels below write it: the product's name follows.
CARD_NAME = re.compile(r"^(?:推薦：(?P<zh>.+)（|Recommended: (?P<en>.+) \()", re.MULTILINE)


def recommended_before(replies: Iterable[str]) -> set[str]:
    """The products the assistant's earlier replies recommended, by name."""
    return {match["zh"] or match["en"] for reply in replies for match in CARD_NAME.finditer(reply)}


def problems_of(
    pick: Pick,
    args: RecommendProducts,
    evidence: Mapping[str, Any],
    shown_before: frozenset[str] = frozenset(),
) -> list[str]:
    """Why a product may not be shown; none when it passes."""
    product = pick.product_id
    if product not in evidence:
        return [f"{product} was not found by a search this turn"]
    if name_of(evidence[product])[0] in shown_before:
        return [f"{product} was recommended earlier in this conversation"]
    by_need = {check.need: check.status for check in pick.checks}
    found = []
    for number, need in enumerate(args.needs, start=1):
        status = by_need.get(number)
        if status is None:
            found.append(f"{product}: need {number} ({need.text}) is not judged")
        elif need.kind == "function" and status != "supported":
            found.append(f"{product}: function {number} ({need.text}) is {status}")
        elif need.kind == "constraint" and status == "contradicted":
            found.append(f"{product}: constraint {number} ({need.text}) is contradicted")
    return found


def name_of(product: Mapping[str, Any]) -> tuple[str, str]:
    fields = product.get("fields", {})
    return fields.get("product_name", ""), fields.get("company_name", "")


def shown_order(product: Mapping[str, Any]) -> tuple[bool, int]:
    """世大智科's products first, then by their best rank in this turn's searches."""
    return PREFERRED_COMPANY not in name_of(product)[1], product.get("rank", 0)


@dataclass(frozen=True)
class Review:
    shown: list[Pick] = field(default_factory=list)  # the products to show, in order
    problems: list[str] = field(default_factory=list)  # why none could be shown


def review(
    args: RecommendProducts,
    evidence: Mapping[str, Any],
    shown_before: frozenset[str] = frozenset(),
) -> Review:
    """The recommendation held to the guards: the products to show, or why none may be.
    A product that fails is left out; the rest are shown."""
    passed: list[Pick] = []
    problems: list[str] = []
    for pick in {p.product_id: p for p in args.products}.values():
        if found := problems_of(pick, args, evidence, shown_before):
            problems += found
        else:
            passed.append(pick)
    if not passed:
        return Review(problems=problems)
    passed.sort(key=lambda pick: shown_order(evidence[pick.product_id]))
    count = MOST_PRODUCTS if args.count == "all" else min(max(args.count, 1), MOST_PRODUCTS)
    return Review(shown=passed[:count])


LABELS = {
    "zh": {
        "pick": "推薦：{name}（{company}）",
        "supported": "- {need}：符合",
        "unknown": "- {need}：目錄未註明",
        "url": "產品網頁：{url}",
        "phone": "廠商電話：{phone}",
        "fallback": (
            "我找到一些可能相關的產品，但沒辦法用資料確認它們符合你所有的條件，所以這次先不推薦。"
        ),
        "seen": "以下是這次找到的產品，供你參考：",
        "item": "- {name}（{company}）",
        "more": "可以再多告訴我一些你的需求，我再幫你找找看。",
        "none": "我目前沒辦法用資料確認適合的產品。",
        "gap": "",  # between sentences
    },
    "en": {
        "pick": "Recommended: {name} ({company})",
        "supported": "- {need}: met",
        "unknown": "- {need}: not stated in the catalog",
        "url": "Product page: {url}",
        "phone": "Phone: {phone}",
        "fallback": (
            "I found some products that may be related, but I could not confirm from the "
            "sources that they meet all your needs, so I am not recommending one this time."
        ),
        "seen": "These are the products I found, for your reference:",
        "item": "- {name} ({company})",
        "more": "Tell me a little more about what you need, and I will look again.",
        "none": "I could not confirm a product that fits from the sources.",
        "gap": " ",
    },
}


def card(
    pick: Pick, args: RecommendProducts, evidence: Mapping[str, Any], label: Mapping[str, str]
) -> str:
    """A product as code shows it: what it is, why, each need and how it stands, and how
    to reach its maker. A field the catalog does not give is left out (AC-REC-06)."""
    product = evidence[pick.product_id]
    name, company = name_of(product)
    lines = [label["pick"].format(name=name, company=company)]
    if pick.reason.strip():
        lines.append(pick.reason.strip())
    lines += [
        label["supported" if status == "supported" else "unknown"].format(need=need)
        for need, status in checks_by_need(pick, args).items()
    ]
    fields = product["fields"]
    if fields.get("product_url"):
        lines.append(label["url"].format(url=fields["product_url"]))
    if fields.get("contact_phone"):
        lines.append(label["phone"].format(phone=fields["contact_phone"]))
    return "\n".join(lines)


def recommended_reply(
    args: RecommendProducts, shown: Sequence[Pick], evidence: Mapping[str, Any], chinese: bool
) -> dict[str, Any]:
    """The reply to a recommendation the guards passed."""
    label = LABELS["zh" if chinese else "en"]
    cards = [card(pick, args, evidence, label) for pick in shown]
    return {
        "text": "\n\n".join(t for t in [args.intro.strip(), *cards] if t),
        "action": "recommend",
        "products": [
            {
                "id": pick.product_id,
                "name": name_of(evidence[pick.product_id])[0],
                "company": name_of(evidence[pick.product_id])[1],
                "checks": checks_by_need(pick, args),
            }
            for pick in shown
        ],
    }


def checks_by_need(pick: Pick, args: RecommendProducts) -> dict[str, str]:
    """How the product stands on each need, by the need's words."""
    status = {check.need: check.status for check in pick.checks}
    return {need.text: status[n] for n, need in enumerate(args.needs, start=1)}


def fallback_reply(evidence: Mapping[str, Any], chinese: bool) -> dict[str, Any]:
    """What code says when the agent's rounds ran out with nothing shown: no
    recommendation, and the products found, named only, since none was confirmed."""
    label = LABELS["zh" if chinese else "en"]
    found = sorted((e for e in evidence.values() if e["kind"] == "product"), key=shown_order)[
        :MOST_PRODUCTS
    ]
    if not found:
        text = label["none"] + label["gap"] + label["more"]
        return {"text": text, "action": "unverified", "products": []}
    items = [label["item"].format(name=n, company=c) for n, c in map(name_of, found)]
    text = "\n".join([label["fallback"], label["seen"], *items, label["more"]])
    return {"text": text, "action": "unverified", "products": []}


def refusal(problems: Sequence[str]) -> str:
    """What the agent reads back when no product passed."""
    return (
        "Not shown: no product meets every need. "
        + "; ".join(problems)
        + ". Search again, ask the user, or say which need the catalog does not state."
    )
