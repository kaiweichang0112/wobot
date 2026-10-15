"""What of a reply is spoken (DEC-089, P16): never a web address, phone number or postal
address, and of a long list only its first items.

Chat and knowledge replies are spoken as they are streamed, cut into sentences. A list
or a recommendation is written by code, so code also writes what is said of it: from
the reply's own parts, so nothing is said that the reply does not show.
"""

import re
from collections.abc import Mapping
from typing import Any

from wobot.agent.recommend import need_label

SPOKEN_ITEMS = 3  # items of the first list that are read out

URL = re.compile(r"https?://\S+")
# A label that only introduces a link, with the link: 「，全文：https://…」, ", PDF: https://…".
LINKED = re.compile(r"[，,]?\s*[^\s，,：:]{1,8}\s*[：:]\s*https?://\S+")
# Fields of a card or a line that are read, not heard.
UNSPOKEN_FIELDS = re.compile(
    r"^(產品網頁|廠商電話|Product page|Phone)[：:].*$|[，,]?\s*(地點|電話|地址)[：:][^，,]*",
    re.MULTILINE,
)

LABELS = {
    "zh": {
        "rest": "完整清單在文字裡。",
        "product": "{name}，{company}。",
        "meets": "符合{needs}。",
        "unknown": "目錄沒寫{needs}。",
        "and": "、",
    },
    "en": {
        "rest": "The full list is in the transcript.",
        "product": "{name}, by {company}.",
        "meets": "It meets {needs}.",
        "unknown": "The catalog does not say {needs}.",
        "and": ", ",
    },
}


def speakable(text: str) -> str:
    """The text without what is read, not heard: links, phone numbers and addresses."""
    text = LINKED.sub("", text)
    text = URL.sub("", text)
    text = UNSPOKEN_FIELDS.sub("", text)
    return re.sub(r"[ \t　]+", " ", text).strip()


def spoken_text(route: str | None, reply: Mapping[str, Any], chinese: bool) -> str | None:
    """What is said of a reply code wrote; None for one that was spoken as it streamed."""
    if route in ("chat", "knowledge") or not reply.get("text"):
        return None
    if reply.get("lists") is not None:
        return _spoken_list(reply["text"], chinese)
    if reply.get("action") == "recommend":
        return _spoken_recommendation(reply, chinese)
    # A question, why nothing fits, or what code says when the agent ran out of rounds.
    lines = [line.removeprefix("- ") for line in reply["text"].splitlines()]
    return speakable("\n".join(lines))


def _spoken_list(text: str, chinese: bool) -> str:
    """The intro, then the first items of the first list, without their links."""
    label = LABELS["zh" if chinese else "en"]
    intro, *blocks = text.split("\n\n")
    items = []
    for line in (blocks[0].splitlines() if blocks else [])[: SPOKEN_ITEMS + 1]:
        if match := re.match(r"^\d+\.\s+(.*)$", line):
            items.append(speakable(match.group(1).replace("　", "，")))
    rest = [label["rest"]] if blocks else []
    return "\n".join([intro, *items[:SPOKEN_ITEMS], *rest])


def _spoken_recommendation(reply: Mapping[str, Any], chinese: bool) -> str:
    """The opening, then each product with the needs it meets and those the catalog does
    not state, then how many more fit: the card's facts, said without its links."""
    label = LABELS["zh" if chinese else "en"]
    blocks = reply["text"].split("\n\n")
    lines = [blocks[0]]
    for product in reply.get("products", []):
        lines.append(label["product"].format(name=product["name"], company=product["company"]))
        checks = product.get("checks", {})
        for state, key in (("supported", "meets"), ("unknown", "unknown")):
            needs = [need_label(n, chinese) for n, s in checks.items() if s == state]
            if needs:
                lines.append(label[key].format(needs=label["and"].join(needs)))
    if len(blocks) > 1 + len(reply.get("products", [])):
        lines.append(blocks[-1])  # how many more fit
    return speakable("\n".join(lines))
