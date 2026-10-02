"""Comparing vision models on transcribed pictures, with fake models: nothing is paid for."""

from tests.knowledge.fakes import FakeVisionReader, MemoryAnswerCache
from wobot.eval.dataset import Case, Dataset
from wobot.eval.gold import Ref
from wobot.eval.vision import Fixture, compare_models, dev_transcriptions
from wobot.knowledge.extraction import Question
from wobot.knowledge.vision import VISUAL_CONTENT, VisualInput


def fixture(case_id, content, text, values=()):
    ref = Ref(
        "image",
        f"https://example.test/{case_id}.png",
        case_id,
        {"text": text, "values": list(values)},
    )
    return Fixture(case_id, ref, VisualInput(case_id, content))


def reads(answer):
    """A fake model answering `answer(picture)` as its transcription."""
    reader = FakeVisionReader(
        lambda item: {
            "contains_information": True,
            "verbatim_text": answer(item),
            "values": [{"label": "", "value": "42", "unit": "mmHg"}],
            "relationships": [],
            "description": "",
            "unreadable": [],
        }
    )
    return reader


async def test_each_model_reads_the_same_pictures_and_is_scored_alike():
    fixtures = [
        fixture("V1", b"cover", ["WhizToys", "運動地墊遊戲平台"]),
        fixture("V2", b"chart", ["體壓分佈測定"], [("42", "mmHg"), ("36", "mmHg")]),
    ]
    good, poor = (
        reads(lambda item: ["WhizToys", "運動地墊遊戲平台", "體壓分佈測定"]),
        reads(lambda item: []),
    )
    poor.question = Question(VISUAL_CONTENT, "poor-vision", 1)
    cache = MemoryAnswerCache()

    first = await compare_models([good, poor], fixtures, cache)
    again = await compare_models([good], fixtures, cache)

    assert [r.model for r in first] == ["fake-vision", "poor-vision"]
    assert (first[0].mean("text_recall"), first[1].mean("text_recall")) == (1.0, 0.0)
    assert first[0].scores["V2"].value_recall == 0.5
    assert (first[0].input_tokens, first[0].calls) == (1600, 2)
    # Kept answers are not paid for again, but still count toward what reading costs.
    assert (again[0].calls, again[0].input_tokens) == (0, 1600)


def test_only_dev_cases_choose_the_model(tmp_path):
    (tmp_path / "t.yaml").write_text(
        "VIS-01:\n  image: https://example.test/a.png\n  text: [封面]\n"
        "CASE-015:\n  image: https://example.test/b.png\n  text: [規格]\n",
        encoding="utf-8",
    )
    check = {"kind": "transcription", "gold": {"file": "t.yaml", "as": "transcription"}}
    cases = [
        Case(case_id, "test", split, "問題", "", None, check, None)
        for case_id, split in (("VIS-01", "dev"), ("CASE-015", "heldout"))
    ]

    labelled = dev_transcriptions([Dataset("t", "", "", cases, "x")], tmp_path)

    assert [case_id for case_id, _ in labelled] == ["VIS-01"]
