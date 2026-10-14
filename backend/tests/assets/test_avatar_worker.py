"""The worker paints an avatar's frames as its own role, with the image model faked: all
21 make it ready, a failure keeps what was made, and a retry paints only what is missing
(DEC-086, AC-AVA-01)."""

import io
import uuid

import pytest
from PIL import Image

from tests.database import run_as_migrator
from wobot.assets.avatar import FrameFailed, Painted
from wobot.assets.frames import BASE, FRAME_KEYS, parent_of
from wobot.assets.media import avatar_photo_key, avatar_prefix
from wobot.assets.worker import Worker

ADD_AVATAR = (
    "INSERT INTO app.avatar_versions (avatar_id, owner_id, status, expression_schema_version, "
    "prompt_version, prompt_hash, image_model, quality) "
    "VALUES ($1, $2, 'queued', 1, 1, 'hash', 'gpt-image-test', 'medium')"
)
ADD_FRAMES = "INSERT INTO app.avatar_frames (avatar_id, frame_key) SELECT $1, unnest($2::text[])"


def frame_png(key: str) -> bytes:
    """A distinct 1024-pixel PNG per frame, so each has its own hash."""
    out = io.BytesIO()
    Image.new("RGB", (1024, 1024), (FRAME_KEYS.index(key) * 10, 0, 0)).save(out, "PNG")
    return out.getvalue()


class FakePainter:
    def __init__(self, failing: dict[str, str] | None = None) -> None:
        self.painted: list[tuple[str, bytes]] = []
        self.failing = failing or {}

    async def paint(self, frame_key: str, source: bytes) -> Painted:
        self.painted.append((frame_key, source))
        if frame_key in self.failing:
            raise FrameFailed(self.failing[frame_key])
        return Painted(frame_png(frame_key), 200, 1100, 1056, 0.1)


async def queued_avatar(media, owner: str) -> uuid.UUID:
    avatar_id = uuid.uuid4()
    await media.put(avatar_photo_key(avatar_id), b"photo", "image/png")
    await run_as_migrator(ADD_AVATAR, avatar_id, owner)
    await run_as_migrator(ADD_FRAMES, avatar_id, list(FRAME_KEYS))
    return avatar_id


async def version(avatar_id: uuid.UUID) -> dict:
    rows = await run_as_migrator(
        "SELECT status, error_code, cleaned_at FROM app.avatar_versions WHERE avatar_id = $1",
        avatar_id,
    )
    return dict(rows[0])


async def frames(avatar_id: uuid.UUID) -> dict[str, dict]:
    rows = await run_as_migrator(
        "SELECT frame_key, status, storage_key, sha256, attempts, input_tokens, output_tokens, "
        "error_code FROM app.avatar_frames WHERE avatar_id = $1",
        avatar_id,
    )
    return {row["frame_key"]: dict(row) for row in rows}


def worker(sessionmaker, media, painter, execution: str = "e-1") -> Worker:
    return Worker(sessionmaker, media, None, execution, painter=painter, concurrency=14)


async def test_all_21_frames_make_the_avatar_ready_and_the_photo_goes(sessionmaker, owner, media):
    avatar_id = await queued_avatar(media, owner)
    painter = FakePainter()

    assert await worker(sessionmaker, media, painter).make_avatar(avatar_id) == "ready"

    assert (await version(avatar_id))["status"] == "ready"
    made = await frames(avatar_id)
    assert {f["status"] for f in made.values()} == {"ready"}
    joy = made["joy_half"]
    assert joy["storage_key"].startswith(f"{avatar_prefix(avatar_id)}joy_half-")
    assert await media.get(joy["storage_key"]) == frame_png("joy_half")
    assert (joy["attempts"], joy["input_tokens"], joy["output_tokens"]) == (1, 1300, 1056)
    # Each frame came from its own parent's picture; the base from the photo.
    sources = dict(painter.painted)
    assert sources[BASE] == b"photo"
    assert sources["joy_half"] == frame_png(parent_of("joy_half"))
    assert await media.get(avatar_photo_key(avatar_id)) is None


async def test_a_failed_frame_fails_the_avatar_and_keeps_what_was_made(sessionmaker, owner, media):
    avatar_id = await queued_avatar(media, owner)
    painter = FakePainter({"fear_closed": "IMAGE_REJECTED", "joy_wide": "PROVIDER_UNAVAILABLE"})

    assert await worker(sessionmaker, media, painter).make_avatar(avatar_id) == "failed"

    assert (await version(avatar_id))["error_code"] == "IMAGE_REJECTED"  # told first
    made = await frames(avatar_id)
    assert made["fear_closed"]["status"] == made["joy_wide"]["status"] == "failed"
    assert made["fear_half"]["status"] == made["fear_wide"]["status"] == "pending"  # blocked
    assert sum(f["status"] == "ready" for f in made.values()) == 17
    # The base was made, so the photo is gone even though the avatar failed.
    assert await media.get(avatar_photo_key(avatar_id)) is None


async def test_a_retry_paints_only_what_is_missing(sessionmaker, owner, media):
    avatar_id = await queued_avatar(media, owner)
    await worker(sessionmaker, media, FakePainter({"fear_closed": "IMAGE_REJECTED"})).make_avatar(
        avatar_id
    )
    # What POST /retry does: failed frames back to pending, the version queued.
    await run_as_migrator(
        "UPDATE app.avatar_frames SET status = 'pending', error_code = NULL "
        "WHERE avatar_id = $1 AND status = 'failed'",
        avatar_id,
    )
    await run_as_migrator(
        "UPDATE app.avatar_versions SET status = 'queued', finished_at = NULL WHERE avatar_id = $1",
        avatar_id,
    )
    painter = FakePainter()

    assert await worker(sessionmaker, media, painter, "e-2").make_avatar(avatar_id) == "ready"

    keys = [key for key, _ in painter.painted]
    assert keys[0] == "fear_closed"  # its two mouths, in either order, after it
    assert sorted(keys[1:]) == ["fear_half", "fear_wide"]
    assert dict(painter.painted)["fear_closed"] == frame_png(BASE)  # the stored base


async def test_without_the_photo_the_base_cannot_be_made(sessionmaker, owner, media):
    avatar_id = await queued_avatar(media, owner)
    await media.delete(avatar_photo_key(avatar_id))  # removed by the bucket's lifecycle

    await worker(sessionmaker, media, FakePainter()).make_avatar(avatar_id)

    assert (await version(avatar_id))["error_code"] == "PHOTO_MISSING"


async def test_a_base_that_fails_keeps_the_photo_for_a_retry(sessionmaker, owner, media):
    avatar_id = await queued_avatar(media, owner)

    await worker(sessionmaker, media, FakePainter({BASE: "PROVIDER_UNAVAILABLE"})).make_avatar(
        avatar_id
    )

    made = await frames(avatar_id)
    assert sum(f["status"] == "pending" for f in made.values()) == 20
    assert await media.get(avatar_photo_key(avatar_id)) == b"photo"


@pytest.mark.parametrize("status", ["retired", "deleted"])
async def test_cleaning_removes_an_unused_avatar_s_frames(sessionmaker, owner, media, status):
    avatar_id = await queued_avatar(media, owner)
    w = worker(sessionmaker, media, FakePainter())
    await w.make_avatar(avatar_id)
    stored = (await frames(avatar_id))["joy_half"]["storage_key"]
    await run_as_migrator(
        "UPDATE app.avatar_versions SET status = $2 WHERE avatar_id = $1", avatar_id, status
    )

    assert await w.clean_avatar(avatar_id) == "cleaned"

    assert await media.get(stored) is None
    assert (await version(avatar_id))["cleaned_at"] is not None


async def test_an_avatar_in_use_is_never_cleaned(sessionmaker, owner, media):
    avatar_id = await queued_avatar(media, owner)
    w = worker(sessionmaker, media, FakePainter())
    await w.make_avatar(avatar_id)
    await run_as_migrator(
        "UPDATE app.avatar_versions SET status = 'active' WHERE avatar_id = $1", avatar_id
    )

    assert await w.clean_avatar(avatar_id) == "nothing_to_clean"
