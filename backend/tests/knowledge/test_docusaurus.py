from tests.knowledge.docs_pages import code, doc_page, heading, table
from wobot.knowledge.sources.docusaurus import article_blocks
from wobot.knowledge.sources.wix import Image, Link


def blocks_of(*body):
    return article_blocks(doc_page("控制盒說明", *body).decode())


def test_reads_the_article_body_only_without_heading_anchors():
    blocks = blocks_of(heading(2, "連線"), "<p>開啟藍牙後配對。</p>")

    assert [(b.kind, b.level, b.text) for b in blocks] == [
        ("heading", 1, "控制盒說明"),
        ("heading", 2, "連線"),
        ("paragraph", None, "開啟藍牙後配對。"),
    ]


def test_keeps_links_videos_and_images_where_they_stand():
    blocks = blocks_of(
        '<p>見 <a href="/docs/sdk">SDK 說明</a>。<img src="/a.png" alt="示意圖" width="618"></p>',
        '<p><iframe src="https://www.youtube.com/embed/abc"></iframe></p>',
    )

    assert [(b.kind, b.text, b.links) for b in blocks[1:]] == [
        ("paragraph", "見 SDK 說明。", (Link("SDK 說明", "/docs/sdk"),)),
        ("image", "示意圖", ()),
        ("button", "", (Link("video", "https://www.youtube.com/embed/abc"),)),
    ]
    assert blocks[2].image == Image("/a.png", "示意圖", 618, None)


def test_lists_keep_nesting_as_indentation():
    blocks = blocks_of("<ul><li>準備<ul><li>地墊</li><li>控制盒</li></ul></li><li>開機</li></ul>")

    assert [(b.kind, b.text) for b in blocks[1:]] == [
        ("item", "準備"),
        ("item", "  地墊"),
        ("item", "  控制盒"),
        ("item", "開機"),
    ]


def test_tables_keep_their_cells_and_code_its_lines():
    blocks = blocks_of(
        table(("欄位", "說明"), ("Byte 0", "<code>0x01</code> 開燈")),
        code("for i in range(3):", "    blink(i)"),
    )

    assert blocks[1].kind == "table"
    assert blocks[1].rows == (("欄位", "說明"), ("Byte 0", "0x01 開燈"))
    assert (blocks[2].kind, blocks[2].text) == ("code", "for i in range(3):\n    blink(i)")


def test_reads_inside_folded_details_and_callouts():
    blocks = blocks_of(
        '<div class="theme-admonition"><div><p>注意：請先充電。</p></div></div>',
        "<details><summary>完整對照表</summary><div>"
        + table(("編號", "顏色"), ("01", "紅"))
        + "</div></details>",
    )

    assert [(b.kind, b.text or b.rows) for b in blocks[1:]] == [
        ("paragraph", "注意：請先充電。"),
        ("paragraph", "完整對照表"),
        ("table", (("編號", "顏色"), ("01", "紅"))),
    ]


def test_a_page_without_an_article_body_yields_nothing():
    assert article_blocks("<html><body><p>Not found</p></body></html>") == []
