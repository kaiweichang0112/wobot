"""What of a reply is spoken (DEC-089, P16): never a web address, phone number or postal
address, and of a long list only its first items, the rest a page at a time when the
user asks to hear more (DEC-096).

Chat and knowledge replies are spoken as they are streamed, cut into sentences. A list
or a recommendation is written by code, so code also writes what is said of it: from
the reply's own parts, so nothing is said that the reply does not show.
"""

import re
from collections.abc import Mapping
from typing import Any

from wobot.agent.recommend import need_label

SPOKEN_ITEMS = 3  # items of a list read out in its turn
PAGE_ITEMS = 5  # items read out each time the user asks to hear more

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
        "more": "還有 {n} 筆可以繼續聽。",
        "product": "{name}，{company}。",
        "meets": "符合{needs}。",
        "unknown": "目錄沒寫{needs}。",
        "and": "、",
    },
    "en": {
        "more": "There are {n} more to hear.",
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
    """The intro, then the first items, without their links."""
    intro, *_ = text.split("\n\n", 1)
    page, _ = list_page(text, 0, SPOKEN_ITEMS, chinese)
    return "\n".join(line for line in (intro, page) if line)


def list_lines(text: str) -> list[tuple[bool, str]]:
    """What can be said of a list reply after its intro, a line at a time, each marked
    whether it is an item: the lists' headings and items as the text shows them. The
    text holds every item, so the list read later is the one the user was shown."""
    _, _, rest = text.partition("\n\n")
    lines = []
    for line in rest.splitlines():
        if not line.strip():
            continue
        item = re.match(r"^(?:\d+\.|-)\s+(.*)$", line)
        said = speakable((item.group(1) if item else line).replace("　", "，"))
        if said:
            lines.append((item is not None, said))
    return lines


def list_page(text: str, start: int, items: int, chinese: bool) -> tuple[str, int | None]:
    """The lines of a list reply from `start` through `items` items, then how many are
    left; and where the next page starts, None when none is left. The same start always
    gives the same page, so asking again after a lost reply repeats nothing."""
    lines = list_lines(text)
    page: list[str] = []
    said, end = 0, start
    for is_item, line in lines[start:]:
        if said == items:
            break
        page.append(line)
        said += is_item
        end += 1
    left = sum(is_item for is_item, _ in lines[end:])
    if left:
        page.append(LABELS["zh" if chinese else "en"]["more"].format(n=left))
    return "\n".join(page), end if left else None


def continue_from(reply: Mapping[str, Any]) -> int | None:
    """Where reading a list reply goes on once its turn has said the first items; None
    for any other reply, or a list said in full."""
    if reply.get("lists") is None or not reply.get("text"):
        return None
    return list_page(reply["text"], 0, SPOKEN_ITEMS, True)[1]


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
