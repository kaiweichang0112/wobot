"""The avatar routes (D3): a photo is stored without its metadata and its 21 frames queued;
frames are read back by their hash; an avatar is put to use only with all 21 made, and
parts of it are made again before then (DEC-086, AC-AVA-02, AC-AVA-04, P9, P10)."""

import asyncio
import hashlib
import io
import uuid

import pytest
from PIL import Image

from tests.api import AUTH
from tests.database import run_as_migrator
from tests.test_voices_api import FakeDispatcher
from wobot.api.main import app
from wobot.assets.avatar_prompts import PROMPT_VERSION, prompt_hash
from wobot.assets.frames import BASE, FRAME_KEYS
from wobot.assets.media import LocalMediaStore, avatar_photo_key

pytestmark = pytest.mark.usefixtures("members")


def jpeg(size=(1600, 1200)) -> bytes:
    exif = Image.Exif()
    exif[0x8825] = {2: (25.0, 2.0, 0.0)}  # a GPS position, which must not be kept
    out = io.BytesIO()
    Image.new("RGB", size, "blue").save(out, format="JPEG", exif=exif)
    return out.getvalue()


@pytest.fixture
def assets(client, tmp_path):
    app.state.dispatcher = FakeDispatcher()
    app.state.media = LocalMediaStore(tmp_path)
    return app.state


def post(client, avatar_id: uuid.UUID, content: bytes | None = None):
    return client.post(
        "/v1/avatars",
        data={"avatar_id": str(avatar_id)},
        files={"photo": ("photo.jpg", content or jpeg(), "image/jpeg")},
        headers=AUTH,
    )


def migrate(sql: str, *args) -> list:
    return asyncio.run(run_as_migrator(sql, *args))


def created(client) -> uuid.UUID:
    avatar_id = uuid.uuid4()
    assert post(client, avatar_id).status_code == 202
    return avatar_id


def painted(assets, avatar_id: uuid.UUID, keys=FRAME_KEYS, status: str = "ready") -> None:
    """What the worker leaves: the frames stored and recorded, the version ended."""
    for key in keys:
        content = f"{avatar_id} {key}".encode()
        sha = hashlib.sha256(content).hexdigest()
        storage = f"avatars/{avatar_id}/{key}-{sha[:16]}.png"
        asyncio.run(assets.media.put(storage, content, "image/png"))
        migrate(
            "UPDATE app.avatar_frames SET status = 'ready', storage_key = $3, sha256 = $4 "
            "WHERE avatar_id = $1 AND frame_key = $2",
            avatar_id,
            key,
            storage,
            sha,
        )
    migrate(
        "UPDATE app.avatar_versions SET status = $2, started_at = now(), "
        "finished_at = CASE WHEN $2 = 'running' THEN NULL ELSE now() END WHERE avatar_id = $1",
        avatar_id,
        status,
    )


def status_of(avatar_id: uuid.UUID) -> str:
    return migrate("SELECT status FROM app.avatar_versions WHERE avatar_id = $1", avatar_id)[0][0]


def test_a_photo_is_stored_bare_and_its_frames_queued(client, as_member, assets):
    as_member("uid-a")
    avatar_id = uuid.uuid4()

    response = post(client, avatar_id)

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["quality"], body["ready_frames"]) == ("queued", "medium", 0)
    assert [f["key"] for f in body["frames"]] == list(FRAME_KEYS)
    assert {f["status"] for f in body["frames"]} == {"pending"}
    assert assets.dispatcher.started == [("make", "avatar", str(avatar_id))]
    stored = Image.open(io.BytesIO(asyncio.run(assets.media.get(avatar_photo_key(avatar_id)))))
    assert (stored.format, stored.size) == ("PNG", (1024, 768))
    assert not stored.getexif()
    [row] = migrate(
        "SELECT prompt_version, prompt_hash, expression_schema_version, image_model "
        "FROM app.avatar_versions WHERE avatar_id = $1",
        avatar_id,
    )
    assert tuple(row) == (PROMPT_VERSION, prompt_hash(), 1, "gpt-image-2.5-sunburst-2026-09-08")


@pytest.mark.parametrize(
    "content", [b"not an image", b"x" * (10 * 1024 * 1024 + 1)], ids=["not an image", "too large"]
)
def test_a_photo_must_be_an_image_of_at_most_10_mb(client, as_member, assets, content):
    as_member("uid-a")

    response = post(client, uuid.uuid4(), content)

    assert response.status_code == 422
    assert assets.dispatcher.started == []


def test_one_avatar_is_made_at_a_time(client, as_member, assets):
    as_member("uid-a")
    created(client)

    response = post(client, uuid.uuid4())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ASSET_IN_PROGRESS"


def test_progress_counts_the_frames_made(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id, [BASE, "joy_closed"], status="running")

    body = client.get(f"/v1/avatars/{avatar_id}", headers=AUTH).json()

    assert body["ready_frames"] == 2
    assert body["frames"][0]["sha256"] is not None


def test_a_lost_worker_fails_the_avatar_and_its_running_frames(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    migrate(
        "UPDATE app.avatar_versions SET status = 'running', started_at = now(), "
        "heartbeat_at = now() - interval '10 minutes' WHERE avatar_id = $1",
        avatar_id,
    )
    migrate(
        "UPDATE app.avatar_frames SET status = 'running' WHERE avatar_id = $1 AND frame_key = $2",
        avatar_id,
        BASE,
    )

    body = client.get(f"/v1/avatars/{avatar_id}", headers=AUTH).json()

    assert (body["status"], body["error_code"]) == ("failed", "WORKER_LOST")
    assert body["frames"][0]["error_code"] == "WORKER_LOST"


def test_a_frame_is_read_back_and_revalidated_by_its_hash(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id)
    path = f"/v1/avatars/{avatar_id}/frames/joy_half"

    response = client.get(path, headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == f"{avatar_id} joy_half".encode()
    etag = response.headers["etag"]
    assert etag == f'"{hashlib.sha256(response.content).hexdigest()}"'
    assert client.get(path, headers={**AUTH, "If-None-Match": etag}).status_code == 304


def test_a_frame_not_made_or_not_a_frame_does_not_exist(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)

    for key in ("joy_half", "happy_closed"):
        response = client.get(f"/v1/avatars/{avatar_id}/frames/{key}", headers=AUTH)
        assert response.status_code == 404, key


def test_an_avatar_short_of_one_frame_cannot_be_activated(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id, FRAME_KEYS[:-1])  # 20 of 21, the version marked ready

    response = client.post(f"/v1/avatars/{avatar_id}/activate", json={}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AVATAR_NOT_READY"


def test_an_avatar_of_another_expression_schema_cannot_be_activated(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id)
    migrate(
        "UPDATE app.avatar_versions SET expression_schema_version = 2 WHERE avatar_id = $1",
        avatar_id,
    )

    response = client.post(f"/v1/avatars/{avatar_id}/activate", json={}, headers=AUTH)

    assert response.json()["error"]["code"] == "AVATAR_NOT_READY"


def test_avatar_mode_opens_once_the_avatar_and_a_voice_are_active(client, as_member, assets):
    as_member("uid-a")
    client.get("/v1/me", headers=AUTH)
    migrate(
        "INSERT INTO app.voice_versions (voice_id, owner_id, status, sample_seconds, "
        "provider_voice_id, started_at, finished_at) "
        "VALUES (gen_random_uuid(), 'uid-a', 'active', 90, 'p', now(), now())"
    )
    first = created(client)
    painted(assets, first)

    assert client.post(f"/v1/avatars/{first}/activate", json={}, headers=AUTH).status_code == 200
    assert client.get("/v1/me", headers=AUTH).json()["avatar_mode"] is True

    second = created(client)
    painted(assets, second)
    response = client.post(
        f"/v1/avatars/{second}/activate", json={"expected_active": str(first)}, headers=AUTH
    )
    assert response.json()["status"] == "active"
    assert status_of(first) == "retired"
    assert assets.dispatcher.started[-1] == ("clean", "avatar", str(first))


def test_a_retry_queues_the_failed_frames_and_keeps_the_rest(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id, FRAME_KEYS[:18], status="failed")
    migrate(
        "UPDATE app.avatar_frames SET status = 'failed', error_code = 'IMAGE_REJECTED' "
        "WHERE avatar_id = $1 AND frame_key = 'thinking_closed'",
        avatar_id,
    )

    response = client.post(f"/v1/avatars/{avatar_id}/retry", headers=AUTH)

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["ready_frames"]) == ("queued", 18)
    assert {f["status"] for f in body["frames"][18:]} == {"pending"}
    assert assets.dispatcher.started[-1] == ("make", "avatar", str(avatar_id))


def test_only_a_failed_avatar_is_retried(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)

    response = client.post(f"/v1/avatars/{avatar_id}/retry", headers=AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ASSET_NOT_EDITABLE"


def test_redoing_a_frame_redoes_what_is_made_from_it(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id)

    response = client.post(f"/v1/avatars/{avatar_id}/frames/joy_closed/redo", headers=AUTH)

    assert response.status_code == 202
    body = response.json()
    pending = sorted(f["key"] for f in body["frames"] if f["status"] == "pending")
    assert (body["status"], pending) == ("queued", ["joy_closed", "joy_half", "joy_wide"])
    assert assets.dispatcher.started[-1] == ("make", "avatar", str(avatar_id))


def test_the_base_is_never_redone_and_an_avatar_in_use_never_changes(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id)

    base = client.post(f"/v1/avatars/{avatar_id}/frames/{BASE}/redo", headers=AUTH)
    assert base.status_code == 422

    client.post(f"/v1/avatars/{avatar_id}/activate", json={}, headers=AUTH)
    in_use = client.post(f"/v1/avatars/{avatar_id}/frames/joy_half/redo", headers=AUTH)
    assert in_use.json()["error"]["code"] == "ASSET_NOT_EDITABLE"


def test_another_account_s_avatar_does_not_exist(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id)
    as_member("uid-b")

    for method, path in [
        ("GET", ""),
        ("GET", "/frames/joy_half"),
        ("POST", "/retry"),
        ("POST", "/frames/joy_half/redo"),
        ("POST", "/activate"),
        ("DELETE", ""),
    ]:
        kwargs = {"json": {}} if path == "/activate" else {}
        response = client.request(method, f"/v1/avatars/{avatar_id}{path}", headers=AUTH, **kwargs)
        assert response.status_code == 404, (method, path)
    assert status_of(avatar_id) == "ready"


def test_deleting_an_avatar_removes_its_frames(client, as_member, assets):
    as_member("uid-a")
    avatar_id = created(client)
    painted(assets, avatar_id)

    assert client.delete(f"/v1/avatars/{avatar_id}", headers=AUTH).status_code == 204

    assert status_of(avatar_id) == "deleted"
    assert assets.dispatcher.started[-1] == ("clean", "avatar", str(avatar_id))
    gone = client.get(f"/v1/avatars/{avatar_id}/frames/joy_half", headers=AUTH)
    assert gone.status_code == 404
