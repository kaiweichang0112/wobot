from wobot.knowledge.chunking.sections import (
    MAX_TOKENS,
    SECTION_TEXT,
    TECHNICAL_TABLE,
    section_chunks,
    table_rows,
)
from wobot.knowledge.records.sections import SectionPage, section_records
from wobot.knowledge.sources.wix import Block, Link
from wobot.knowledge.tokens import count_tokens

PAGE = SectionPage("https://shop.example.test/mat", "範例公司", "shop", "運動地墊", "mat")


def heading(text, level=2, element="h"):
    return Block(element, "heading", text, level=level)


def para(text, element="p", links=()):
    return Block(element, "paragraph", text, links)


def button(url, label="型錄"):
    return Block("b", "button", label, (Link(label, url),))


def paths(parsed):
    return [(d.raw["heading_path"][2:], d.raw["paragraphs"]) for d in parsed.drafts]


# --- Records ----------------------------------------------------------------------------


def test_a_wix_text_box_is_one_paragraph_and_headings_are_one_level():
    blocks = [
        para("範例公司", element="header"),
        heading("特色", level=6),
        para("第一行", element="box"),
        para("第二行", element="box"),
        Block("box", "blank", ""),
        para("下一段", element="box"),
        heading("規格", level=1),
        para("重 2 公斤", element="spec"),
    ]

    parsed = section_records(blocks, PAGE, nested=False)

    assert paths(parsed) == [
        ([], ["範例公司"]),
        (["特色"], ["第一行\n第二行", "下一段"]),
        (["規格"], ["重 2 公斤"]),
    ]


def test_a_heading_with_nothing_under_it_is_content_but_a_repeated_one_is_not_new():
    blocks = [
        heading("運動地墊"),
        heading("運動地墊"),
        para("多人遊戲。"),
        heading("全台 13 縣市"),
        heading("40 間單位使用"),
    ]

    parsed = section_records(blocks, PAGE, nested=False)

    assert paths(parsed) == [
        (["運動地墊"], ["多人遊戲。"]),
        (["全台 13 縣市"], []),
        (["40 間單位使用"], []),
    ]


def test_docs_headings_nest_under_the_page_title():
    blocks = [
        heading("常見問題", level=1),
        heading("硬體", level=2),
        heading("Q: 可以戶外使用嗎？", level=3),
        para("可防潑水。"),
        heading("Q: 要充電嗎？", level=3),
        para("插電使用。"),
        heading("App", level=2),
        para("支援兩種系統。"),
    ]

    parsed = section_records(blocks, PAGE, nested=True)

    # "硬體" only opens its questions, so it is no record of its own.
    assert paths(parsed) == [
        (["硬體", "Q: 可以戶外使用嗎？"], ["可防潑水。"]),
        (["硬體", "Q: 要充電嗎？"], ["插電使用。"]),
        (["App"], ["支援兩種系統。"]),
    ]


def test_keeps_buttons_to_other_sites_and_files_but_not_the_menu():
    blocks = [
        heading("下載"),
        button("https://shop.example.test/_files/ugd/manual.pdf", "說明書"),
        button("https://drive.google.com/file/d/x/view", "英文型錄"),
        button("https://shop.example.test/contact", "聯絡"),
        button("tel:+886200000000", ""),
        para("見 SDK", links=(Link("SDK", "/docs/sdk"),)),
    ]

    [draft] = section_records(blocks, PAGE, nested=False).drafts

    assert [link["url"] for link in draft.raw["links"]] == [
        "https://shop.example.test/_files/ugd/manual.pdf",
        "https://drive.google.com/file/d/x/view",
        "https://shop.example.test/docs/sdk",
    ]


def test_a_table_remembers_where_it_stood():
    blocks = [
        heading("規格"),
        para("尺寸如下。"),
        Block("", "table", "", rows=(("項目", "數值"), ("長", "30 cm"))),
        para("單位為公分。"),
    ]

    [draft] = section_records(blocks, PAGE, nested=True).drafts

    assert draft.raw["tables"] == [{"after": 1, "rows": [["項目", "數值"], ["長", "30 cm"]]}]
    assert draft.logical_key == "section:shop:mat:規格"


# --- Chunks -----------------------------------------------------------------------------


def records(*sections):
    """Sections as (heading, paragraphs) under the page, read as a Wix page would be."""
    blocks = []
    for title, paragraphs in sections:
        blocks.append(heading(title))
        blocks += [para(text, element=f"{title}{i}") for i, text in enumerate(paragraphs)]
    return section_records(blocks, PAGE, nested=False).drafts


def test_short_sections_under_one_heading_share_a_chunk_each_under_its_own_line():
    drafts = records(("防潑水", ["可在室內外使用。"]), ("多人", ["最多四人同時遊戲。"]))

    [chunk] = section_chunks(drafts)

    assert chunk.context_header == "範例公司 › 運動地墊"
    assert chunk.body == "## 防潑水\n可在室內外使用。\n\n## 多人\n最多四人同時遊戲。"
    assert chunk.strategy == SECTION_TEXT
    assert len(chunk.record_revisions) == 2


def test_a_heading_with_nothing_under_it_leads_into_the_next_section_or_stands_alone():
    long = "離床前提醒照護者，減少跌倒。" * 20  # past MIN_TOKENS, so it joins no short run
    leading = records(("安心臥床墊", []), ("特色", [long]))
    trailing = records(("特色", [long]), ("全台 13 縣市", []))

    [chunk] = section_chunks(leading)
    assert chunk.body.startswith("## 安心臥床墊\n\n## 特色\n")
    assert len(chunk.record_revisions) == 2
    # Every record is in a chunk, as the version check requires.
    last = section_chunks(trailing)[-1]
    assert (last.context_header, last.body) == ("範例公司 › 運動地墊", "## 全台 13 縣市")


def test_a_long_section_splits_between_paragraphs_never_past_the_cap():
    paragraph = "地墊感測腳步並亮燈回饋。" * 25  # about 200 tokens
    drafts = records(("說明", [paragraph] * 6))

    chunks = section_chunks(drafts)

    assert len(chunks) > 1
    assert all(c.token_count <= MAX_TOKENS for c in chunks)
    assert [c.context_header for c in chunks][0] == f"範例公司 › 運動地墊 › 說明 (1/{len(chunks)})"
    assert all(paragraph in c.body.split("\n\n") for c in chunks)  # none cut in two


def test_one_paragraph_longer_than_the_cap_is_cut():
    drafts = records(("程式碼", ["\n".join(f"line_{i} = blink({i})" for i in range(400))]))

    chunks = section_chunks(drafts)

    assert all(c.token_count <= MAX_TOKENS for c in chunks)
    assert sum(count_tokens(c.body) for c in chunks) >= count_tokens(drafts[0].raw["paragraphs"][0])


def test_table_rows_repeat_their_header_and_a_rows_only_chunk_is_a_table_chunk():
    rows = [["編號", "R", "顏色"], *([str(i), str(i * 10), ""] for i in range(200))]
    blocks = [heading("顏色表", level=2), Block("", "table", "", rows=tuple(map(tuple, rows)))]
    drafts = section_records(blocks, PAGE, nested=True).drafts

    chunks = section_chunks(drafts)

    assert table_rows(rows[:2]) == ["編號：0｜R：0"]  # an empty cell is left out
    assert {c.strategy for c in chunks[1:]} == {TECHNICAL_TABLE}
    assert all(line.startswith("編號：") for c in chunks[1:] for line in c.body.split("\n\n"))


def test_links_are_shown_but_their_addresses_not_embedded():
    blocks = [heading("下載"), button("https://drive.google.com/file/d/x/view", "英文型錄")]
    drafts = section_records(blocks, PAGE, nested=False).drafts

    [chunk] = section_chunks(drafts)

    assert "英文型錄：https://drive.google.com/file/d/x/view" in chunk.body
    assert "drive.google.com" not in chunk.embedding_input
    assert chunk.links == ({"text": "英文型錄", "url": "https://drive.google.com/file/d/x/view"},)
