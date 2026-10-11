import pytest

from wobot.knowledge.facets import (
    FACET_VALUES,
    FACETS,
    FUNCTION_GROUPS,
    FUNCTION_NAMES,
    FUNCTIONS,
    FacetQuery,
    allowed_values,
    judge,
    passes,
    status,
)


def test_each_word_names_one_function():
    words = [word for function in FUNCTIONS for word in (function.name, *function.aliases)]

    assert len(words) == len(set(words))


def test_nearby_functions_and_groups_name_functions_there_are():
    known = set(FUNCTION_NAMES) | set(FUNCTION_GROUPS)

    assert {name for function in FUNCTIONS for name in function.unlike} <= known
    assert {name for group in FUNCTION_GROUPS.values() for name in group} <= set(FUNCTION_NAMES)


def test_every_facet_has_its_values():
    assert FACETS[0] == "functions" and set(FACETS[1:]) == set(FACET_VALUES)
    assert allowed_values("functions") == FUNCTION_NAMES
    assert all(len(set(allowed_values(f))) == len(allowed_values(f)) for f in FACETS)


@pytest.mark.parametrize(
    ("values", "must", "expected"),
    [
        ((), True, "unknown"),  # the text states nothing of the facet
        (("home", "hospital"), True, "supported"),
        (("hospital",), True, "contradicted"),  # it names only another place
        ((), False, "unknown"),
        (("bed",), False, "supported"),  # not worn: its form settles it
        (("worn", "room"), False, "contradicted"),
    ],
)
def test_a_condition_is_judged_by_the_values_the_text_states(values, must, expected):
    value = "worn" if not must else "home"

    assert status(values, value, must=must) == expected


def test_a_product_fits_with_every_function_and_no_condition_contradicted():
    query = FacetQuery.of(
        {"functions": ["離床偵測"], "must": {"places": ["home"]}, "must_not": {"form": ["worn"]}}
    )

    assert passes({"functions": {"離床偵測"}, "form": {"bed"}}, query)  # home unstated
    assert not passes({"functions": {"離床偵測"}, "form": {"worn"}}, query)
    assert not passes({"functions": {"睡眠監測"}, "form": {"bed"}}, query)
    assert passes({"functions": {"下肢復健"}}, FacetQuery.of({"functions": ["復健訓練"]}))


def test_several_values_a_facet_must_have_are_met_by_any_of_them():
    query = FacetQuery.of({"functions": ["離床偵測"], "must": {"places": ["home", "institution"]}})
    either = judge({"functions": {"離床偵測"}, "places": {"home"}}, query)

    assert either.conditions == {"places=home|institution": "supported"} and either.passes
    assert not passes({"functions": {"離床偵測"}, "places": {"hospital"}}, query)
    assert status({"home"}, "home", "institution", must=True) == "supported"
    # Each value it must not have stays a condition of its own.
    unworn = FacetQuery.of({"must_not": {"form": ["worn", "robot"]}})
    assert judge({"form": {"bed"}}, unworn).conditions == {
        "form!=worn": "supported",
        "form!=robot": "supported",
    }


def test_a_judgement_names_each_need_and_counts_what_the_catalog_states():
    query = FacetQuery.of(
        {
            "functions": ["離床偵測", "翻身監測"],
            "must": {"places": ["home"], "viewing": ["computer"]},
            "must_not": {"form": ["worn"]},
        }
    )

    judgement = judge({"functions": {"離床偵測"}, "form": {"bed"}, "places": {"home"}}, query)

    assert judgement.functions == {"離床偵測": True, "翻身監測": False}
    assert judgement.conditions == {
        "places=home": "supported",
        "viewing=computer": "unknown",
        "form!=worn": "supported",
    }
    assert (judgement.passes, judgement.supported) == (False, 2)


@pytest.mark.parametrize(
    ("query", "problem"),
    [
        ({"functions": ["離床感應"]}, "not functions"),
        ({"must": {"colour": ["red"]}}, "no facet 'colour'"),
        ({"must_not": {"form": ["wearable"]}}, "not form values"),
        ({"should": {}}, "no query part should"),
    ],
)
def test_a_query_outside_the_vocabulary_is_an_error(query, problem):
    with pytest.raises(ValueError, match=problem):
        FacetQuery.of(query)
