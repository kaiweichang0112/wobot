import pytest
from pydantic import ValidationError

from tests.agent.test_retrieval import Unreachable
from wobot.agent import find as finding
from wobot.agent.find import (
    CatalogProduct,
    FindProducts,
    Gap,
    ProductCatalogs,
    Split,
    find,
    find_products,
    product_catalog,
    similarities,
    stated,
)
from wobot.agent.retrieval import LookupFailed
from wobot.eval import gold
from wobot.eval.dataset import load_dataset
from wobot.eval.facets import need_checks
from wobot.knowledge.facets import FacetQuery
from wobot.knowledge.records.text import key_text


def product(name: str, company: str = "Co", **facets: dict[str, str]) -> CatalogProduct:
    return CatalogProduct(
        f"r-{name}", f"product:{name}", {"product_name": name, "company_name": company}, facets
    )


def bed_exit(name: str, company: str = "Co", **facets: dict[str, str]) -> CatalogProduct:
    return product(name, company, functions={"離床偵測": "離床提醒"}, **facets)


BED_EXIT = FacetQuery(("離床偵測",))


# --- On the labelled products -------------------------------------------------------------


def gold_catalog() -> list[CatalogProduct]:
    path = gold.GOLD_DIR / "product-facets.yaml"
    return [
        product(ref.value["name"], ref.value["company"], **ref.expected["facets"])
        for split in ("dev", "heldout")
        for ref in gold.product_facet_refs(path, split)
    ]


def test_each_recommendation_need_finds_what_a_person_said_fits_preferred_first():
    """recommendation-v2's needs on the labelled products, as DEC-069 ranks them: every
    product a person said fits is found, none said never to show, and the one the case
    prefers comes first unless it was shown already."""
    catalog = gold_catalog()
    names = {key_text(p.name) for p in catalog}
    dataset = load_dataset("recommendation-v2")
    prefers = {c.case_id: c.check.get("prefers") for c in dataset.cases if c.check}

    for check in need_checks([dataset]):
        found = find(catalog, check.query, 1, {}, shown=check.shown)

        fitting = [key_text(r.product.name) for r in found.fitting]
        assert check.fits | check.never <= names, check.case_id
        assert check.fits - check.shown <= set(fitting), check.case_id
        assert not check.never & set(fitting), check.case_id
        preferred = prefers[check.case_id]
        if preferred and key_text(preferred) not in check.shown:
            assert fitting[0] == key_text(preferred), check.case_id
        assert not check.shown & set(fitting), check.case_id


# --- Ranking, splits and gaps -------------------------------------------------------------


def test_the_preferred_maker_comes_first_then_what_states_more_then_meaning():
    query = FacetQuery(("離床偵測",), {"places": ("home",)})
    catalog = [
        bed_exit("unstated"),
        bed_exit("home", places={"home": "居家"}),
        bed_exit("close"),
        bed_exit("preferred", "世大智科股份有限公司"),
    ]

    found = find(catalog, query, "all", {"r-close": 0.9, "r-unstated": 0.2, "r-home": 0.1})

    assert [r.product.name for r in found.fitting] == ["preferred", "home", "close", "unstated"]
    assert found.decisive == [] and found.gaps == []


def test_products_shown_earlier_are_left_out_and_counted():
    catalog = [bed_exit("A"), bed_exit("B"), product("C")]

    found = find(catalog, BED_EXIT, 1, {}, shown=["A", "C"])

    assert [r.product.name for r in found.fitting] == ["B"]
    assert found.recommended_before == 1  # C does not fit


def test_a_facet_the_user_left_open_that_splits_the_fitting_products_is_decisive():
    catalog = [
        bed_exit("A", places={"home": "居家"}, power={"mains": "插電"}),
        bed_exit("B", places={"home": "居家"}, power={"mains": "插電"}),
        bed_exit("C", places={"institution": "長照機構"}),
        bed_exit("D", places={"home": "居家", "institution": "機構"}),
        bed_exit("E", viewing={"app": "App", "computer": "電腦"}),
    ]

    found = find(catalog, BED_EXIT, 1, {})

    # power is stated alike by all that state it, and viewing by one product alone.
    assert found.decisive == [Split("places", {"home": 3, "institution": 2}, 1)]
    assert find(catalog, BED_EXIT, 5, {}).decisive == []  # all can be shown
    assert find(catalog, BED_EXIT, "all", {}).decisive == []
    settled = FacetQuery(("離床偵測",), {"places": ("home",)})
    assert find(catalog, settled, 1, {}).decisive == []  # the user said where


def test_when_nothing_fits_each_need_says_how_many_it_leaves():
    query = FacetQuery(("離床偵測", "翻身監測"), {"places": ("home",)}, {"form": ("worn",)})
    catalog = [
        bed_exit("A", places={"institution": "長照機構"}),
        bed_exit("B", places={"home": "居家"}, form={"worn": "手環"}),
        product("C", functions={"翻身監測": "翻身提醒"}),
    ]

    found = find(catalog, query, 1, {})

    assert found.fitting == []
    assert found.gaps == [
        Gap("離床偵測", 2),
        Gap("翻身監測", 1),
        Gap("places=home", 0),  # nothing does both functions either way
        Gap("form!=worn", 0),
    ]
    one = FacetQuery(("離床偵測",), {"places": ("home",)}, {"form": ("worn",)})
    assert find(catalog, one, 1, {}).gaps == [
        Gap("離床偵測", 2),
        Gap("places=home", 1),  # A, whose form is unstated
        Gap("form!=worn", 1),  # B
    ]


def test_the_agent_reads_each_need_in_the_catalog_s_words():
    query = FacetQuery(
        ("復健訓練",), {"places": ("home",), "viewing": ("app",)}, {"form": ("worn",)}
    )
    item = product(
        "P",
        functions={"下肢復健": "行走訓練"},
        places={"home": "居家"},
        form={"equipment": "訓練機"},
    )

    assert stated(item, query) == {
        "復健訓練": "行走訓練",
        "places=home": "居家",
        "viewing=app": "unknown",
        "form!=worn": "訓練機",  # what it is instead
    }
    assert stated(product("Q", form={"worn": "手環"}), query)["form!=worn"] == "contradicted"


# --- The tool's arguments -----------------------------------------------------------------


def test_the_arguments_become_a_query_and_a_text_without_the_conditions():
    args = FindProducts.model_validate(
        {
            "functions": ["離床偵測", "離床偵測"],
            "must": {"places": ["home", "home"]},
            "must_not": {"form": ["worn"]},
            "other": [" 安靜 ", ""],
        }
    )

    assert args.query == FacetQuery(("離床偵測",), {"places": ("home",)}, {"form": ("worn",)})
    assert args.text == "離床偵測、安靜"
    assert args.count == 1


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({}, "name a function"),
        ({"other": ["  "]}, "name a function"),
        ({"functions": ["離床感應"]}, "functions.0"),
        ({"functions": ["離床偵測"], "must": {"colour": ["red"]}}, "colour"),
        (
            {"functions": ["離床偵測"], "must": {"form": ["bed"]}, "must_not": {"form": ["bed"]}},
            "both required and excluded: form bed",
        ),
        ({"functions": ["離床偵測"], "count": 0}, "count"),
    ],
)
def test_arguments_outside_the_vocabulary_are_refused(arguments, problem):
    with pytest.raises(ValidationError, match=problem):
        FindProducts.model_validate(arguments)


# --- Against the database -----------------------------------------------------------------


async def test_a_version_s_products_come_with_their_facets_and_card_fields(knowledge):
    catalog = await product_catalog(knowledge.db, knowledge.version_id)

    by_name = {p.name: p for p in catalog}
    assert set(by_name) == {"測試床墊 TM-1", "測試地墊 TM-2", "測試手環 TM-3"}
    bed = by_name["測試床墊 TM-1"]
    assert bed.handle.startswith("r-") and bed.key.startswith("product:")
    assert bed.fields["contact_phone"] == "02-0000-0000"
    assert bed.facets == {"functions": {"離床偵測": "離床偵測"}, "form": {"bed": "床墊下"}}


async def test_products_are_ranked_by_meaning_over_the_whole_catalog(knowledge):
    catalogs = ProductCatalogs(knowledge.db)
    args = FindProducts(functions=["離床偵測"])

    found = await find_products(
        catalogs, knowledge.db, knowledge.embedder, knowledge.version_id, args
    )
    close = await similarities(
        knowledge.db, knowledge.embedder, knowledge.version_id, "離床偵測", 3
    )

    assert [r.product.name for r in found.fitting] == ["測試床墊 TM-1"]
    assert knowledge.embedder.calls[-1] == ["離床偵測"]
    assert {p.handle for p in await catalogs.get(knowledge.version_id)} == set(close)


async def test_a_version_is_read_once_and_only_the_latest_are_kept(monkeypatch):
    reads = []

    async def read(db, version_id):
        reads.append(version_id)
        return [product(str(version_id))]

    monkeypatch.setattr(finding, "product_catalog", read)
    catalogs = ProductCatalogs(Unreachable())

    for version_id in (1, 1, 2, 3, 1):
        await catalogs.get(version_id)

    assert reads == [1, 2, 3, 1]  # 1 was let go when 3 was read


async def test_an_unreachable_database_is_a_failure_not_an_empty_catalog(knowledge):
    with pytest.raises(LookupFailed):
        await product_catalog(Unreachable(), 1)
    with pytest.raises(LookupFailed):
        await similarities(Unreachable(), knowledge.embedder, 1, "離床偵測", 3)
