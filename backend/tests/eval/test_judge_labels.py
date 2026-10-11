from dataclasses import replace

import pytest
import yaml

from wobot.eval.dataset import Case, Dataset
from wobot.eval.judge import Judged, by_candidate
from wobot.eval.judge_labels import (
    Agreement,
    against_labels,
    agreement,
    any_filled,
    labels_template,
    read_labels,
    reply_sha,
    sample,
)
from wobot.eval.report import judge_markdown


def judged(case_id, run=0, *, expected=True, supported=True, grounded=True, mentions=None):
    def grade(passed):
        return {"reason": f"{case_id} reason", "passed": passed}

    return Judged(
        candidate="model-a:none",
        case_id=case_id,
        run=run,
        reply=f"{case_id} reply {run}",
        criteria={
            "expected": grade(expected),
            "supported": grade(supported),
            "language": grade(True),
        },
        statements=[{"statement": f"{case_id} says", "supported": grounded, "reason": "r"}],
        faithfulness=1.0 if grounded else 0.0,
        grounded=grounded,
        mentions_passed=mentions,
    )


FLAGGED = [
    judged("KN-1", expected=False),
    judged("KN-2", grounded=False),
    judged("KN-3", mentions=False),  # the text check failed what the judge passed
]
PASSED = [judged(f"KN-{n}") for n in range(10, 30)]


def test_samples_every_flagged_reply_then_fills_up_with_passed_ones():
    sampled = sample([*PASSED, *FLAGGED], size=8, supported=3, seed=1)

    keys = [s.judged.case_id for s in sampled]
    assert len(sampled) == 8 and set(keys) >= {"KN-1", "KN-2", "KN-3"}
    asked = {s.judged.case_id for s in sampled if s.asks_supported}
    assert len(asked) == 3 and "KN-2" in asked  # every reply found unsupported
    assert keys[:3] != ["KN-1", "KN-2", "KN-3"]  # shuffled: the order tells nothing
    again = sample([*PASSED, *FLAGGED], size=8, supported=3, seed=1)
    assert [s.judged.case_id for s in again] == keys  # the seed makes it again


def test_leaves_out_replies_the_judge_could_not_judge():
    failed = Judged(candidate="model-a:none", case_id="KN-9", run=0, reply="x", error="boom")

    assert "KN-9" not in {s.judged.case_id for s in sample([failed, *FLAGGED], size=10)}


def case(case_id, history=()):
    return Case(
        case_id=case_id,
        scenario="product_detail",
        split="dev",
        user_input=f"{case_id} message",
        expected_behavior=f"{case_id} expected",
        reference=None,
        check={"kind": "knowledge", "mentions": ["x"]},
        pending=None,
        history=list(history),
    )


EVIDENCE = {
    "passages": [{"id": "k-1", "header": "page", "text": "line one\nline two", "records": []}],
    "records": [],
}
RUN = {
    "meta": {"dataset": "knowledge-v1 abc"},
    "cases": [
        {"case_id": c, "question": "q", "mentions": [], "evidence": EVIDENCE}
        for c in ("KN-1", "KN-2", "KN-3")
    ],
}
DATASET = Dataset(
    "knowledge-v1",
    "",
    "",
    [case("KN-1", [{"user": "earlier", "answer": "said"}]), case("KN-2"), case("KN-3")],
    "abc",
)


@pytest.fixture
def template(tmp_path):
    sampled = sample(FLAGGED, size=3, supported=1, seed=0)
    path = tmp_path / "judge-labels.yaml"
    path.write_text(
        labels_template(sampled, RUN, "x-answer.json", 788, DATASET, "x-judge.json", 0),
        encoding="utf-8",
    )
    return path


def test_the_template_is_blind_and_reads_back_blank(template):
    text = template.read_text(encoding="utf-8")
    data = yaml.safe_load(text)

    assert "reason" not in text and "passed" not in text  # no verdict of the judge
    assert data["run"] == "x-answer.json" and data["index_version"] == 788
    first = next(e for e in data["replies"] if e["case_id"] == "KN-1")
    assert first["history"] == [{"user": "earlier", "answer": "said"}]
    assert first["message"] == "KN-1 message" and first["expected_behavior"] == "KN-1 expected"
    assert first["reply_sha"] == reply_sha("KN-1 reply 0")
    assert list(data["evidence"]) == ["KN-2"]  # only where supported is asked
    assert data["evidence"]["KN-2"]["passages"][0]["text"] == "line one\nline two"
    labels = read_labels(template)
    assert labels.blank == 4  # three expected, one supported
    assert not any_filled(template)


def fill(path, **verdicts):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in data["replies"]:
        for field, value in verdicts.get(entry["case_id"], {}).items():
            entry[field] = value
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_a_label_is_pass_or_fail(template):
    fill(template, **{"KN-1": {"expected": "yes"}})

    with pytest.raises(ValueError, match="expected: write pass or fail, not 'yes'"):
        read_labels(template)


def test_a_file_with_a_label_holds_a_persons_work(template):
    fill(template, **{"KN-3": {"expected": "pass"}})

    assert any_filled(template)


def test_the_judge_is_measured_on_each_label_given(template):
    fill(
        template,
        **{
            "KN-1": {"expected": "fail"},  # the judge failed it too
            "KN-2": {"expected": "pass", "supported": "pass"},  # faithfulness said not
            "KN-3": {"expected": "fail"},  # the judge passed it
        },
    )

    against = against_labels(FLAGGED, read_labels(template))

    by_name = {c.name: c for c in against.comparisons}
    assert by_name["expected"].agreement == Agreement(
        both_pass=1, judge_passes=1, judge_fails=0, both_fail=1
    )
    assert [label.id for _, label in by_name["expected"].unlike] == [
        next(e.id for e in read_labels(template).entries if e.key[1] == "KN-3")
    ]
    assert by_name["supported"].agreement == Agreement(both_pass=1)
    assert by_name["grounded"].agreement == Agreement(judge_fails=1)
    assert against.blank == 0 and against.stale == []


def test_a_label_never_moves_to_another_reply(template):
    changed = [replace(FLAGGED[0], reply="a new reply"), *FLAGGED[1:]]

    against = against_labels(changed, read_labels(template))

    entry = next(e for e in read_labels(template).entries if e.key[1] == "KN-1")
    assert against.stale == [entry.id]


def test_kappa_takes_out_agreement_by_chance():
    a = agreement(
        [(True, True)] * 20 + [(True, False)] * 2 + [(False, True)] * 3 + [(False, False)] * 15
    )

    assert a.labelled == 40 and a.share == pytest.approx(0.875)
    # chance: 0.55 × 0.575 + 0.45 × 0.425 = 0.5075
    assert a.kappa == pytest.approx((0.875 - 0.5075) / (1 - 0.5075))


@pytest.mark.parametrize(
    ("pairs", "share", "kappa"),
    [
        ([], None, None),
        ([(True, True)] * 5, 1.0, None),  # both pass everything: chance is certain
        ([(True, False)] * 5, 0.0, 0.0),  # never alike, and chance would not be either
    ],
    ids=["nothing labelled", "all alike, one class", "never alike"],
)
def test_kappa_where_one_side_never_varies(pairs, share, kappa):
    a = agreement(pairs)

    assert a.share == share and a.kappa == kappa


def test_the_report_shows_agreement_and_each_disagreement(template):
    fill(
        template,
        **{
            "KN-1": {"expected": "fail"},
            "KN-2": {"expected": "pass", "supported": "pass", "note": "a close call"},
            "KN-3": {"expected": "fail"},
        },
    )
    against = against_labels(FLAGGED, read_labels(template))

    text = judge_markdown(by_candidate(FLAGGED), {"judge": "gpt-6.1-sol:low"}, against)

    # Kappa: observed 2/3, chance 2/3 × 1/3 + 1/3 × 2/3 = 4/9, so (2/9) / (5/9).
    assert "| expected | 3 | 67% | 0.40 | 1 | 0 |" in text
    assert "the person fails it; the judge: KN-3 reason." in text
    assert "the person passes it; the judge: KN-2 says. Note: a close call" in text
