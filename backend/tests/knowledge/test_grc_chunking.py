from datetime import date

from wobot.knowledge.chunking import grc as chunking
from wobot.knowledge.chunking.drafts import BlockItem, block_chunks
from wobot.knowledge.records.drafts import record_draft


def student(name, year, url="https://hdl.handle.net/11296/x", degree="master"):
    fields = {
        "name": name,
        "degree": degree,
        "graduation_year": year,
        "thesis_title_zh": f"{name}的論文",
        "thesis_title_en": None,
        "fulltext_url": url,
    }
    return record_draft("student", f"student:{degree}:{name}", raw={}, fields=fields, locator={})


def test_one_chunk_per_degree_and_year_showing_links_but_not_embedding_them():
    chunks = chunking.student_chunks(
        [student("王", 2022), student("李", 2022), student("陳", 2021)]
    )

    assert [c.context_header for c in chunks] == [
        "元智大學老人福祉科技研究中心｜碩士畢業生｜2022年",
        "元智大學老人福祉科技研究中心｜碩士畢業生｜2021年",
    ]
    first = chunks[0]
    assert "電子全文：https://hdl.handle.net/11296/x" in first.body
    assert "https://" not in first.embedding_input
    assert (
        first.links
        == ({"kind": "thesis", "text": "電子全文", "url": "https://hdl.handle.net/11296/x"},) * 2
    )
    assert len(first.record_revisions) == 2


def test_a_project_shows_period_and_amount_or_the_text_when_the_period_is_a_typo():
    fields = {
        "title_zh": "計畫",
        "title_en": "Project",
        "funder_raw": "國科會 NSC",
        "period_raw": "2014/01/20~2014/96/30",
        "period_start": None,
        "period_end": None,
        "amount_ntd": 3_600_000,
        "amount_raw": "NTD3,600,000",
        "year": 2014,
    }
    good = {**fields, "period_start": date(2022, 12, 8), "period_end": date(2024, 4, 30)}
    drafts = [
        record_draft("project", "project:a", raw={}, fields=good, locator={}),
        record_draft("project", "project:b", raw={}, fields=fields, locator={}),
    ]

    body = chunking.project_chunks(drafts)[0].body

    assert "執行期間：2022/12/08～2024/04/30" in body
    assert "執行期間：2014/01/20~2014/96/30（原文如此）" in body
    assert "金額：新台幣 3,600,000 元" in body


def test_a_long_block_splits_between_items_never_inside_one():
    items = [
        BlockItem(f"項目{i} " + "長" * 300, f"項目{i} " + "長" * 300, (f"k{i}", "h"))
        for i in range(5)
    ]

    chunks = block_chunks(
        strategy="publication_block",
        strategy_version=1,
        heading_path=["x"],
        context_header="標題",
        items=items,
        max_tokens=700,
    )

    assert len(chunks) > 1
    assert [c.context_header for c in chunks] == [
        f"標題 ({n}/{len(chunks)})" for n in range(1, len(chunks) + 1)
    ]
    assert sum(len(c.record_revisions) for c in chunks) == 5  # every item exactly once
    assert all(c.token_count <= 700 or len(c.record_revisions) == 1 for c in chunks)


def lecture(entry, title, year_block="2024~2025", category="keynote", url=None):
    fields = {
        "category": category,
        "year_block": year_block,
        "entry_text": entry,
        "title": title,
        "links": [{"kind": "pdf", "text": "PDF", "url": url}],
    }
    return record_draft("lecture", f"lecture:{entry}", raw={}, fields=fields, locator={})


def test_lecture_blocks_follow_the_page_and_never_what_a_model_read():
    entry = "“Smart care,” keynote speech, Care Congress, 2025/12/05"
    pdf = "https://drive.google.com/file/d/talk/view"
    chunks = chunking.lecture_chunks(
        [lecture(entry, "Smart care", url=pdf), lecture("“Beds,” Forum, 2024/05/10", "Beds")]
    )
    reread = chunking.lecture_chunks(
        [lecture(entry, "Smart", url=pdf), lecture("“Beds,” Forum, 2024/05/10", None)]
    )

    assert [c.context_header for c in chunks] == [
        "徐業良教授演講｜Keynote and plenary speeches at international conferences｜2024~2025年"
    ]
    assert chunks[0].body == f"{entry}\nPDF：{pdf}\n\n“Beds,” Forum, 2024/05/10"
    assert "https://" not in chunks[0].embedding_input
    assert chunks[0].links == ({"kind": "pdf", "text": "PDF", "url": pdf},)
    assert [c.content_hash for c in reread] == [c.content_hash for c in chunks]


def test_the_item_variant_gives_each_record_its_own_chunk_under_the_same_header():
    drafts = [lecture("“A,” Forum, 2025/01/02", "A"), lecture("“B,” Forum, 2024/05/10", "B")]

    blocks = chunking.lecture_chunks(drafts)
    items = chunking.lecture_chunks(drafts, per_item=True)

    assert [c.strategy for c in blocks] == ["lecture_block"]
    assert [c.strategy for c in items] == ["lecture_item", "lecture_item"]
    assert {c.context_header for c in items} == {blocks[0].context_header}
    assert [c.record_revisions for c in items] == [(d.revision,) for d in drafts]


def test_the_item_variant_keeps_prose_sections_whole():
    section = record_draft(
        "section",
        "section:徐業良:簡介",
        raw={"heading_path": ["徐業良", "簡介"], "paragraphs": ["第一段。", "第二段。"]},
        fields={},
        locator={},
    )

    (chunk,) = chunking.profile_chunks([section], per_item=True)

    assert chunk.strategy == "profile_section"
    assert chunk.body == "第一段。\n\n第二段。"
