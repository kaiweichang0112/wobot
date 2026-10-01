from datetime import date

from tests.knowledge.wix_pages import BLANK, a, br_p, h, link_p, ol, p, page, rich
from wobot.knowledge.records import grc
from wobot.knowledge.sources.wix import page_blocks


def blocks_of(*elements):
    return [b for b in page_blocks(page(*elements).decode()) if b.element_id != "comp-footer"]


def student(name, zh=None, en=None, link="https://hdl.handle.net/11296/x"):
    lines = [p(name)]
    lines += [p(f"論文名稱：{zh}")] if zh else []
    lines += [p(f"Thesis title: {en}")] if en else []
    lines += [link_p("電子全文", link)] if link != "none" else []
    return "".join(lines)


def students_page(*years):
    """years: (year, item id, [student markup, ...]) in page order."""
    elements = []
    for year, item, students in years:
        elements.append(rich(f"comp-year__{item}", p(str(year))))
        elements.append(rich(f"comp-list__{item}", BLANK.join(students)))
    return blocks_of(*elements)


# --- Students ---------------------------------------------------------------------------


def test_reads_each_graduate_of_each_year():
    blocks = students_page(
        (
            2022,
            "item1",
            [student("王小明", "智慧床墊", "Smart mattress"), student("李小華", "地墊")],
        ),
        (2021, "item-k2", [student("陳大同", en="Wheelchair seat")]),
    )

    parsed = grc.parse_students(blocks, "master")

    assert parsed.problems == []
    assert [(d.fields["name"], d.fields["graduation_year"]) for d in parsed.drafts] == [
        ("王小明", 2022),
        ("李小華", 2022),
        ("陳大同", 2021),
    ]
    first = parsed.drafts[0]
    assert first.logical_key == "student:master:王小明"
    assert first.fields["thesis_title_en"] == "Smart mattress"
    assert first.fields["fulltext_url"] == "https://hdl.handle.net/11296/x"
    assert first.locator == {"repeater_item": "item1", "position": 1}


def test_reads_a_graduate_written_as_one_paragraph_with_line_breaks():
    entry = br_p("丁小音", "論文名稱：感知坐墊", "Thesis title: Smart seat", "電子全文")
    parsed = grc.parse_students(students_page((2018, "item-a", [entry])), "master")

    assert parsed.drafts[0].fields["thesis_title_zh"] == "感知坐墊"


def test_a_link_that_leads_nowhere_is_kept_as_missing_and_reported():
    parsed = grc.parse_students(
        students_page((2014, "i", [student("劉某", "床墊", link=None)])), "phd"
    )

    assert parsed.drafts[0].fields["fulltext_url"] is None
    assert parsed.drafts[0].warnings == (
        "phd 2014 #1: full-text link leads nowhere on the source page",
    )


def test_a_graduate_without_any_title_blocks():
    parsed = grc.parse_students(
        students_page((2020, "i", [student("無題", link="none")])), "master"
    )

    assert parsed.problems and "needs a name and a thesis title" in parsed.problems[0]


def test_a_year_without_graduates_blocks():
    blocks = blocks_of(rich("comp-year__i", p("2019")))

    assert grc.parse_students(blocks, "master").problems == ["master 2019: a year without students"]


# --- Projects ---------------------------------------------------------------------------


def projects_page(*sections):
    elements = []
    for index, (year, body) in enumerate(sections):
        elements.append(rich(f"comp-h{index}", p(f"{year}\xa0Research projects")))
        elements.append(rich(f"comp-b{index}", body))
    return blocks_of(*elements)


FUNDER = "國科會/範例公司 National Science Council/Example Co. Ltd."


def test_reads_period_amount_and_both_funder_names_exactly():
    blocks = projects_page(
        (
            2022,
            p("智慧照護計畫")
            + p("Smart care project")
            + p(f"{FUNDER}\xa0 \xa0 2022/12/08~2024/04/30\xa0 N", "TD3,600,000"),
        )
    )

    [project] = grc.parse_projects(blocks).drafts

    assert project.fields == {
        "title_zh": "智慧照護計畫",
        "title_en": "Smart care project",
        "funder_raw": FUNDER,
        "funder_zh": "國科會/範例公司",
        "funder_en": "National Science Council/Example Co. Ltd.",
        "period_raw": "2022/12/08~2024/04/30",
        "period_start": date(2022, 12, 8),
        "period_end": date(2024, 4, 30),
        "amount_ntd": 3_600_000,
        "amount_raw": "NTD3,600,000",
        "year": 2022,
    }


def test_a_funder_line_ends_a_project_even_without_a_blank_line():
    body = br_p(
        "第一案",
        "First",
        "甲公司 A Co. 2013/05/01~2014/07/31 NTD150,000",
        "第二案",
        "Second",
        "乙公司 B Inc. 2013/03/01~2013/12/31 NTD305,500",
    )

    drafts = grc.parse_projects(projects_page((2013, body))).drafts

    assert [(d.fields["title_zh"], d.fields["amount_ntd"]) for d in drafts] == [
        ("第一案", 150_000),
        ("第二案", 305_500),
    ]


def test_titles_go_by_position_even_when_the_english_one_holds_a_chinese_name():
    body = br_p(
        "延攬人才：鄭某",
        "Postdoctoral Research: 鄭某",
        "國科會 NSC 2009/01/01~2009/11/30 NTD696,300",
    )

    [project] = grc.parse_projects(projects_page((2008, body))).drafts

    assert project.fields["title_en"] == "Postdoctoral Research: 鄭某"
    assert project.warnings == ()


def test_a_period_typo_keeps_the_text_and_claims_no_dates():
    body = br_p("計畫", "Project", "國科會 NSC 2014/01/20~2014/96/30 NTD100,000")

    [project] = grc.parse_projects(projects_page((2014, body))).drafts

    assert project.fields["period_raw"] == "2014/01/20~2014/96/30"
    assert (project.fields["period_start"], project.fields["period_end"]) == (None, None)
    assert "no valid period" in project.warnings[0]


def test_a_doubled_slash_still_reads_the_date():
    body = br_p("計畫", "Project", "國科會 NSC 2021/06/01~2023//03/31 NTD1,428,340")

    [project] = grc.parse_projects(projects_page((2021, body))).drafts

    assert project.fields["period_end"] == date(2023, 3, 31)


def test_a_project_without_an_amount_blocks():
    body = br_p("計畫", "Project", "國科會 NSC 2020/01/01~2020/12/31")

    parsed = grc.parse_projects(projects_page((2020, body)))

    assert parsed.drafts == []
    assert "no NTD amount" in parsed.problems[0]


# --- Publications -----------------------------------------------------------------------


def publications_page(*sections):
    """sections: (heading markup, [item markup, ...])."""
    return blocks_of(rich("comp-pubs", *(heading + ol(*items) for heading, items in sections)))


def test_files_items_under_their_category_whatever_tag_names_it():
    blocks = publications_page(
        (h(6, "Journal papers​"), ["Hsu, Y. L. (2026). A study. Journal, 1(1), 1–2."]),
        (h(6, "Conference papers"), ["Chu, P. (1989, September). A talk."]),
        (h(6, "Books / book chapters"), ["徐業良(2025)，機械設計，三版。"]),
        (
            h(6, "Patents"),
            [f'徐業良, "感測墊", 專利第M1號, 2025/05/01 {a("PDF", "https://x/p.pdf")}'],
        ),
        (p("​General publications"), ["徐業良, 專欄文章, 季刊 109年第二十九期"]),
    )

    parsed = grc.parse_publications(blocks)

    assert parsed.problems == []
    assert [(d.fields["category"], d.fields["year"]) for d in parsed.drafts] == [
        ("Journal papers", 2026),
        ("Conference papers", 1989),
        ("Books / book chapters", 2025),
        ("Patents", 2025),
        ("General publications", 2020),  # ROC 109
    ]
    patent = parsed.drafts[3]
    assert patent.fields["item_text"].endswith("2025/05/01")  # the "PDF" label kept as a link
    assert patent.fields["links"] == [{"kind": "pdf", "text": "PDF", "url": "https://x/p.pdf"}]


def test_a_doi_is_the_identity_of_a_paper():
    doi = "https://doi.org/10.4017/gt.2026.25.1.1257.03"
    blocks = publications_page(
        *[
            (h(6, name), [f"Hsu (2026). Paper. {a(doi, doi)}"])
            for name in grc.PUBLICATION_CATEGORIES
        ]
    )

    draft = grc.parse_publications(blocks).drafts[0]

    assert draft.logical_key == "publication:journal:10.4017/gt.2026.25.1.1257.03"
    assert draft.fields["links"][0]["kind"] == "doi"


def test_a_missing_category_blocks():
    parsed = grc.parse_publications(publications_page((h(6, "Journal papers"), ["Hsu (2026). P."])))

    assert "no items under" in parsed.problems[0]


# --- Profile ----------------------------------------------------------------------------


def test_profile_prose_becomes_sections_and_lists_become_items():
    lists = rich(
        "comp-lists",
        *(
            h(6, name) + ol(f"{name} item, 2020/01 - present") + BLANK
            for name in grc.PROFILE_SECTIONS[:-1]
        ),
        p("Research Interests"),
        ol("Gerontechnology"),
    )
    blocks = blocks_of(
        rich("comp-intro", p("徐教授在元智大學服務。")),
        lists,
        rich("comp-bio", p("Biography"), p("Professor Hsu has served."), p("He founded GRC.")),
    )

    parsed = grc.parse_profile(blocks, person="徐業良", intro_heading="簡介")

    assert parsed.problems == []
    sections = {
        d.logical_key: d.raw["paragraphs"] for d in parsed.drafts if d.record_type == "section"
    }
    assert sections == {
        "section:徐業良:簡介": ["徐教授在元智大學服務。"],
        "section:徐業良:biography": ["Professor Hsu has served.", "He founded GRC."],
    }
    items = [d for d in parsed.drafts if d.record_type == "list_item"]
    assert [d.fields["category"] for d in items] == list(grc.PROFILE_SECTIONS)
    assert items[-1].fields["item_text"] == "Gerontechnology"
