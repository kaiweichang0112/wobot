import pytest

from tests.agent.test_find import bed_exit
from wobot.agent.find import FindProducts, find
from wobot.agent.rec_agent import ShowProducts
from wobot.agent.recommend import (
    MOST_PRODUCTS,
    VALUE_LABELS,
    fallback_reply,
    first_fitting,
    need_label,
    picked,
    recommended_before,
    recommended_reply,
    shown_count,
    shows_at_once,
)
from wobot.knowledge.facets import ENGLISH_NAMES, FACET_VALUES, FUNCTION_GROUPS, FUNCTION_NAMES

NEEDS = ["離床偵測", "places=home", "form!=worn"]


def found_product(handle: str, name: str, company: str, needs: dict, **fields) -> dict:
    return {
        "id": handle,
        "key": f"product:{name}",
        "fields": {"product_name": name, "company_name": company} | fields,
        "needs": needs,
    }


WHIZPAD = found_product(
    "r-bbbbbbbb",
    "WhizPad 安心臥智慧床墊",
    "世大智科股份有限公司",
    {"離床偵測": "離床提醒", "places=home": "居家", "form!=worn": "鋪於床墊下"},
    product_url="https://example.org/whizpad",
    contact_phone="03-1234567",
)
IPROTEK = found_product(
    "r-aaaaaaaa",
    "iProték 智慧床墊",
    "嘉碩生醫電子股份有限公司",
    {"離床偵測": "離床警示", "places=home": "unknown", "form!=worn": "床墊"},
)


def found(*products: dict, count=None, reads_text=False, matched=None) -> dict:
    return {
        "needs": NEEDS,
        "count": count,
        "matched": len(products) if matched is None else matched,
        "reads_text": reads_text,
        "products": list(products),
    }


# --- Which products ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "shown"),
    [(None, MOST_PRODUCTS), (1, 1), (3, 3), (9, MOST_PRODUCTS), ("all", MOST_PRODUCTS)],
)
def test_up_to_five_are_shown_unless_the_user_named_fewer(count, shown):
    assert shown_count(count) == shown


def test_code_shows_at_once_what_fits_when_nothing_is_left_to_ask():
    catalog = [
        bed_exit("A", places={"home": "居家"}),
        bed_exit("B", places={"institution": "機構"}),
    ]

    def at_once(**args) -> bool:
        query = FindProducts(**args)
        return shows_at_once(find(catalog, query.query, query.count, {}), query)

    assert at_once(functions=["離床偵測"], must={"places": ["home"]})
    assert at_once(functions=["離床偵測"], count="all")
    assert not at_once(functions=["離床偵測"])  # where it is used splits them: ask
    assert not at_once(functions=["離床偵測"], count=1)  # a number still asks (DEC-075)
    assert not at_once(functions=["離床偵測"], count=1, other=["夜間靜音"])  # the agent reads
    assert not at_once(functions=["翻身監測"], count=1)  # none fits: the agent explains


def test_a_find_shows_its_best_ranked_as_many_as_asked_for():
    assert first_fitting(found(WHIZPAD, IPROTEK, count=1)) == ["r-bbbbbbbb"]
    assert first_fitting(found(WHIZPAD, IPROTEK)) == ["r-bbbbbbbb", "r-aaaaaaaa"]


def test_the_agent_shows_only_what_the_find_found_in_code_s_order():
    show = ShowProducts(products=["r-aaaaaaaa", "r-bbbbbbbb"], intro="這兩項都很安靜。")

    assert picked(show, found(WHIZPAD, IPROTEK, count=2)) == (["r-bbbbbbbb", "r-aaaaaaaa"], None)
    assert picked(show, found(WHIZPAD, IPROTEK, count=1)) == (["r-bbbbbbbb"], None)
    assert picked(ShowProducts(products=["r-cccccccc"], intro=""), found(WHIZPAD)) == (
        [],
        "r-cccccccc not among the products find_products found",
    )
    assert picked(show, None)[1] == "call find_products first: this turn found no product"


def test_a_recommendation_says_how_many_more_fit_than_it_shows():
    shown = ["r-bbbbbbbb"]

    reply = recommended_reply(found(WHIZPAD, IPROTEK, count=1, matched=7), shown, True)
    english = recommended_reply(found(WHIZPAD, matched=3), shown, False)
    judged = recommended_reply(found(WHIZPAD, IPROTEK, matched=7), shown, True, fitting=2)
    every = recommended_reply(found(WHIZPAD), shown, True)

    assert reply["text"].endswith("\n\n另有 6 項產品也符合，想看的話再跟我說。")
    assert english["text"].endswith(
        "\n\nProducts that also fit but are not shown: 2. Ask me to see them."
    )
    assert judged["text"].endswith("另有 1 項產品也符合，想看的話再跟我說。")  # the agent's
    assert "另有" not in every["text"]


# --- Cards ------------------------------------------------------------------------------


def test_a_card_quotes_the_catalog_for_each_need_and_shows_how_to_reach_the_maker():
    reply = recommended_reply(found(WHIZPAD, IPROTEK), ["r-bbbbbbbb", "r-aaaaaaaa"], True)

    assert reply["text"] == (
        "這些是符合你需求的產品：\n\n"
        "推薦：WhizPad 安心臥智慧床墊（世大智科股份有限公司）\n"
        "- 離床偵測：符合（目錄：「離床提醒」）\n"
        "- 使用場所為居家：符合（目錄：「居家」）\n"
        "- 型態不是穿戴式：符合（目錄：「鋪於床墊下」）\n"
        "產品網頁：https://example.org/whizpad\n"
        "廠商電話：03-1234567\n\n"
        "推薦：iProték 智慧床墊（嘉碩生醫電子股份有限公司）\n"
        "- 離床偵測：符合（目錄：「離床警示」）\n"
        "- 使用場所為居家：目錄未註明\n"
        "- 型態不是穿戴式：符合（目錄：「床墊」）"
    )
    assert reply["action"] == "recommend"
    assert reply["products"][1] == {
        "id": "r-aaaaaaaa",
        "name": "iProték 智慧床墊",
        "company": "嘉碩生醫電子股份有限公司",
        "checks": {"離床偵測": "supported", "places=home": "unknown", "form!=worn": "supported"},
    }


def test_an_english_card_names_the_needs_in_english():
    reply = recommended_reply(found(IPROTEK), ["r-aaaaaaaa"], False, intro="One fits. ")

    assert reply["text"] == (
        "One fits.\n\n"
        "Recommended: iProték 智慧床墊 (嘉碩生醫電子股份有限公司)\n"
        "- Bed-exit detection: met (catalog: 「離床警示」)\n"
        "- Place of use is home: not stated in the catalog\n"
        "- Form is not worn: met (catalog: 「床墊」)"
    )


def test_every_function_and_facet_value_has_its_label():
    assert set(ENGLISH_NAMES) == set(FUNCTION_NAMES) | set(FUNCTION_GROUPS)
    assert {f: set(v) for f, v in VALUE_LABELS.items()} == {
        f: set(v) for f, v in FACET_VALUES.items()
    }
    assert need_label("復健訓練", chinese=False) == "Rehabilitation"
    assert need_label("viewing=computer", chinese=True) == "查看方式為電腦"
    assert need_label("places=home|institution", chinese=True) == "使用場所為居家或長照機構"
    assert need_label("places=home|institution", chinese=False) == (
        "Place of use is home or care home"
    )


# --- When the rounds run out --------------------------------------------------------------


def test_the_fallback_shows_what_the_last_find_judged_to_fit():
    reply = fallback_reply({"found": found(WHIZPAD, IPROTEK)}, chinese=True)

    assert reply["action"] == "recommend"
    assert [p["id"] for p in reply["products"]] == ["r-bbbbbbbb", "r-aaaaaaaa"]  # up to five


def test_the_fallback_names_unconfirmed_what_only_the_agent_could_judge():
    reply = fallback_reply({"found": found(WHIZPAD, reads_text=True)}, chinese=True)

    assert reply["action"] == "unverified" and reply["products"] == []
    assert "- WhizPad 安心臥智慧床墊（世大智科股份有限公司）" in reply["text"]


@pytest.mark.parametrize("evidence", [{}, {"found": found()}])
def test_the_fallback_with_nothing_found_asks_for_more(evidence):
    reply = fallback_reply(evidence, chinese=False)

    assert reply == {
        "text": "I could not confirm a product that fits from the sources. "
        "Tell me a little more about what you need, and I will look again.",
        "action": "unverified",
        "products": [],
    }


def test_the_products_earlier_cards_named_are_found_by_name():
    replies = [
        "符合你的需求。\n\n推薦：WhizPad 安心臥智慧床墊（世大智科股份有限公司）\n- 離床：符合",
        "Two fit.\n\nRecommended: iProték 智慧床墊 (嘉碩生醫電子股份有限公司)\n- 離床預警: met",
        "請問是在家裡用嗎？推薦你先想想。",
    ]

    assert recommended_before(replies) == {"WhizPad 安心臥智慧床墊", "iProték 智慧床墊"}
