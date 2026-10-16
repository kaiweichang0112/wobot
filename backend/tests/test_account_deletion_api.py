"""Deleting an account (E1, IAM-007, spec 04): it needs a recent sign-in, is refused
everything from the moment it is marked, takes its conversations and their memory at
once, hands its voices and avatars to the job to remove, and may be sent again."""

import asyncio
import threading
import time
import uuid

import pytest
from langchain_core.messages import AIMessage

from tests.agent.fakes import fake_models
from tests.api import AUTH, MEMBERS, send, sse_events, start
from tests.database import run_as_migrator
from tests.test_conversations_api import add_turns, checkpoints_of, save_checkpoint
from tests.test_turn_control_api import PausingChat
from tests.test_voices_api import assets, created, made, status_of  # noqa: F401
from wobot.agent.graph import Models
from wobot.api.auth import get_token_claims
from wobot.api.conversations import thread_of
from wobot.api.deletion import RECENT_SIGN_IN_SECONDS
from wobot.api.main import app

pytestmark = pytest.mark.usefixtures("members")


def sign_in(uid: str, seconds_ago: int = 0) -> None:
    """Signed in as the member, the seconds given ago."""
    claims = {
        "uid": uid,
        "email": MEMBERS[uid],
        "email_verified": True,
        "auth_time": int(time.time()) - seconds_ago,
    }
    app.dependency_overrides[get_token_claims] = lambda: claims


def delete_account(client):
    return client.delete("/v1/me", headers=AUTH)


def deleted_at(uid: str):
    rows = asyncio.run(
        run_as_migrator("SELECT deleted_at FROM app.accounts WHERE account_id = $1", uid)
    )
    return rows[0]["deleted_at"] if rows else None


def conversations_of(uid: str) -> int:
    rows = asyncio.run(
        run_as_migrator("SELECT count(*) AS n FROM app.conversations WHERE owner_id = $1", uid)
    )
    return rows[0]["n"]


def test_deleting_needs_a_recent_sign_in(client):
    sign_in("uid-a", seconds_ago=RECENT_SIGN_IN_SECONDS + 60)
    conversation = start(client)

    response = delete_account(client)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "REAUTH_REQUIRED"
    assert deleted_at("uid-a") is None
    assert conversations_of("uid-a") == 1
    assert client.get(f"/v1/conversations/{conversation}/turns", headers=AUTH).status_code == 200


def test_a_deleted_account_loses_its_conversations_and_is_refused(client):
    sign_in("uid-a")
    conversation = start(client)
    asyncio.run(add_turns(conversation, 2))
    asyncio.run(save_checkpoint(thread_of(uuid.UUID(conversation))))
    sign_in("uid-b")
    kept = start(client)
    sign_in("uid-a")

    response = delete_account(client)

    assert response.status_code == 202
    assert deleted_at("uid-a") is not None
    assert conversations_of("uid-a") == 0
    assert asyncio.run(checkpoints_of(conversation)) == 0
    for method, path in [("GET", "/v1/me"), ("POST", "/v1/conversations")]:
        refused = client.request(method, path, headers=AUTH)
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "ACCOUNT_DELETED"
    # The other account is untouched.
    sign_in("uid-b")
    assert client.get(f"/v1/conversations/{kept}/turns", headers=AUTH).status_code == 200


def test_deleting_may_be_sent_again_without_a_new_sign_in(client):
    sign_in("uid-a")
    assert delete_account(client).status_code == 202
    first = deleted_at("uid-a")

    # The app's first request was cut off and it tries again, much later.
    sign_in("uid-a", seconds_ago=RECENT_SIGN_IN_SECONDS + 60)

    assert delete_account(client).status_code == 202
    assert deleted_at("uid-a") == first


def test_an_account_never_seen_is_marked_too(client):
    sign_in("uid-a")
    assert deleted_at("uid-a") is None  # no row yet: the first request is this one

    assert delete_account(client).status_code == 202
    assert deleted_at("uid-a") is not None


def test_voices_are_taken_out_of_use_and_handed_to_the_job(client, assets):  # noqa: F811
    sign_in("uid-a")
    active = created(client)
    made(active)
    client.post(f"/v1/voices/{active}/activate", json={}, headers=AUTH)
    making = created(client)  # its replacement, still being cloned
    assets.dispatcher.started.clear()

    assert delete_account(client).status_code == 202

    assert (status_of(active), status_of(making)) == ("deleted", "deleted")
    assert sorted(assets.dispatcher.started) == sorted(
        [("clean", "voice", str(active)), ("clean", "voice", str(making))]
    )


def test_a_version_already_removed_is_not_handed_over_again(client, assets):  # noqa: F811
    sign_in("uid-a")
    voice_id = created(client)
    made(voice_id)
    asyncio.run(
        run_as_migrator(
            "UPDATE app.voice_versions SET status = 'retired', cleaned_at = now() "
            "WHERE voice_id = $1",
            voice_id,
        )
    )
    assets.dispatcher.started.clear()

    assert delete_account(client).status_code == 202
    assert assets.dispatcher.started == []


@pytest.mark.usefixtures("published_version")
def test_a_turn_running_when_the_account_is_deleted_stops_and_leaves_no_memory(client, chat_graph):
    sign_in("uid-a")
    stalled = PausingChat(messages=iter([AIMessage("第一個字 後面的字")]))
    chat_graph(Models(**{**vars(fake_models()), "chat": stalled}))
    conversation = start(client)
    streamed = {}

    def stream() -> None:
        streamed["response"] = send(client, conversation, "嗨")

    began = time.perf_counter()
    runner = threading.Thread(target=stream)
    runner.start()
    time.sleep(0.5)  # the first word is out and the model stalls
    assert delete_account(client).status_code == 202
    runner.join()

    assert time.perf_counter() - began < 5  # not the model's 10 s
    names = [name for name, _ in sse_events(streamed["response"].text)]
    assert "reply" not in names and "completed" not in names
    assert conversations_of("uid-a") == 0
    assert asyncio.run(checkpoints_of(conversation)) == 0
