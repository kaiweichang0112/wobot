"""Conversations and settings over the API (C2): each account sees only its own, pages
never repeat or skip, a rename cannot silently overwrite another device's, and deleting
a conversation leaves nothing of it in either store."""

import asyncio
import uuid

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from tests.api import AUTH, start
from tests.database import run_as_migrator
from wobot.agent.checkpoints import open_checkpointer
from wobot.api.conversations import thread_of
from wobot.config import get_settings


async def add_turns(conversation_id: str, count: int) -> None:
    for seq in range(1, count + 1):
        await run_as_migrator(
            "INSERT INTO app.turns (turn_id, conversation_id, seq, user_text, status, "
            "app_version, finished_at) VALUES ($1, $2, $3, $4, 'completed', 'test', now())",
            uuid.uuid4(),
            uuid.UUID(conversation_id),
            seq,
            f"question {seq}",
        )


async def save_checkpoint(conversation_id: str) -> None:
    config = {"configurable": {"thread_id": conversation_id, "checkpoint_ns": ""}}
    async with open_checkpointer(get_settings()) as saver:
        await saver.aput(config, empty_checkpoint(), {}, {})


async def checkpoints_of(conversation_id: str) -> int:
    rows = await run_as_migrator(
        "SELECT count(*) AS n FROM app.checkpoints WHERE thread_id = $1", conversation_id
    )
    return rows[0]["n"]


def test_an_account_sees_only_its_own_conversations(client, members, as_member):
    as_member("uid-a")
    mine = start(client)

    as_member("uid-b")
    assert client.get("/v1/conversations", headers=AUTH).json()["conversations"] == []
    # Another account's conversation is answered as one that does not exist.
    for response in (
        client.get(f"/v1/conversations/{mine}/turns", headers=AUTH),
        client.delete(f"/v1/conversations/{mine}", headers=AUTH),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    as_member("uid-a")
    listed = client.get("/v1/conversations", headers=AUTH).json()["conversations"]
    assert [c["conversation_id"] for c in listed] == [mine]


def test_conversations_are_paged_most_recent_first(client, members, as_member):
    as_member("uid-a")
    started = [start(client) for _ in range(3)]

    first = client.get("/v1/conversations?limit=2", headers=AUTH).json()
    rest = client.get(f"/v1/conversations?limit=2&cursor={first['next_cursor']}", headers=AUTH)

    pages = [c["conversation_id"] for c in first["conversations"] + rest.json()["conversations"]]
    assert pages == started[::-1]
    assert rest.json()["next_cursor"] is None


def test_turns_read_oldest_first_and_page_backwards(client, members, as_member):
    as_member("uid-a")
    conversation = start(client)
    asyncio.run(add_turns(conversation, 3))

    latest = client.get(f"/v1/conversations/{conversation}/turns?limit=2", headers=AUTH).json()
    earlier = client.get(
        f"/v1/conversations/{conversation}/turns?limit=2&cursor={latest['next_cursor']}",
        headers=AUTH,
    ).json()

    assert [t["seq"] for t in latest["turns"]] == [2, 3]
    assert [t["user_text"] for t in earlier["turns"]] == ["question 1"]
    assert earlier["next_cursor"] is None


def test_deleting_a_conversation_leaves_nothing_in_either_store(client, members, as_member):
    as_member("uid-a")
    conversation = start(client)
    asyncio.run(add_turns(conversation, 2))
    asyncio.run(save_checkpoint(thread_of(uuid.UUID(conversation))))
    assert asyncio.run(checkpoints_of(conversation)) == 1

    response = client.delete(f"/v1/conversations/{conversation}", headers=AUTH)

    assert response.status_code == 204
    assert asyncio.run(checkpoints_of(conversation)) == 0
    turns = asyncio.run(
        run_as_migrator(
            "SELECT count(*) AS n FROM app.turns WHERE conversation_id = $1",
            uuid.UUID(conversation),
        )
    )
    assert turns[0]["n"] == 0
    # Gone: deleting again finds nothing.
    assert client.delete(f"/v1/conversations/{conversation}", headers=AUTH).status_code == 404


def test_a_rename_takes_the_version_the_client_last_read(client, members, as_member):
    as_member("uid-a")
    assert client.get("/v1/me", headers=AUTH).json()["settings"] == {
        "chatbot_name": "Wobot",
        "version": 0,
    }

    first = client.patch(
        "/v1/me/settings", json={"chatbot_name": "  小幫手 ", "expected_version": 0}, headers=AUTH
    )
    # A second device that read version 0 too may not overwrite it unseen.
    stale = client.patch(
        "/v1/me/settings", json={"chatbot_name": "Robo", "expected_version": 0}, headers=AUTH
    )
    second = client.patch(
        "/v1/me/settings", json={"chatbot_name": "Robo", "expected_version": 1}, headers=AUTH
    )

    assert first.json() == {"chatbot_name": "小幫手", "version": 1}
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert second.json() == {"chatbot_name": "Robo", "version": 2}
    # Each account names its own assistant.
    as_member("uid-b")
    assert client.get("/v1/me", headers=AUTH).json()["settings"]["chatbot_name"] == "Wobot"


@pytest.mark.parametrize("name", ["", "   ", "x" * 41, "two\nlines", "tab\tin"])
def test_an_invalid_name_is_a_field_error(client, members, as_member, name):
    as_member("uid-a")

    response = client.patch(
        "/v1/me/settings", json={"chatbot_name": name, "expected_version": 0}, headers=AUTH
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert "chatbot name" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "path",
    ["/v1/conversations?limit=0", "/v1/conversations?cursor=nope", "/v1/conversations/x/turns"],
)
def test_a_malformed_request_uses_the_error_envelope(client, members, as_member, path):
    as_member("uid-a")

    response = client.get(path, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_conversations_require_a_token(client):
    assert client.get("/v1/conversations").status_code == 401


def test_conversations_require_an_allowlisted_account(client, sign_in):
    sign_in("uid-stranger", "stranger@example.com")

    assert client.post("/v1/conversations", headers=AUTH).status_code == 403
