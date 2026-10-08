import pytest

from wobot.agent.rec_agent import Need, NeedCheck, Pick, RecommendProducts
from wobot.agent.recommend import (
    MOST_PRODUCTS,
    fallback_reply,
    recommended_before,
    recommended_reply,
    refusal,
    review,
)

NEEDS = [Need(text="離床預警", kind="function"), Need(text="居家使用", kind="constraint")]


def product(handle: str, name: str, company: str, rank: int, **fields) -> tuple[str, dict]:
    return handle, {
        "kind": "product",
        "key": f"product:{name}",
        "rank": rank,
        "fields": {"product_name": name, "company_name": company} | fields,
        "source": "catalog.xlsx",
    }


EVIDENCE = dict(
    [
        product("r-aaaaaaaa", "iProték 智慧床墊", "嘉碩生醫電子股份有限公司", 1),
        product(
            "r-bbbbbbbb",
            "WhizPad 安心臥智慧床墊",
            "世大智科股份有限公司",
            4,
            product_url="https://example.org/whizpad",
            contact_phone="03-1234567",
        ),
        product("r-cccccccc", "Monibed 紅外線離床警示器", "某公司", 2),
    ]
)


def pick(handle: str, *statuses: str, reason: str = "床墊下感測，離床即通知。") -> Pick:
    checks = [NeedCheck(need=n, status=s) for n, s in enumerate(statuses, start=1)]
    return Pick(product_id=handle, checks=checks, reason=reason)


def recommend(*picks: Pick, count=1, needs=NEEDS) -> RecommendProducts:
    return RecommendProducts(
        needs=needs, products=list(picks), count=count, intro="這項產品符合你的需求。"
    )


def test_every_function_must_be_supported_and_no_constraint_contradicted():
    found = review(
        recommend(
            pick("r-aaaaaaaa", "unknown", "supported"),
            pick("r-cccccccc", "supported", "contradicted"),
        ),
        EVIDENCE,
    )

    assert found.shown == []
    assert found.problems == [
        "r-aaaaaaaa: function 1 (離床預警) is unknown",
        "r-cccccccc: constraint 2 (居家使用) is contradicted",
    ]


def test_a_constraint_the_catalog_does_not_state_passes():
    found = review(recommend(pick("r-aaaaaaaa", "supported", "unknown")), EVIDENCE)

    assert [p.product_id for p in found.shown] == ["r-aaaaaaaa"] and found.problems == []


@pytest.mark.parametrize(
    ("given", "problem"),
    [
        (pick("r-dddddddd", "supported", "supported"), "r-dddddddd was not found by a search"),
        (pick("r-aaaaaaaa", "supported"), "need 2 (居家使用) is not judged"),
    ],
)
def test_a_product_not_found_or_not_judged_on_every_need_is_refused(given, problem):
    found = review(recommend(given), EVIDENCE)

    assert found.shown == [] and problem in found.problems[0]


def test_a_failing_product_is_left_out_and_the_rest_shown():
    found = review(
        recommend(
            pick("r-cccccccc", "supported", "contradicted"),
            pick("r-aaaaaaaa", "supported", "supported"),
        ),
        EVIDENCE,
    )

    assert [p.product_id for p in found.shown] == ["r-aaaaaaaa"]


def test_the_preferred_company_comes_first_then_the_best_ranked():
    fit = [pick(h, "supported", "supported") for h in ("r-aaaaaaaa", "r-bbbbbbbb", "r-cccccccc")]

    one = review(recommend(*fit), EVIDENCE)
    every = review(recommend(*fit, count="all"), EVIDENCE)

    assert [p.product_id for p in one.shown] == ["r-bbbbbbbb"]  # 世大智科, though ranked 4th
    assert [p.product_id for p in every.shown] == ["r-bbbbbbbb", "r-aaaaaaaa", "r-cccccccc"]


def test_all_stops_at_the_most_products():
    evidence = dict(product(f"r-{n:08x}", f"床墊 {n}", "某公司", n) for n in range(1, 8))
    picks = [pick(handle, "supported", "supported") for handle in evidence]

    found = review(recommend(*picks, count="all"), evidence)

    assert len(found.shown) == MOST_PRODUCTS


def test_a_card_shows_each_need_and_how_to_reach_the_maker():
    args = recommend(pick("r-bbbbbbbb", "supported", "unknown"))

    reply = recommended_reply(args, review(args, EVIDENCE).shown, EVIDENCE, chinese=True)

    assert reply["text"] == (
        "這項產品符合你的需求。\n\n"
        "推薦：WhizPad 安心臥智慧床墊（世大智科股份有限公司）\n"
        "床墊下感測，離床即通知。\n"
        "- 離床預警：符合\n"
        "- 居家使用：目錄未註明\n"
        "產品網頁：https://example.org/whizpad\n"
        "廠商電話：03-1234567"
    )
    assert reply["action"] == "recommend"
    assert reply["products"] == [
        {
            "id": "r-bbbbbbbb",
            "name": "WhizPad 安心臥智慧床墊",
            "company": "世大智科股份有限公司",
            "checks": {"離床預警": "supported", "居家使用": "unknown"},
        }
    ]


def test_an_english_card_leaves_out_what_the_catalog_does_not_give():
    args = recommend(pick("r-aaaaaaaa", "supported", "supported", reason=""))

    reply = recommended_reply(args, review(args, EVIDENCE).shown, EVIDENCE, chinese=False)

    assert reply["text"].endswith(
        "Recommended: iProték 智慧床墊 (嘉碩生醫電子股份有限公司)\n- 離床預警: met\n- 居家使用: met"
    )


def test_the_fallback_names_what_was_found_and_asks_for_more():
    reply = fallback_reply(EVIDENCE, chinese=True)

    assert reply["action"] == "unverified" and reply["products"] == []
    lines = reply["text"].splitlines()
    assert lines[2:5] == [
        "- WhizPad 安心臥智慧床墊（世大智科股份有限公司）",
        "- iProték 智慧床墊（嘉碩生醫電子股份有限公司）",
        "- Monibed 紅外線離床警示器（某公司）",
    ]
    assert lines[-1] == "可以再多告訴我一些你的需求，我再幫你找找看。"


def test_the_fallback_with_nothing_found_asks_for_more():
    assert fallback_reply({}, chinese=False)["text"] == (
        "I could not confirm a product that fits from the sources. "
        "Tell me a little more about what you need, and I will look again."
    )


def test_a_refusal_tells_the_agent_what_to_do_next():
    text = refusal(["r-aaaaaaaa: function 1 (離床預警) is unknown"])

    assert text.startswith("Not shown: no product meets every need. r-aaaaaaaa:")
    assert text.endswith("say which need the catalog does not state.")


def test_the_products_earlier_cards_named_are_found_by_name():
    replies = [
        "符合你的需求。\n\n推薦：WhizPad 安心臥智慧床墊（世大智科股份有限公司）\n- 離床：符合",
        "Two fit.\n\nRecommended: iProték 智慧床墊 (嘉碩生醫電子股份有限公司)\n- 離床預警: met",
        "請問是在家裡用嗎？推薦你先想想。",
    ]

    assert recommended_before(replies) == {"WhizPad 安心臥智慧床墊", "iProték 智慧床墊"}


def test_a_product_recommended_earlier_is_not_shown_again():
    args = recommend(
        pick("r-bbbbbbbb", "supported", "supported"), pick("r-aaaaaaaa", "supported", "supported")
    )

    again = review(args, EVIDENCE, frozenset({"WhizPad 安心臥智慧床墊"}))
    only = review(
        recommend(pick("r-bbbbbbbb", "supported", "supported")),
        EVIDENCE,
        frozenset({"WhizPad 安心臥智慧床墊"}),
    )

    assert [p.product_id for p in again.shown] == ["r-aaaaaaaa"]
    assert only.problems == ["r-bbbbbbbb was recommended earlier in this conversation"]
