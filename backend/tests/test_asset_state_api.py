"""`GET /v1/me` reports the account's voice and avatar, read from the database each time,
and opens Avatar mode only when both of the account's own are active (MODE-004)."""

import asyncio
import uuid

import pytest

from tests.api import AUTH
from tests.database import run_as_migrator

pytestmark = pytest.mark.usefixtures("members")

ADD_VOICE = (
    "INSERT INTO app.voice_versions (voice_id, owner_id, status, sample_seconds, "
    "provider_voice_id, started_at, finished_at) "
    "VALUES ($1, $2, $3, 90, $4, now(), CASE WHEN $3 IN ('queued', 'running') THEN NULL "
    "ELSE now() END)"
)
ADD_AVATAR = (
    "INSERT INTO app.avatar_versions (avatar_id, owner_id, status, expression_schema_version, "
    "prompt_version, prompt_hash, image_model, quality, started_at, finished_at, error_code) "
    "VALUES ($1, $2, $3, 1, 1, 'hash', 'gpt-image-2.5-sunburst', 'medium', now(), "
    "CASE WHEN $3 IN ('queued', 'running') THEN NULL ELSE now() END, $4)"
)
NOTHING = {"active": None, "candidate": None}


def voice(owner: str, status: str) -> str:
    voice_id = uuid.uuid4()
    provider = f"provider-{voice_id}" if status in ("ready", "active", "retired") else None
    asyncio.run(run_as_migrator(ADD_VOICE, voice_id, owner, status, provider))
    return str(voice_id)


def avatar(owner: str, status: str, error_code: str | None = None) -> str:
    avatar_id = uuid.uuid4()
    asyncio.run(run_as_migrator(ADD_AVATAR, avatar_id, owner, status, error_code))
    return str(avatar_id)


def me(client) -> dict:
    response = client.get("/v1/me", headers=AUTH)
    assert response.status_code == 200
    return response.json()


def test_a_new_account_has_neither(client, as_member):
    as_member("uid-a")

    body = me(client)

    assert (body["voice"], body["avatar"], body["avatar_mode"]) == (NOTHING, NOTHING, False)


def test_avatar_mode_needs_both_active(client, as_member):
    as_member("uid-a")
    me(client)  # registers the account the versions belong to
    active_voice = voice("uid-a", "active")
    making = avatar("uid-a", "running")

    body = me(client)

    assert body["voice"] == {
        "active": {"id": active_voice, "status": "active", "error_code": None},
        "candidate": None,
    }
    assert body["avatar"] == {
        "active": None,
        "candidate": {"id": making, "status": "running", "error_code": None},
    }
    assert body["avatar_mode"] is False


def test_the_active_ones_stay_in_use_while_a_replacement_fails(client, as_member):
    as_member("uid-a")
    me(client)
    active_voice = voice("uid-a", "active")
    active_avatar = avatar("uid-a", "active")
    failed = avatar("uid-a", "failed", "IMAGE_REJECTED")

    body = me(client)

    assert body["avatar_mode"] is True
    assert body["voice"]["active"]["id"] == active_voice
    assert body["avatar"] == {
        "active": {"id": active_avatar, "status": "active", "error_code": None},
        "candidate": {"id": failed, "status": "failed", "error_code": "IMAGE_REJECTED"},
    }


def test_retired_and_deleted_versions_are_not_offered(client, as_member):
    as_member("uid-a")
    me(client)
    voice("uid-a", "retired")
    voice("uid-a", "deleted")
    avatar("uid-a", "deleted")

    body = me(client)

    assert (body["voice"], body["avatar"], body["avatar_mode"]) == (NOTHING, NOTHING, False)


def test_another_account_s_assets_are_not_reported(client, as_member):
    as_member("uid-b")
    me(client)
    voice("uid-b", "active")
    avatar("uid-b", "active")
    as_member("uid-a")

    body = me(client)

    assert (body["voice"], body["avatar"], body["avatar_mode"]) == (NOTHING, NOTHING, False)
