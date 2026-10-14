"""Painting the frames: the photo prepared, each frame from its parent, as many at once
as allowed, and the image model's answer checked and costed."""

import asyncio
import base64
import io

import httpx2
import openai
import pytest
from PIL import Image

from wobot.assets.avatar import (
    Blocked,
    FrameFailed,
    Painter,
    checked_frame,
    contact_sheet,
    paint_all,
    prepare_photo,
)
from wobot.assets.frames import BASE, FRAME_KEYS, parent_of


def png(size=(1024, 1024), transparent: bool = True) -> bytes:
    """A frame: a face on nothing, or, not transparent, on an opaque backdrop."""
    image = Image.new("RGBA", size, (0, 0, 0, 0) if transparent else (40, 40, 40, 255))
    image.paste(
        (240, 200, 170, 255), (size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4)
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


async def test_each_frame_is_painted_from_its_parent_once_the_parent_is_done():
    started: list[str] = []

    async def paint(key: str, source: bytes) -> bytes:
        parent = parent_of(key)
        assert source == (b"photo" if parent is None else f"of {parent}".encode())
        assert parent is None or parent in started
        started.append(key)
        await asyncio.sleep(0)
        return f"of {key}".encode()

    results = await paint_all(FRAME_KEYS, {None: b"photo"}, paint, concurrency=14)

    assert set(results) == set(FRAME_KEYS)
    assert started[0] == BASE


async def test_no_more_frames_are_painted_at_once_than_allowed():
    running, most = 0, 0

    async def paint(key: str, source: bytes) -> bytes:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return b"x"

    await paint_all(FRAME_KEYS, {None: b"photo"}, paint, concurrency=3)

    assert most == 3


async def test_a_failed_frame_blocks_only_what_is_made_from_it():
    async def paint(key: str, source: bytes) -> bytes:
        if key == "anger_closed":
            raise FrameFailed("IMAGE_REJECTED")
        return b"x"

    results = await paint_all(FRAME_KEYS, {None: b"photo"}, paint, concurrency=14)

    assert isinstance(results["anger_closed"], FrameFailed)
    assert isinstance(results["anger_half"], Blocked)
    assert isinstance(results["anger_wide"], Blocked)
    assert sum(isinstance(r, bytes) for r in results.values()) == 18


async def test_frames_made_before_serve_as_parents():
    painted = []

    async def paint(key: str, source: bytes) -> bytes:
        painted.append((key, source))
        return b"new"

    await paint_all(["joy_half", "fear_wide"], {"joy_closed": b"joy"}, paint, concurrency=2)

    assert painted == [("joy_half", b"joy")]  # fear_closed was neither made nor given


def test_the_photo_is_turned_upright_shrunk_and_stripped_of_metadata():
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90° clockwise to view
    exif[0x010F] = "Camera maker"
    out = io.BytesIO()
    Image.new("RGB", (3000, 2000), "red").save(out, format="JPEG", exif=exif)

    prepared = Image.open(io.BytesIO(prepare_photo(out.getvalue())))

    assert prepared.format == "PNG"
    assert prepared.size == (683, 1024)  # upright: taller than wide
    assert not prepared.getexif()
    assert "exif" not in prepared.info


def test_what_is_not_an_image_is_refused():
    with pytest.raises(ValueError):
        prepare_photo(b"not an image")


@pytest.mark.parametrize(
    "content",
    [png((512, 512)), png(transparent=False), b"not an image"],
    ids=["too small", "opaque", "not an image"],
)
def test_a_frame_must_be_a_transparent_1024_pixel_png(content):
    with pytest.raises(FrameFailed, match="INVALID_IMAGE"):
        checked_frame(content)


def image_response(content: bytes = png()) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "created": 0,
            "data": [{"b64_json": base64.b64encode(content).decode()}],
            "usage": {
                "input_tokens": 1300,
                "input_tokens_details": {"text_tokens": 200, "image_tokens": 1100},
                "output_tokens": 1056,
                "total_tokens": 2356,
            },
        },
    )


def painter(responses: list[httpx2.Response], requests: list) -> Painter:
    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return responses[min(len(requests), len(responses)) - 1]

    client = openai.AsyncOpenAI(
        api_key="test-key",
        max_retries=3,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return Painter(client, "gpt-image-test", "medium")


async def test_a_frame_is_asked_for_with_its_prompt_and_costed_from_its_usage():
    requests = []

    painted = await painter([image_response()], requests).paint("joy_closed", png())

    [request] = requests
    assert request.url.path == "/v1/images/edits"
    body = request.content.decode("latin-1")
    for field, value in [
        ("model", "gpt-image-test"),
        ("quality", "medium"),
        ("size", "1024x1024"),
        ("background", "transparent"),
        ("output_format", "png"),
    ]:
        assert f'name="{field}"\r\n\r\n{value}\r\n' in body
    assert "JOY" in body
    assert (painted.text_input_tokens, painted.image_input_tokens, painted.output_tokens) == (
        200,
        1100,
        1056,
    )
    # 200 × $5 + 1,100 × $8 + 1,056 × $30, per million tokens
    assert painted.cost == pytest.approx(0.04148)


async def test_a_rate_limited_frame_waits_and_tries_again():
    requests = []
    limited = httpx2.Response(
        429,
        headers={"retry-after-ms": "10"},
        json={"error": {"message": "Rate limit", "type": "rate_limit_error", "code": None}},
    )

    painted = await painter([limited, limited, image_response()], requests).paint(BASE, png())

    assert len(requests) == 3
    assert painted.png == png()


async def test_a_photo_the_model_refuses_fails_as_rejected():
    refused = httpx2.Response(
        400,
        json={
            "error": {
                "message": "Your request was rejected by the safety system.",
                "type": "image_generation_user_error",
                "code": "moderation_blocked",
            }
        },
    )

    with pytest.raises(FrameFailed, match="IMAGE_REJECTED"):
        await painter([refused], []).paint(BASE, png())


async def test_an_answer_that_is_not_a_frame_fails_as_invalid():
    with pytest.raises(FrameFailed, match="INVALID_IMAGE"):
        await painter([image_response(png((256, 256)))], []).paint(BASE, png())


def test_the_sheet_shows_the_frames_on_the_app_s_backdrop():
    sheet = Image.open(io.BytesIO(contact_sheet({BASE: png(), "joy_half": png()}, "#1F1E1C")))

    assert sheet.size == (3 * 256, 7 * 256)
    assert sheet.getpixel((2, 2)) == (0x1F, 0x1E, 0x1C)  # through the frame's transparency
    assert sheet.getpixel((128, 128)) == (240, 200, 170)  # the base's face, top left
    assert sheet.getpixel((256 + 128, 256 + 128)) == (240, 200, 170)  # joy_half
    assert sheet.getpixel((128, 256 + 128)) == (0x1F, 0x1E, 0x1C)  # joy_closed, not given
