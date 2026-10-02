from tests.knowledge.wix_pages import BLANK, NBSP, ZWSP, a, br_p, h, ol, p, page, rich
from wobot.knowledge.sources.wix import IMAGE_HOST, Image, Link, clean_block_text, page_blocks


def blocks_of(*elements):
    return [b for b in page_blocks(page(*elements).decode()) if b.element_id != "comp-footer"]


def test_joins_split_spans_without_inventing_spaces():
    [block] = blocks_of(
        rich(
            "comp-a", p("科技部 Ministry", NBSP, " ", "2020/08/01~2022/07/31 N", "TD1,", "900,000")
        )
    )

    assert block.text == "科技部 Ministry 2020/08/01~2022/07/31 NTD1,900,000"


def test_cleans_zero_width_and_no_break_spaces_even_inside_dates():
    assert clean_block_text(f"{ZWSP}2012/11/23~201{ZWSP}3/12/31{NBSP}{NBSP} NTD276,000") == (
        "2012/11/23~2013/12/31 NTD276,000"
    )


def test_keeps_line_breaks_and_marks_blank_paragraphs():
    blocks = blocks_of(rich("comp-a", br_p("中文標題", "English title"), BLANK, p("下一個")))

    assert [(b.kind, b.text) for b in blocks] == [
        ("paragraph", "中文標題\nEnglish title"),
        ("blank", ""),
        ("paragraph", "下一個"),
    ]


def test_reads_headings_and_list_items_with_their_links():
    blocks = blocks_of(
        rich("comp-a", h(6, "Patents"), ol(f"徐某, 專利 {a('PDF', 'https://example.com/p.pdf')}"))
    )

    assert (blocks[0].kind, blocks[0].level, blocks[0].text) == ("heading", 6, "Patents")
    assert blocks[1].kind == "item"
    assert blocks[1].links == (Link("PDF", "https://example.com/p.pdf"),)


def test_a_dataquery_link_has_no_url():
    [block] = blocks_of(rich("comp-a", f"<p>{a('電子全文')}</p>"))

    assert block.links == (Link("電子全文", None),)


def test_repeater_items_share_their_suffix():
    blocks = blocks_of(
        rich("comp-year__item-k1", p("2022")), rich("comp-list__item-k1", p("王小明"))
    )

    assert [b.repeater_item for b in blocks] == ["item-k1", "item-k1"]


def test_reads_buttons_only_when_asked_named_after_their_owner():
    html = page(
        rich("comp-a", p("文字")),
        '<div id="comp-btn"><a data-testid="linkElement" href="https://example.test/c.pdf">'
        "<span>型錄</span></a></div>",
    ).decode()

    assert "button" not in {b.kind for b in page_blocks(html)}
    [button] = [b for b in page_blocks(html, buttons=True) if b.kind == "button"]
    assert (button.element_id, button.text) == ("comp-btn", "型錄")
    assert button.links == (Link("型錄", "https://example.test/c.pdf"),)


def test_reads_images_as_their_original_files_only_when_asked():
    resized = f"https://{IMAGE_HOST}/media/ab_1~mv2.png/v1/fill/w_539,h_245,al_c/a.png"
    gallery = f"https://{IMAGE_HOST}/media/ab_2~mv2.png/v1/fill/w_774,h_531,q_90/ab_2~mv2.png"
    html = page(
        rich("comp-a", h(2, "規格")),
        f'<div id="comp-img"><img src="{resized}" alt="規格圖" width="539" height="245"></div>',
        f'<div id="item-g"><img src="{gallery}" alt="懶人包"></div>',
        '<img src="https://www.facebook.com/tr?id=1" width="1" height="1">',
    ).decode()

    assert all(b.kind != "image" for b in page_blocks(html))
    blocks = [b for b in page_blocks(html, images=True) if b.kind == "image"]
    assert [(b.element_id, b.image) for b in blocks] == [
        ("comp-img", Image(f"https://{IMAGE_HOST}/media/ab_1~mv2.png", "規格圖", 539, 245)),
        ("item-g", Image(f"https://{IMAGE_HOST}/media/ab_2~mv2.png", "懶人包", 774, 531)),
    ]
