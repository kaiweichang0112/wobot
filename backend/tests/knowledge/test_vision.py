import base64
import io
import json

from PIL import Image

from tests.knowledge.openai_responses import openai_answering
from wobot.knowledge.extraction import Answer
from wobot.knowledge.hashing import content_hash
from wobot.knowledge.vision import (
    DETAIL,
    INSTRUCTIONS,
    MAX_OUTPUT_TOKENS,
    MAX_SIDE,
    PROMPT_VERSION,
    REASONING_EFFORT,
    OpenAIVisionReader,
    VisualInput,
    VisualReading,
    prepare_image,
    visual_input,
)

# As for lectures: after changing the instructions, the schema or the request settings,
# bump PROMPT_VERSION and record the new fingerprint here.
PROMPT_FINGERPRINTS = {
    1: "a1e70a16f78b68b597c8424b75f49c1066c0861420cfb3c9fff708050a12de65",
}

READING = {
    "contains_information": True,
    "verbatim_text": ["體壓分佈測定", "42 36 30 mmHg"],
    "values": [{"label": "壓力", "value": "42", "unit": "mmHg"}],
    "relationships": ["紅色代表高壓力點。"],
    "description": "兩張床墊的體壓分佈圖並排比較。",
    "unreadable": [],
}


def png(size=(40, 20), mode="RGBA", color=(255, 0, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_changed_prompt_comes_with_a_new_version():
    fingerprint = content_hash(
        {
            "instructions": INSTRUCTIONS,
            "schema": VisualReading.model_json_schema(),
            "reasoning_effort": REASONING_EFFORT,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "detail": DETAIL,
            "max_side": MAX_SIDE,
        }
    )

    assert PROMPT_FINGERPRINTS.get(PROMPT_VERSION) == fingerprint


def test_a_picture_is_sent_as_one_png_frame_on_white_never_enlarged():
    small = Image.open(io.BytesIO(prepare_image(png())))
    large = Image.open(io.BytesIO(prepare_image(png((4032, 3024), "RGB", (0, 0, 255)))))

    assert (small.format, small.mode, small.size) == ("PNG", "RGB", (40, 20))
    assert small.getpixel((0, 0)) == (255, 255, 255)  # transparent red shows as white
    assert large.size == (MAX_SIDE, 1536)


def test_answers_are_kept_by_file_and_page_not_by_bytes():
    image = VisualInput("abc", b"one picture")
    redrawn = VisualInput("abc", b"the same picture drawn again")
    page = VisualInput("pdf", b"page", page=3)

    assert image == redrawn and visual_input(image) == visual_input(redrawn)
    assert visual_input(page)["page"] == 3
    assert "render_scale" in visual_input(page) and "render_scale" not in visual_input(image)


async def test_sends_the_picture_inline_under_a_strict_schema():
    requests = []
    client = openai_answering(
        {"type": "output_text", "text": json.dumps(READING), "annotations": []}, requests
    )

    answer = await OpenAIVisionReader(client, "gpt-test").read(VisualInput("abc", png()))

    assert answer == Answer(READING, None, "gpt-test-2026-01-01", 420, 31)
    sent = requests[0]
    [message] = sent["input"]
    [image] = message["content"]
    assert image["type"] == "input_image" and image["detail"] == DETAIL
    prefix = "data:image/png;base64,"
    assert image["image_url"].startswith(prefix)
    assert base64.b64decode(image["image_url"][len(prefix) :]) == prepare_image(png())
    assert sent["instructions"] == INSTRUCTIONS
    assert sent["text"]["format"]["strict"] is True
    assert sent["reasoning"] == {"effort": REASONING_EFFORT}
    assert sent["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["store"] is False


async def test_bytes_that_are_no_image_fail_without_a_call():
    requests = []
    client = openai_answering({"type": "output_text", "text": "{}", "annotations": []}, requests)

    answer = await OpenAIVisionReader(client, "gpt-test").read(VisualInput("x", b"<html>"))

    assert answer.output is None and answer.failure.startswith("unreadable image")
    assert requests == []
