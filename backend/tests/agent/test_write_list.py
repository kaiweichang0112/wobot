import hashlib
import json

import pytest

from wobot.agent import write_list
from wobot.agent.write_list import (
    REMEMBERED_ITEMS,
    ListIntro,
    ShownList,
    heading,
    list_reply,
    lists_that_count,
    write_list_prompt,
)


def student(n: int, name: str, year: int = 2022, **fields) -> dict:
    return {
        "id": f"r-{n:08x}",
        "key": f"student:master:{name}",
        "source": "https://example.org/students",
        "fields": {
            "name": name,
            "degree": "master",
            "graduation_year": year,
            "thesis_title_zh": "智慧床墊",
            "thesis_title_en": "A Smart Mattress",
            "fulltext_url": None,
        }
        | fields,
    }


def found(result_id: str, items: list, uncertain=(), window=None, year=None) -> dict:
    years = {} if year is None else {"year_from": year, "year_to": year}
    return {
        "result_id": result_id,
        "tool": "list_students",
        "kind": "student",
        "filters": {"degree": "master"} | years,
        "window": window,
        "items": items,
        "uncertain": list(uncertain),
    }


RESULTS = {
    "q-2021": found("q-2021", [student(1, "張宗聖", 2021)], year=2021),
    "q-2022": found(
        "q-2022",
        [student(2, "王小明", fulltext_url="https://hdl.handle.net/11296/a"), student(3, "李大華")],
        year=2022,
    ),
}


def intro(text: str, *lists: tuple[str, list[str] | None]) -> ListIntro:
    return ListIntro(intro=text, lists=[ShownList(result_id=r, item_ids=i) for r, i in lists])


def test_the_prompt_version_moves_with_the_prompt():
    prompt = json.dumps([write_list.INSTRUCTIONS, ListIntro.model_json_schema()])
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    assert (write_list.PROMPT_VERSION, fingerprint) == (2, "6dd3c3ea301f")


def test_the_writer_reads_the_message_and_each_lists_count_not_the_loop():
    _, human = write_list_prompt("列出 2021、2022 年的碩士畢業生", RESULTS)
    shown = json.loads(human.content)

    assert shown["message"] == "列出 2021、2022 年的碩士畢業生"
    assert [(r["result_id"], r["count"]) for r in shown["lists"]] == [("q-2021", 1), ("q-2022", 2)]


def test_code_writes_every_item_after_the_models_words():
    reply, _ = list_reply(intro("2022 年共有 2 位碩士畢業生。", ("q-2022", None)), RESULTS, True)

    assert reply["text"] == (
        "2022 年共有 2 位碩士畢業生。\n\n"
        "1. 2022　碩士　王小明：《智慧床墊》（A Smart Mattress），全文：https://hdl.handle.net/11296/a\n"
        "2. 2022　碩士　李大華：《智慧床墊》（A Smart Mattress）"
    )
    assert reply["lists"] == [
        {
            "result_id": "q-2022",
            "kind": "student",
            "keys": ["student:master:王小明", "student:master:李大華"],
            "uncertain_keys": [],
        }
    ]


def test_an_english_question_gets_english_labels():
    reply, _ = list_reply(intro("Two graduated in 2022.", ("q-2022", None)), RESULTS, False)

    assert '1. 2022　Master\'s　王小明: "A Smart Mattress" (智慧床墊), Full text: ' in reply["text"]


def test_lists_asked_apart_are_shown_in_the_order_chosen_each_under_a_heading():
    chosen = intro("各年人數如下。", ("q-2022", None), ("q-2021", None), ("q-2022", None))

    reply, _ = list_reply(chosen, RESULTS, True)

    assert [shown["result_id"] for shown in reply["lists"]] == ["q-2022", "q-2021"]
    assert "\n\n2022 年・碩士畢業生（2 筆）\n1. 2022　碩士　王小明" in reply["text"]
    assert "\n\n2021 年・碩士畢業生（1 筆）\n1. 2021　碩士　張宗聖" in reply["text"]


def test_one_list_alone_has_no_heading():
    reply, _ = list_reply(intro("共 2 位。", ("q-2022", None)), RESULTS, True)

    assert "畢業生（" not in reply["text"]


@pytest.mark.parametrize(
    ("kind", "filters", "lang", "expected"),
    [
        (
            "student",
            {"degree": "phd", "year_from": 2007, "year_to": 2010},
            "zh",
            "2007–2010 年・博士畢業生（0 筆）",
        ),
        (
            "student",
            {"degree": "master", "year_from": 2021, "year_to": 2021},
            "en",
            "2021 · Master's graduates (0)",
        ),
        ("lecture", {"last_years": 5, "category": "keynote"}, "zh", "近 5 年・主題演講（0 筆）"),
        (
            "publication",
            {"category": "Patents", "year_from": 2020},
            "zh",
            "2020 年起・專利（0 筆）",
        ),
        ("product", {"category": "1-3"}, "en", "1-3 臥床監測、離床預警、壓傷防護 (0)"),
        ("project", {"contains": "長照"}, "zh", "研究計畫・「長照」（0 筆）"),
    ],
)
def test_a_heading_names_the_list_by_its_filters(kind, filters, lang, expected):
    shown = {"kind": kind, "filters": filters, "items": []}

    assert heading(shown, lang) == expected


def test_the_model_may_narrow_a_list_but_not_add_to_it():
    narrowed, _ = list_reply(
        intro("一位。", ("q-2022", ["r-00000003", "r-99999999"])), RESULTS, True
    )
    invented, _ = list_reply(intro("一位。", ("q-2022", ["r-99999999"])), RESULTS, True)
    unknown, _ = list_reply(intro("沒有。", ("q-1999", None)), RESULTS, True)

    assert narrowed["lists"][0]["keys"] == ["student:master:李大華"]
    assert len(invented["lists"][0]["keys"]) == 2  # no ID held: the whole list, not none
    assert unknown == {"text": "沒有。", "lists": []}


def test_items_the_window_cannot_place_are_shown_apart():
    window = {"from": "2021-10-06", "to": "2026-10-06"}
    results = {"q-5": found("q-5", [student(1, "王小明")], [student(2, "張宗聖", 2021)], window)}

    reply, _ = list_reply(intro("近五年。", ("q-5", None)), results, True)

    assert reply["text"].endswith(
        "以下 1 筆的來源只記載年份或沒有日期，無法確定是否在 2021-10-06 至 2026-10-06 之間：\n"
        "- 2021　碩士　張宗聖：《智慧床墊》（A Smart Mattress）"
    )
    assert reply["lists"][0]["uncertain_keys"] == ["student:master:張宗聖"]


def test_the_conversation_keeps_only_the_first_items():
    many = [student(n, f"學生{n}") for n in range(REMEMBERED_ITEMS + 3)]
    results = {"q-all": found("q-all", many)}

    reply, remembered = list_reply(intro("共 23 位。", ("q-all", None)), results, True)

    assert len(reply["lists"][0]["keys"]) == REMEMBERED_ITEMS + 3
    assert f"{REMEMBERED_ITEMS}. " in remembered and f"{REMEMBERED_ITEMS + 1}. " not in remembered
    assert remembered.endswith("（另有 3 筆）")


def test_a_lecture_names_its_place_and_links_its_pdf():
    talk = {
        "id": "r-00000001",
        "key": "lecture:2024:1",
        "source": "https://example.org/talks",
        "fields": {
            "date_raw": "2024/05/03",
            "lecture_date": "2024-05-03",
            "year": 2024,
            "title": "智慧照顧",
            "entry_text": "智慧照顧，年會，台北",
            "event": "年會",
            "location": "台北",
            "pdf_url": "https://example.org/a.pdf",
        },
    }
    results = {"q-t": found("q-t", [talk]) | {"kind": "lecture"}}

    reply, _ = list_reply(intro("一場。", ("q-t", None)), results, True)

    assert (
        "1. 2024/05/03　智慧照顧，年會，地點：台北，PDF：https://example.org/a.pdf" in reply["text"]
    )


def test_the_writer_reads_the_lists_that_found_something_or_all_when_none_did():
    empty = found("q-0", [])

    assert list(lists_that_count({"q-0": empty, **RESULTS})) == ["q-2021", "q-2022"]
    assert list(lists_that_count({"q-0": empty})) == ["q-0"]
