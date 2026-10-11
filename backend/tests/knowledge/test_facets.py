from wobot.knowledge.facets import (
    FACET_VALUES,
    FACETS,
    FUNCTION_GROUPS,
    FUNCTION_NAMES,
    FUNCTIONS,
    allowed_values,
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
