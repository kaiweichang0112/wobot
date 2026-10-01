from wobot.eval.metrics import FieldScore
from wobot.eval.report import summary
from wobot.eval.runner import CaseResult, RunResult


def case(case_id, fields):
    return CaseResult("d", case_id, "dev", "list", "scored", fields=fields)


def test_field_accuracy_pools_only_the_cases_that_compare_the_field():
    projects = FieldScore(compared=6, correct={"amount_ntd": 6}, present={"amount_ntd": 6})
    students = FieldScore(
        compared=3, correct={"thesis_title_zh": 2}, present={"thesis_title_zh": 3}
    )
    run = RunResult(1, 5, [case("A", projects), case("B", students)], {})

    group = summary(run)["list/dev"]

    assert group["field_accuracy"] == {"amount_ntd": 1.0, "thesis_title_zh": 2 / 3}
    assert group["field_presence"] == {"amount_ntd": 1.0, "thesis_title_zh": 1.0}
