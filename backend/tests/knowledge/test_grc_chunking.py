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
