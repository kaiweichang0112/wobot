"""A turn over the API (C3): saved as running before the stream opens, its reply
streamed as server-sent events, and the conversation's memory moved on only when it
completes (DEC-079, DEC-080)."""

import asyncio
import json
import uuid

import pytest

from tests.agent.fakes import fake_models
from tests.api import failing_chat, send, sse_events, start, turn_row
from tests.database import run_as_migrator
from wobot.agent.graph import Models, build_graph
from wobot.api.main import app

pytestmark = pytest.mark.usefixtures("members", "published_version")


def test_a_chat_reply_streams_word_by_word_then_completes(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models(chat=("你好 我是 Wobot",)))
    conversation = start(client)
    turn_id = uuid.uuid4()

    response = send(client, conversation, "嗨", turn_id)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "accepted" and names[1] == "status"
    assert names[-2:] == ["reply", "completed"]
    assert events[1][1]["stage"] == "writing"
    deltas = [data["text"] for name, data in events if name == "text_delta"]
    assert len(deltas) > 1  # word by word, not at once
    assert "".join(deltas) == events[-2][1]["text"] == "你好 我是 Wobot"
    assert all(data["turn_id"] == str(turn_id) for _, data in events)
    assert "你好" in response.text  # sent as UTF-8, not as \u escapes
    row = turn_row(turn_id)
    assert (row["status"], row["reply_text"], row["route"]) == (
        "completed",
        "你好 我是 Wobot",
        "chat",
    )
    assert row["head_checkpoint_id"] is not None


def test_the_next_turn_remembers_only_completed_turns(client, as_member, chat_graph):
    as_member("uid-a")
    models = chat_graph(fake_models(chat=("第一個回答", "第三個回答")))
    conversation = start(client)
    send(client, conversation, "第一個問題")

    # The second turn's model fails: the turn fails, and the head stays where it was.
    app.state.graph = build_graph(
        Models(**{**vars(models), "chat": failing_chat()}),
        app.state.engine,
        None,
        app.state.checkpointer,
    )
    failed_id = uuid.uuid4()
    failed = sse_events(send(client, conversation, "第二個問題", failed_id).text)
    app.state.graph = build_graph(models, app.state.engine, None, app.state.checkpointer)
    send(client, conversation, "第三個問題")

    assert [name for name, _ in failed][-2:] == ["reply", "error"]
    assert failed[-1][1] == {
        "turn_id": str(failed_id),
        "attempt": 1,
        "code": "PROVIDER_UNAVAILABLE",
        "message": "The reply could not be written just now. Try again.",
        "retryable": True,
    }
    assert turn_row(failed_id)["status"] == "failed"
    # What the router saw on the third turn: the first exchange and the new question,
    # nothing of the failed one.
    seen = [m.text for m in models.router.calls[-1][0]]
    assert seen == ["第一個問題", "第一個回答", "第三個問題"]


def test_a_first_turn_that_failed_leaves_no_memory(client, as_member, chat_graph):
    as_member("uid-a")
    models = fake_models(chat=("回答",))
    chat_graph(Models(**{**vars(models), "chat": failing_chat()}))
    conversation = start(client)
    send(client, conversation, "失敗的問題")

    chat_graph(models)
    send(client, conversation, "新的問題")

    assert [m.text for m in models.router.calls[-1][0]] == ["新的問題"]


def test_a_conversation_writes_one_reply_at_a_time(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models())
    conversation = start(client)
    asyncio.run(
        run_as_migrator(
            "INSERT INTO app.turns (turn_id, conversation_id, seq, user_text, status, "
            "app_version) VALUES ($1, $2, 1, '另一台裝置', 'running', 'test')",
            uuid.uuid4(),
            uuid.UUID(conversation),
        )
    )

    response = send(client, conversation, "嗨")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TURN_IN_PROGRESS"


def test_a_message_sent_again_is_not_answered_twice(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models())
    conversation = start(client)
    turn_id = uuid.uuid4()
    send(client, conversation, "嗨", turn_id)

    again = send(client, conversation, "嗨", turn_id)

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "TURN_EXISTS"


@pytest.mark.parametrize("text", ["", "   ", "x" * 2001])
def test_a_message_has_a_length(client, as_member, chat_graph, text):
    as_member("uid-a")
    chat_graph(fake_models())

    response = send(client, start(client), text)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_another_account_s_conversation_takes_no_turns(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models())
    conversation = start(client)

    as_member("uid-b")
    response = send(client, conversation, "嗨")

    assert response.status_code == 404


def test_without_a_graph_chat_is_unavailable(client, as_member):
    as_member("uid-a")

    response = send(client, start(client), "嗨")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"


def test_a_list_reply_keeps_its_record_keys_off_the_wire(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models(route="list"))
    turn_id = uuid.uuid4()

    events = sse_events(send(client, start(client), "列出演講", turn_id).text)

    [reply] = [data for name, data in events if name == "reply"]
    assert "lists" not in reply
    kept = asyncio.run(run_as_migrator("SELECT reply FROM app.turns WHERE turn_id = $1", turn_id))
    assert "lists" in json.loads(kept[0]["reply"])  # kept for the avatar (Phase D)
