"""The voice routes (D2): a sample is stored and its cloning started once, however often
the app sends it; a lost start is started again; one voice is in use at a time, and one
taken out of use is removed at ElevenLabs (DEC-086, VOI-013)."""

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.api import AUTH
from tests.database import run_as_migrator
from wobot.api import voices
from wobot.api.main import app
from wobot.assets.dispatch import DispatchError
from wobot.assets.media import LocalMediaStore, voice_sample_key

pytestmark = pytest.mark.usefixtures("members")

TONE = Path(__file__).parent / "assets" / "tone-1s.m4a"  # a real M4A, 1.15 s long
SAMPLE = b"a 90-second recording"


class FakeDispatcher:
    def __init__(self) -> None:
        self.started: list[tuple[str, ...]] = []
        self.fail = False

    async def dispatch(self, *args: str) -> str | None:
        if self.fail:
            raise DispatchError("HTTP 503")
        self.started.append(args)
        return f"execution-{len(self.started)}"


class FakeElevenLabs:
    def __init__(self) -> None:
        self.spoken: list[tuple[str, str, str | None]] = []

    async def speak(
        self, provider_voice_id: str, text: str, model: str, language: str | None = None
    ) -> bytes:
        self.spoken.append((provider_voice_id, model, language))
        return b"ID3 mp3"


@pytest.fixture
def assets(client, tmp_path, monkeypatch):
    """The app's job, media and ElevenLabs, faked. Samples are judged by their bytes:
    SAMPLE passes as 90 s, anything else is read for real."""
    real = voices.sample_seconds
    monkeypatch.setattr(voices, "sample_seconds", lambda c: 90.0 if c == SAMPLE else real(c))
    app.state.dispatcher = FakeDispatcher()
    app.state.media = LocalMediaStore(tmp_path)
    app.state.eleven = FakeElevenLabs()
    return app.state


def post(client, voice_id: uuid.UUID, content: bytes = SAMPLE):
    return client.post(
        "/v1/voices",
        data={"voice_id": str(voice_id)},
        files={"sample": ("sample.m4a", content, "audio/mp4")},
        headers=AUTH,
    )


def migrate(sql: str, *args) -> list:
    return asyncio.run(run_as_migrator(sql, *args))


def made(voice_id: uuid.UUID) -> None:
    """What the worker does once the clone exists."""
    migrate(
        "UPDATE app.voice_versions SET status = 'ready', provider_voice_id = $2, "
        "started_at = now(), finished_at = now() WHERE voice_id = $1",
        voice_id,
        f"provider-{voice_id}",
    )


def created(client, voice_id: uuid.UUID | None = None) -> uuid.UUID:
    voice_id = voice_id or uuid.uuid4()
    assert post(client, voice_id).status_code == 202
    return voice_id


def status_of(voice_id: uuid.UUID) -> str:
    return migrate("SELECT status FROM app.voice_versions WHERE voice_id = $1", voice_id)[0][0]


def test_a_sample_is_stored_and_its_cloning_started(client, as_member, assets):
    as_member("uid-a")
    voice_id = uuid.uuid4()

    response = post(client, voice_id)

    assert response.status_code == 202
    body = response.json()
    assert (body["voice_id"], body["status"], body["sample_seconds"]) == (
        str(voice_id),
        "queued",
        90.0,
    )
    assert assets.dispatcher.started == [("make", "voice", str(voice_id))]
    assert asyncio.run(assets.media.get(voice_sample_key(voice_id))) == SAMPLE
    execution = migrate("SELECT execution FROM app.voice_versions WHERE voice_id = $1", voice_id)[
        0
    ][0]
    assert execution == "execution-1"


def test_a_sample_sent_again_returns_the_same_version(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)

    response = post(client, voice_id)

    assert response.status_code == 202
    assert response.json()["voice_id"] == str(voice_id)
    assert len(assets.dispatcher.started) == 1


def test_another_account_cannot_take_a_voice_id(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)
    as_member("uid-b")

    response = post(client, voice_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ASSET_EXISTS"


def test_one_voice_is_made_at_a_time(client, as_member, assets):
    as_member("uid-a")
    created(client)

    response = post(client, uuid.uuid4())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ASSET_IN_PROGRESS"


@pytest.mark.parametrize(
    "content",
    [TONE.read_bytes(), b"not audio", b"x" * (voices.SAMPLE_MAX_BYTES + 1)],
    ids=["too short", "not audio", "too large"],
)
def test_a_sample_must_be_an_m4a_recording_of_one_to_three_minutes(
    client, as_member, assets, content
):
    as_member("uid-a")

    response = post(client, uuid.uuid4(), content)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert assets.dispatcher.started == []


def test_the_real_reader_measures_an_m4a():
    assert voices.sample_seconds(TONE.read_bytes()) == pytest.approx(1.15, abs=0.05)
    assert voices.sample_seconds(b"not audio") is None


def test_a_lost_start_is_started_again_by_one_read(client, as_member, assets):
    as_member("uid-a")
    assets.dispatcher.fail = True
    voice_id = created(client)  # still accepted: the version waits, queued
    assets.dispatcher.fail = False
    assert client.get(f"/v1/voices/{voice_id}", headers=AUTH).json()["status"] == "queued"
    assert assets.dispatcher.started == []  # too soon to call it lost

    migrate(
        "UPDATE app.voice_versions SET updated_at = now() - interval '2 minutes' "
        "WHERE voice_id = $1",
        voice_id,
    )
    client.get(f"/v1/voices/{voice_id}", headers=AUTH)
    client.get(f"/v1/voices/{voice_id}", headers=AUTH)

    assert assets.dispatcher.started == [("make", "voice", str(voice_id))]


def test_a_voice_whose_worker_stopped_beating_fails(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)
    migrate(
        "UPDATE app.voice_versions SET status = 'running', started_at = now(), "
        "heartbeat_at = now() - interval '10 minutes' WHERE voice_id = $1",
        voice_id,
    )

    body = client.get(f"/v1/voices/{voice_id}", headers=AUTH).json()

    assert (body["status"], body["error_code"]) == ("failed", "WORKER_LOST")


def test_another_account_s_voice_does_not_exist(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)
    made(voice_id)
    as_member("uid-b")

    for method, path in [
        ("GET", ""),
        ("POST", "/preview"),
        ("POST", "/activate"),
        ("DELETE", ""),
    ]:
        kwargs = {"json": {}} if path == "/activate" else {}
        response = client.request(method, f"/v1/voices/{voice_id}{path}", headers=AUTH, **kwargs)
        assert response.status_code == 404, (method, path)
    assert status_of(voice_id) == "ready"


def test_a_ready_voice_is_previewed_in_itself(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)
    made(voice_id)

    response = client.post(f"/v1/voices/{voice_id}/preview", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    # Mandarin, told: the model also speaks Cantonese.
    assert assets.eleven.spoken == [(f"provider-{voice_id}", "eleven_v4_turbo", "zh")]


def test_a_voice_being_made_has_no_preview(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)

    response = client.post(f"/v1/voices/{voice_id}/preview", headers=AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VOICE_NOT_READY"


def test_activating_a_voice_retires_and_removes_the_one_in_use(client, as_member, assets):
    as_member("uid-a")
    first = created(client)
    made(first)
    activated = client.post(f"/v1/voices/{first}/activate", json={}, headers=AUTH)
    assert activated.json()["status"] == "active"
    second = created(client)
    made(second)

    response = client.post(
        f"/v1/voices/{second}/activate", json={"expected_active": str(first)}, headers=AUTH
    )

    assert response.status_code == 200
    assert (status_of(first), status_of(second)) == ("retired", "active")
    assert assets.dispatcher.started[-1] == ("clean", "voice", str(first))
    me = client.get("/v1/me", headers=AUTH).json()
    assert me["voice"]["active"]["id"] == str(second)


def test_activating_against_a_stale_view_is_refused(client, as_member, assets):
    as_member("uid-a")
    first = created(client)
    made(first)
    client.post(f"/v1/voices/{first}/activate", json={}, headers=AUTH)
    second = created(client)
    made(second)

    # The app still thinks no voice is in use: another device activated one meanwhile.
    response = client.post(f"/v1/voices/{second}/activate", json={}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVISION_CONFLICT"
    assert (status_of(first), status_of(second)) == ("active", "ready")


def test_a_voice_being_made_cannot_be_activated(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)

    response = client.post(f"/v1/voices/{voice_id}/activate", json={}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VOICE_NOT_READY"


def test_deleting_the_voice_in_use_ends_avatar_mode_and_removes_it(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)
    made(voice_id)
    client.post(f"/v1/voices/{voice_id}/activate", json={}, headers=AUTH)

    response = client.delete(f"/v1/voices/{voice_id}", headers=AUTH)

    assert response.status_code == 204
    assert status_of(voice_id) == "deleted"
    assert assets.dispatcher.started[-1] == ("clean", "voice", str(voice_id))
    assert client.get("/v1/me", headers=AUTH).json()["voice"] == {
        "active": None,
        "candidate": None,
    }
    # Sent again before the removal finished, it starts the removal again.
    assert client.delete(f"/v1/voices/{voice_id}", headers=AUTH).status_code == 204
    assert assets.dispatcher.started[-2:] == [("clean", "voice", str(voice_id))] * 2


def test_a_voice_being_made_can_be_deleted(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)

    assert client.delete(f"/v1/voices/{voice_id}", headers=AUTH).status_code == 204
    assert status_of(voice_id) == "deleted"
    created(client)  # no longer being made: another may start


def test_without_elevenlabs_a_preview_is_unavailable(client, as_member, assets):
    as_member("uid-a")
    voice_id = created(client)
    made(voice_id)
    assets.eleven = None

    response = client.post(f"/v1/voices/{voice_id}/preview", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"
