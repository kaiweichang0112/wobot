import pytest

from wobot.eval import gold
from wobot.eval.corpus import Corpus, Item

ZWSP = "​"


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def corpus(*items):
    return Corpus(version_id=1, embedding_config_id="c", strategies={}, items=list(items))


def test_match_text_ignores_what_the_page_adds():
    page = f"“Smart care,”  keynote speech, Seoul, 202{ZWSP}5/12/05 PDF"

    assert gold.match_text(page) == gold.match_text(
        "“Smart care,” keynote speech, Seoul, 2025/12/05"
    )


def test_text_files_skip_comments_and_split_products_at_the_tab(tmp_path):
    path = write(tmp_path, "p.txt", "# header\n\n測試床墊 TM-1\t範例科技\n")

    (ref,) = gold.line_refs(path, "product")

    assert ref.value == {"name": "測試床墊 TM-1", "company": "範例科技"}
    assert ref.where == "p.txt:3"


def test_a_product_line_without_a_company_is_an_error(tmp_path):
    with pytest.raises(gold.GoldError, match="tab"):
        gold.line_refs(write(tmp_path, "p.txt", "測試床墊 TM-1 範例科技\n"), "product")


def test_blank_template_entries_are_not_labels(tmp_path):
    students = write(tmp_path, "s.yaml", "2022:\n  - name: ''\n    thesis_title_zh: ''\n")
    fields = write(tmp_path, "f.yaml", "- entry: |-\n    “A,” Forum\n  title: ''\n  event: ''\n")
    named = write(
        tmp_path, "r.yaml", "CASE-1:\n  relevant:\n    - product: {name: '', company: ''}\n"
    )

    assert gold.student_refs(students, "master", [2022]) == []
    assert gold.speech_field_refs(fields) == []
    assert gold.named_refs(named, "CASE-1", "relevant") == []


def test_a_project_date_the_calendar_lacks_expects_no_date(tmp_path):
    path = write(
        tmp_path,
        "p.yaml",
        "- title_zh: '計畫'\n  title_en: ''\n  funder: '科技部'\n"
        "  period_start: '2014-01-20'\n  period_end: '2014-96-30'\n  amount_ntd: '1,000'\n",
    )

    (ref,) = gold.project_refs(path)

    assert ref.expected["period_start"] == "2014-01-20"
    assert ref.expected["period_end"] is None
    assert ref.expected["amount_ntd"] == 1000


def test_a_project_date_may_be_written_as_the_page_does(tmp_path):
    path = write(
        tmp_path,
        "p.yaml",
        "- title_zh: '計畫'\n  title_en: ''\n  funder: '科技部'\n"
        "  period_start: '2024/10/11'\n  period_end: '2025/8/31'\n  amount_ntd: '1'\n",
    )

    (ref,) = gold.project_refs(path)

    assert (ref.expected["period_start"], ref.expected["period_end"]) == (
        "2024-10-11",
        "2025-08-31",
    )


def test_text_that_is_not_a_date_is_an_error_not_an_empty_date(tmp_path):
    path = write(
        tmp_path,
        "p.yaml",
        "- title_zh: '計畫'\n  title_en: ''\n  funder: '科技部'\n"
        "  period_start: '2024年10月'\n  period_end: '2025-08-31'\n  amount_ntd: '1'\n",
    )

    with pytest.raises(gold.GoldError, match="write dates as"):
        gold.project_refs(path)


def test_named_items_must_use_a_known_kind(tmp_path):
    path = write(tmp_path, "r.yaml", "CASE-1:\n  relevant:\n    - paper: 'x'\n")

    with pytest.raises(gold.GoldError, match="unknown kind 'paper'"):
        gold.named_refs(path, "CASE-1", "relevant")


def test_resolves_each_kind_and_reports_what_matches_nothing():
    items = corpus(
        Item("lecture:k:a", "lecture", {"entry_text": "“A,” Forum, 2024/01/02"}),
        Item("product:co:tm-1", "product", {"product_name": "TM-1", "company_name": "Co"}),
        Item(
            "student:master:王",
            "student",
            {"name": "王", "degree": "master", "graduation_year": 2022},
        ),
        Item("section:徐業良:簡介", "section", {"heading": "簡介", "text": []}),
        Item(
            "profile:x",
            "list_item",
            {"list_kind": "profile_item", "category": "Research Interests", "item_text": "x"},
        ),
    )
    refs = [
        gold.Ref("speech", "“A,” Forum, 2024/01/02 PDF", "a"),
        gold.Ref("product", {"name": "tm-1", "company": "CO"}, "b"),
        gold.Ref("student", {"name": "王", "degree": "phd"}, "c"),
        gold.Ref("profile", "research interests", "d"),
    ]

    keys = [r.keys for r in gold.resolve(refs, items)]

    assert keys == [["lecture:k:a"], ["product:co:tm-1"], [], ["profile:x"]]


def test_a_piece_of_text_names_the_one_item_that_holds_it():
    items = corpus(
        Item("lecture:a", "lecture", {"entry_text": "“Smart care,” keynote, Forum A, Seoul"}),
        Item("lecture:b", "lecture", {"entry_text": "“Smart care,” invited, Forum B, Tokyo"}),
        Item(
            "section:p:簡介", "section", {"heading": "簡介", "text": ["他創立中心。他成立公司。"]}
        ),
        Item("project:1", "project", {"title_zh": "照護計畫", "title_en": "Care project"}),
    )
    refs = [
        gold.Ref("speech", "Forum B, Tokyo", "a"),
        gold.Ref("speech", "Smart care", "b"),
        gold.Ref("profile", "他成立公司", "c"),
        gold.Ref("project", "照護計畫 Care project", "d"),
    ]

    resolved = gold.resolve(refs, items)

    assert [r.keys for r in resolved] == [["lecture:b"], [], ["section:p:簡介"], ["project:1"]]
    assert resolved[1].problem == "fits 2 items; copy more of the line"


def section(key, *path, text=()):
    return Item(key, "section", {"heading": path[-1], "path": list(path), "text": list(text)})


def test_a_section_is_named_by_its_heading_by_the_one_above_or_by_its_text():
    items = corpus(
        section("s:home", "範例智科", "首頁", text=["不需插電。"]),
        section("s:pad", "範例智科", "安心臥", "規格", text=["重 2 公斤"]),
        section("s:mat", "範例智科", "運動地墊", "規格", text=["長 30 cm"]),
        section("s:docs", "技術文件", "首頁", text=["SDK 下載"]),
    )
    refs = [
        gold.Ref("section", "安心臥 › 規格", "a"),
        gold.Ref("section", "規格", "b"),
        gold.Ref("section", "範例智科 › 首頁", "c"),
        gold.Ref("section", "長 30", "d"),
    ]

    resolved = gold.resolve(refs, items)

    assert [r.keys for r in resolved] == [["s:pad"], [], ["s:home"], ["s:mat"]]
    assert resolved[1].problem == "heading of 2 sections; add the one above"


# --- Transcriptions ---------------------------------------------------------------------

MEDIA = "https://static.wixstatic.com/media"


def test_a_transcription_names_one_picture_with_its_lines_and_values(tmp_path):
    path = write(
        tmp_path,
        "t.yaml",
        "A:\n  image: ''\n  text: ['']\n  values: [{value: '', unit: ''}]\n"
        "B:\n  document: catalog\n  page: 2\n  text: ['  體壓  分佈 ', '']\n"
        "  values: [{value: 42, unit: mmHg}, {value: '88', unit: ''}]\n"
        "C:\n  document: catalog\n  page: two\n  text: [封面]\n",
    )

    assert gold.transcription_refs(path, "A") == []  # a template not yet filled in
    unlisted = write(tmp_path, "u.yaml", "D:\n  image: x\n  text: [a]\n  values: [{value: ''}]\n")
    none = write(tmp_path, "n.yaml", "D:\n  image: x\n  text: [a]\n  values: []\n")
    assert gold.transcription_refs(unlisted, "D")[0].expected["values"] is None
    assert gold.transcription_refs(none, "D")[0].expected["values"] == []
    [ref] = gold.transcription_refs(path, "B")
    assert (ref.kind, ref.value) == ("document_page", {"document": "catalog", "page": 2})
    assert ref.expected == {"text": ["體壓 分佈"], "values": [("42", "mmHg"), ("88", None)]}
    with pytest.raises(gold.GoldError, match="page is a number"):
        gold.transcription_refs(path, "C")


def test_an_image_is_found_by_the_address_a_browser_copies():
    resized = f"{MEDIA}/ab_1~mv2.png/v1/fill/w_539,h_245,al_c/x.png"
    items = corpus(
        Item("image:a", "image", {"image_url": f"{MEDIA}/ab_1~mv2.png"}),
        Item("document_page:catalog:2", "document_page", {}),
    )

    resolved = gold.resolve(
        [
            gold.Ref("image", resized, "t"),
            gold.Ref("document_page", {"document": "catalog", "page": 2}, "t"),
            gold.Ref("document_page", {"document": "catalog", "page": 9}, "t"),
        ],
        items,
    )

    assert [r.keys for r in resolved] == [["image:a"], ["document_page:catalog:2"], []]
