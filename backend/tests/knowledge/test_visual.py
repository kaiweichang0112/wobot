from wobot.knowledge.chunking.sections import MAX_TOKENS
from wobot.knowledge.chunking.visual import IMAGE_EXPLANATION, image_chunks
from wobot.knowledge.extraction import Answer, Question
from wobot.knowledge.records.images import ShownImage, image_record, shown_images
from wobot.knowledge.records.sections import (
    ImagePlacement,
    SectionPage,
    image_placements,
    section_records,
)
from wobot.knowledge.sources.wix import Block, Image

PAGE = SectionPage("https://shop.example.test/mat", "範例公司", "shop", "運動地墊", "mat")
QUESTION = Question("visual_content", "test-vision", 1)


def heading(text, level=2):
    return Block("h", "heading", text, level=level)


def image(url, alt="", size=(400, 300)):
    return Block("i", "image", alt, image=Image(url, alt, *size))


def reading(**parts):
    return {
        "contains_information": True,
        "verbatim_text": [],
        "values": [],
        "relationships": [],
        "description": "一張產品照片。",
        "unreadable": [],
    } | parts


def record(shown, output):
    answer = Answer(output, None, "test-vision-2026-01-01", 900, 80)
    return image_record(PAGE, shown, sha256="abc", answer=answer, question=QUESTION, position=0)


# --- Placements -------------------------------------------------------------------------


def test_an_image_sits_under_the_headings_its_text_would():
    flat = [heading("規格", 1), image("/a.png"), heading("配件", 6), image("/b.png")]
    nested = [heading("說明", 1), heading("硬體"), heading("燈條", 3), image("/c.png")]

    assert [p.path for p in image_placements(flat, nested=False)] == [("規格",), ("配件",)]
    # The h1 names the page, as for sections.
    assert [p.path for p in image_placements(nested, nested=True)] == [("硬體", "燈條")]


def test_images_change_no_section():
    blocks = [heading("規格"), image("/a.png"), Block("p", "paragraph", "重 2 公斤")]

    [draft] = section_records(blocks, PAGE, nested=False).drafts

    assert draft.raw["paragraphs"] == ["重 2 公斤"]


def test_only_images_that_can_hold_something_are_read_each_once():
    placements = [
        ImagePlacement(("規格",), Image("/a.png", "規格圖", 539, 245)),
        ImagePlacement(("配件",), Image("/a.png", "規格圖", 539, 245)),  # shown again
        ImagePlacement((), Image("/logo.png", "logo.png", 130, 54)),  # too small
        ImagePlacement((), Image("/icon.svg", "測驗", 1097, 1097)),  # drawn
        ImagePlacement((), Image("data:image/png;base64,AAAA", "", None, None)),
        ImagePlacement((), Image("/photo.jpg", "IMG_8103.jpg", None, None)),  # size unknown
    ]

    assert shown_images(placements, PAGE.url) == [
        ShownImage("https://shop.example.test/a.png", ("規格圖",), (("規格",), ("配件",))),
        ShownImage("https://shop.example.test/photo.jpg", (), ((),)),
    ]


# --- Chunks -----------------------------------------------------------------------------


def test_an_image_chunk_marks_what_is_the_model_s_and_what_the_page_s():
    shown = ShownImage("https://shop.example.test/a.png", ("體壓分佈測定",), (("規格",),))
    output = reading(
        verbatim_text=["體壓分佈測定", "WhizPad智慧床墊"],
        values=[
            {"label": "高壓力點", "value": "42", "unit": "mmHg"},
            {"label": "減少壓傷", "value": "88", "unit": "%"},
        ],
        relationships=["紅色代表高壓力點。"],
        description="兩張體壓分佈圖並排比較。",
        unreadable=["右下角的小字"],
    )

    [chunk] = image_chunks([record(shown, output)])

    assert chunk.strategy == IMAGE_EXPLANATION
    assert chunk.context_header == "範例公司 › 運動地墊 › 規格"
    assert chunk.body.split("\n\n") == [
        "圖片替代文字（網頁原文）：體壓分佈測定",
        "模型描述：兩張體壓分佈圖並排比較。",
        "圖中文字（模型轉錄）：\n體壓分佈測定\nWhizPad智慧床墊",
        "數值（模型讀取）：\n- 高壓力點：42 mmHg\n- 減少壓傷：88%",
        "圖示關係（模型說明）：\n- 紅色代表高壓力點。",
        "無法辨識（模型回報）：\n- 右下角的小字",
        "原圖：https://shop.example.test/a.png",
    ]
    assert "https://" not in chunk.embedding_input
    assert chunk.links == ({"text": "原圖", "url": "https://shop.example.test/a.png"},)


def test_a_photo_is_its_description_and_where_it_is_shown():
    shown = ShownImage("https://shop.example.test/p.jpg", (), (("規格",), ()))

    [chunk] = image_chunks([record(shown, reading(contains_information=False))])

    assert chunk.body.split("\n\n")[:2] == ["出現位置：規格；運動地墊", "模型描述：一張產品照片。"]


def test_a_long_reading_splits_under_the_cap():
    shown = ShownImage("https://shop.example.test/a.png", (), ((),))
    lines = [f"第 {i} 行：地墊感測腳步並亮燈回饋。" for i in range(300)]

    long = reading(verbatim_text=lines, description="一段過長的描述，" * 400)

    chunks = image_chunks([record(shown, long)])

    assert len(chunks) > 1
    assert all(chunk.token_count <= MAX_TOKENS for chunk in chunks)


def test_a_reading_past_the_section_target_stays_one_chunk():
    shown = ShownImage("https://shop.example.test/a.png", (), ((),))
    lines = [f"第 {i} 行：地墊感測腳步並亮燈回饋。" for i in range(20)]  # about 560 tokens

    [chunk] = image_chunks([record(shown, reading(verbatim_text=lines))])

    assert 500 < chunk.token_count <= MAX_TOKENS
