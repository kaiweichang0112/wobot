"""Make an avatar's frames: the photo prepared, each frame painted by the image model from
the frame it depends on, as many at once as the work and the limit allow.

The frames form a tree three levels deep (`frames.parent_of`): the base, six closed
expressions, fourteen mouths. Each frame starts once its own parent is done, not when its
whole level is, so a slow frame holds up only its own children.
"""

import asyncio
import base64
import io
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import openai
from PIL import Image, ImageOps, UnidentifiedImageError

from wobot.assets.avatar_prompts import prompt_for
from wobot.assets.frames import EXPRESSIONS, FRAME_KEYS, MOUTHS, parent_of
from wobot.config import Settings

SIZE = 1024
# The photo is sent at this size at most: the model paints a 1024-pixel face, and a
# larger input only costs more input tokens.
PHOTO_MAX_SIDE = 1024
PHOTO_MAX_BYTES = 10 * 1024 * 1024
# US$ per 1M tokens for gpt-image-2.5-sunburst (checked 2026-10-14, spec 07): what a
# frame cost, from the usage the API reports.
PRICES = {"text_input": 5.0, "image_input": 8.0, "image_output": 30.0}

logger = logging.getLogger(__name__)


class FrameFailed(Exception):
    """A frame could not be made; `code` is what the app is told."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Blocked(Exception):
    """A frame's parent failed, so it was not attempted."""


@dataclass(frozen=True)
class Painted:
    png: bytes
    text_input_tokens: int
    image_input_tokens: int
    output_tokens: int
    seconds: float

    @property
    def input_tokens(self) -> int:
        return self.text_input_tokens + self.image_input_tokens

    @property
    def cost(self) -> float:
        return (
            self.text_input_tokens * PRICES["text_input"]
            + self.image_input_tokens * PRICES["image_input"]
            + self.output_tokens * PRICES["image_output"]
        ) / 1_000_000


def prepare_photo(content: bytes) -> bytes:
    """The photo as the model gets it: upright, RGB, at most 1024 pixels a side, as a PNG
    without any of the original's metadata, such as where it was taken."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("not an image") from exc
    image.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
    out = io.BytesIO()
    image.save(out, format="PNG")  # writes no EXIF unless given one
    return out.getvalue()


def checked_frame(png: bytes) -> bytes:
    """The model's image, if it is a PNG of the size every frame shares, with a background
    that shows through: some of it fully transparent."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            image.verify()
        with Image.open(io.BytesIO(png)) as image:
            fits = (
                image.format == "PNG"
                and image.size == (SIZE, SIZE)
                and image.mode == "RGBA"
                and image.getchannel("A").getextrema()[0] == 0
            )
    except (UnidentifiedImageError, OSError) as exc:
        raise FrameFailed("INVALID_IMAGE") from exc
    if not fits:
        raise FrameFailed("INVALID_IMAGE")
    return png


def contact_sheet(frames: dict[str, bytes], backdrop: str, cell: int = 256) -> bytes:
    """The frames on the app's backdrop, a row per expression and a column per mouth:
    how they look in Avatar mode, for a person to review (AC-AVA-07)."""
    columns = len(MOUTHS)
    sheet = Image.new("RGB", (cell * columns, cell * len(EXPRESSIONS)), backdrop)
    for index, key in enumerate(FRAME_KEYS):
        if key not in frames:
            continue
        with Image.open(io.BytesIO(frames[key])) as image:
            tile = image.convert("RGBA").resize((cell, cell))
        row, column = divmod(index, columns)
        sheet.paste(tile, (column * cell, row * cell), tile)
    out = io.BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()


class Painter:
    """Paints one frame with the image model, from the image it depends on."""

    def __init__(self, client: openai.AsyncOpenAI, model: str, quality: str) -> None:
        self._client = client
        self.model = model
        self.quality = quality

    async def paint(self, frame_key: str, source: bytes) -> Painted:
        started = time.perf_counter()
        try:
            result = await self._client.images.edit(
                model=self.model,
                image=("source.png", source, "image/png"),
                prompt=prompt_for(frame_key),
                size=f"{SIZE}x{SIZE}",
                quality=self.quality,
                background="transparent",
                output_format="png",
                n=1,
            )
        except openai.BadRequestError as exc:
            # Moderation, or an image the model will not edit: a new photo may pass.
            logger.warning("frame refused", extra={"frame": frame_key, "code": exc.code})
            raise FrameFailed("IMAGE_REJECTED") from exc
        except openai.APIError as exc:
            # Rate limits and server errors were retried by the client first.
            logger.warning("frame failed", extra={"frame": frame_key, "error": type(exc).__name__})
            raise FrameFailed("PROVIDER_UNAVAILABLE") from exc
        if not result.data or not result.data[0].b64_json:
            raise FrameFailed("INVALID_IMAGE")
        png = checked_frame(base64.b64decode(result.data[0].b64_json))
        usage = result.usage
        details = usage.input_tokens_details if usage else None
        return Painted(
            png=png,
            text_input_tokens=getattr(details, "text_tokens", 0) or 0,
            image_input_tokens=getattr(details, "image_tokens", 0) or 0,
            output_tokens=usage.output_tokens if usage else 0,
            seconds=round(time.perf_counter() - started, 2),
        )


@asynccontextmanager
async def painter_of(
    settings: Settings, quality: str | None = None
) -> AsyncIterator[Painter | None]:
    """The image model, when OpenAI's key is set. Its client retries 429 and 5xx three
    times, waiting as the response's Retry-After asks."""
    if settings.openai_api_key is None:
        yield None
        return
    async with openai.AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.avatar_timeout_seconds,
        max_retries=3,
    ) as client:
        yield Painter(client, settings.avatar_image_model, quality or settings.avatar_quality)


def ordered(frame_keys: Iterable[str]) -> list[str]:
    """The keys in the canonical order, which starts every parent before its children."""
    wanted = set(frame_keys)
    return [key for key in FRAME_KEYS if key in wanted]


async def paint_all(
    frame_keys: Iterable[str],
    sources: dict[str | None, bytes],
    paint: Callable[[str, bytes], Awaitable[bytes]],
    concurrency: int,
) -> dict[str, bytes | BaseException]:
    """Make the frames named, each from its parent: one made here, or one in `sources`
    (the photo under the base's parent, None). `paint` returns the frame's PNG. Returns
    each frame's PNG, or why it was not made: `Blocked` when its parent failed."""
    limit = asyncio.Semaphore(concurrency)
    tasks: dict[str, asyncio.Task[bytes]] = {}

    async def make(frame_key: str) -> bytes:
        parent = parent_of(frame_key)
        if parent in tasks:
            try:
                source = await tasks[parent]
            except BaseException as exc:
                raise Blocked(parent) from exc
        elif parent in sources:
            source = sources[parent]
        else:
            raise Blocked(parent)  # neither made before nor asked for now
        async with limit:
            return await paint(frame_key, source)

    for frame_key in ordered(frame_keys):
        tasks[frame_key] = asyncio.create_task(make(frame_key))
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    return dict(zip(tasks, results, strict=True))
