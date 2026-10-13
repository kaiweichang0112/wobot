"""Stopping, retrying and ending turns (C5). Every end is a compare-and-swap on the
turn's ID, attempt and status, so a stop, a reply that completes late, a time limit, a
lost request or a deleted conversation each leave one outcome (DEC-079)."""

import asyncio
import json
import threading
import time
import uuid
from functools import partial

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.base import empty_checkpoint

from tests.agent.fakes import fake_chat, fake_models
from tests.api import AUTH, failing_chat, send, sse_events, start, turn_row
from tests.database import run_as_migrator
from wobot.agent.graph import Models
from wobot.api import turns
from wobot.api.main import app
from wobot.api.turns import Started, finish

pytestmark = pytest.mark.usefixtures("members", "published_version")


def running_turn(conversation: str, seq: int = 1, minutes_ago: int = 0) -> uuid.UUID:
    """A turn another request is running, started the minutes ago given."""
    turn_id = uuid.uuid4()
    asyncio.run(
        run_as_migrator(
            "INSERT INTO app.turns (turn_id, conversation_id, seq, user_text, status, "
            "app_version, started_at) VALUES ($1, $2, $3, '問題', 'running', 'test', "
            "now() - make_interval(mins => $4))",
            turn_id,
            uuid.UUID(conversation),
            seq,
            minutes_ago,
        )
    )
    return turn_id


def started(turn_id: uuid.UUID, conversation: str) -> Started:
    return Started(turn_id, uuid.UUID(conversation), 1, "問題", "Wobot", 1, None)


def end(client, turn: Started, status: str, text: str | None, checkpoint_id: str | None = None):
    """Ends the turn as the request running it would, on the app's event loop."""
    return client.portal.call(
        partial(finish, app.state, turn, status, text, checkpoint_id=checkpoint_id)
    )


def cancel(client, turn_id: uuid.UUID, attempt: int = 1):
    return client.post(f"/v1/turns/{turn_id}/cancel", json={"attempt": attempt}, headers=AUTH)


def retry(client, turn_id: uuid.UUID, expected_attempt: int = 1):
    return client.post(
        f"/v1/turns/{turn_id}/retry", json={"expected_attempt": expected_attempt}, headers=AUTH
    )


def slow_chat(seconds: float, reply: str = "慢慢的回答") -> RunnableLambda:
    """A chat model that takes the seconds given to answer."""

    async def answer(_messages):
        await asyncio.sleep(seconds)
        return AIMessage(reply)

    return RunnableLambda(answer)


class PausingChat(type(fake_chat())):
    """A chat model that streams its first word, then stalls."""

    async def _astream(self, *args, **kwargs):
        first = True
        async for chunk in super()._astream(*args, **kwargs):
            yield chunk
            if first:
                first = False
                await asyncio.sleep(10)


def test_a_client_that_disconnects_stops_the_turn(client, as_member, chat_graph):
    # Driven as uvicorn drives the app (ASGI 2.3): Starlette listens for the disconnect
    # while the reply streams, so the turn is stopped at once, not at the next write.
    as_member("uid-a")
    stalled = PausingChat(messages=iter([AIMessage("第一個字 後面的字")]))
    chat_graph(Models(**{**vars(fake_models()), "chat": stalled}))
    conversation = start(client)
    turn_id = uuid.uuid4()
    body = json.dumps({"turn_id": str(turn_id), "text": "嗨"}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/v1/conversations/{conversation}/turns",
        "raw_path": f"/v1/conversations/{conversation}/turns".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"authorization", AUTH["Authorization"].encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("test", 1),
        "server": ("test", 80),
    }

    async def play() -> bytes:
        received = bytearray()
        word_sent = asyncio.Event()
        request = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if request:
                return request.pop()
            await word_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            received.extend(message.get("body", b""))
            if b"text_delta" in received:
                word_sent.set()

        await app(scope, receive, send)
        return bytes(received)

    began = time.perf_counter()
    sent = client.portal.call(play)

    assert time.perf_counter() - began < 5
    assert [name for name, _ in sse_events(sent.decode())][-1] == "text_delta"
    row = turn_row(turn_id)
    assert (row["status"], row["reply_text"], row["head_checkpoint_id"]) == (
        "cancelled",
        "第一個字",
        None,
    )


@pytest.mark.parametrize(
    ("app_sends", "kept"), [("第一個", "第一個"), (None, "第一個字")], ids=["app", "stream"]
)
def test_a_stop_the_stream_never_hears_of_still_stops_the_turn(
    client, as_member, chat_graph, app_sends, kept
):
    # As on Cloud Run: the app's closing its stream does not reach the instance, so the
    # stop is only the cancel request, which the running stream reads back.
    as_member("uid-a")
    stalled = PausingChat(messages=iter([AIMessage("第一個字 後面的字")]))
    chat_graph(Models(**{**vars(fake_models()), "chat": stalled}))
    conversation = start(client)
    turn_id = uuid.uuid4()
    streamed = {}

    def stream() -> None:
        streamed["response"] = send(client, conversation, "嗨", turn_id)

    began = time.perf_counter()
    runner = threading.Thread(target=stream)
    runner.start()
    time.sleep(0.5)  # the first word is out and the model stalls
    stopped = client.post(
        f"/v1/turns/{turn_id}/cancel",
        json={"attempt": 1, "shown_text": app_sends},
        headers=AUTH,
    )
    runner.join()

    assert stopped.json()["status"] == "cancelled"
    assert time.perf_counter() - began < 5  # not the model's 10 s
    names = [name for name, _ in sse_events(streamed["response"].text)]
    assert names[0] == "accepted" and "text_delta" in names
    assert "reply" not in names and "completed" not in names
    row = turn_row(turn_id)
    assert (row["status"], row["reply_text"], row["head_checkpoint_id"]) == (
        "cancelled",
        kept,
        None,
    )


def test_a_stop_holds_against_a_reply_that_completes_after_it(client, as_member, chat_graph):
    as_member("uid-a")
    conversation = start(client)
    turn_id = running_turn(conversation)

    stopped = cancel(client, turn_id)
    late = end(client, started(turn_id, conversation), "completed", "完整的回答", "late")

    assert stopped.status_code == 200
    assert stopped.json() == {"turn_id": str(turn_id), "attempt": 1, "status": "cancelled"}
    assert late is False
    row = turn_row(turn_id)
    assert (row["status"], row["reply_text"], row["head_checkpoint_id"]) == (
        "cancelled",
        None,
        None,
    )


@pytest.mark.parametrize("app_first", [True, False])
def test_a_stopped_turn_keeps_the_words_already_sent(client, as_member, app_first):
    # The app sends the stop and closes its stream; either may reach the server first.
    as_member("uid-a")
    conversation = start(client)
    turn_id = running_turn(conversation)

    if app_first:
        cancel(client, turn_id)
    end(client, started(turn_id, conversation), "cancelled", "已經送出的字")
    if not app_first:
        assert cancel(client, turn_id).json()["status"] == "cancelled"

    row = turn_row(turn_id)
    assert (row["status"], row["reply_text"]) == ("cancelled", "已經送出的字")


def test_a_stop_sent_again_or_too_late_changes_nothing(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models(chat=("回答",)))
    conversation = start(client)
    completed_id = uuid.uuid4()
    send(client, conversation, "第一題", completed_id)
    running_id = running_turn(conversation, seq=2)

    cancel(client, running_id)
    again = cancel(client, running_id)
    too_late = cancel(client, completed_id)

    assert again.json()["status"] == "cancelled"
    assert too_late.status_code == 200
    assert too_late.json() == {"turn_id": str(completed_id), "attempt": 1, "status": "completed"}
    assert turn_row(completed_id)["reply_text"] == "回答"


def test_another_account_cannot_stop_or_retry_a_turn(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models())
    turn_id = running_turn(start(client))

    as_member("uid-b")

    assert cancel(client, turn_id).status_code == 404
    assert retry(client, turn_id).status_code == 404
    assert turn_row(turn_id)["status"] == "running"


def test_a_failed_reply_is_written_again_without_asking_twice(client, as_member, chat_graph):
    as_member("uid-a")
    models = chat_graph(fake_models(chat=("第一個回答", "第二個回答", "第三個回答")))
    conversation = start(client)
    send(client, conversation, "第一個問題")
    chat_graph(Models(**{**vars(models), "chat": failing_chat()}))
    failed_id = uuid.uuid4()
    send(client, conversation, "第二個問題", failed_id)
    chat_graph(models)

    response = retry(client, failed_id)
    send(client, conversation, "第三個問題")

    assert response.status_code == 200
    events = sse_events(response.text)
    assert [name for name, _ in events][0] == "accepted"
    assert [name for name, _ in events][-2:] == ["reply", "completed"]
    assert {data["attempt"] for _, data in events} == {2}
    row = turn_row(failed_id)
    assert (row["status"], row["reply_text"], row["error_code"]) == (
        "completed",
        "第二個回答",
        None,
    )
    # The retry ran from the memory before the failure, and the next turn remembers the
    # question once, with the reply that completed.
    assert [m.text for m in models.router.calls[-2][0]] == [
        "第一個問題",
        "第一個回答",
        "第二個問題",
    ]
    assert [m.text for m in models.router.calls[-1][0]] == [
        "第一個問題",
        "第一個回答",
        "第二個問題",
        "第二個回答",
        "第三個問題",
    ]
    turns = client.get(f"/v1/conversations/{conversation}/turns", headers=AUTH).json()["turns"]
    assert [t["user_text"] for t in turns] == ["第一個問題", "第二個問題", "第三個問題"]


def test_only_the_last_stopped_or_failed_reply_can_be_retried(client, as_member, chat_graph):
    as_member("uid-a")
    models = chat_graph(fake_models(chat=("回答", "回答")))
    conversation = start(client)
    completed_id = uuid.uuid4()
    send(client, conversation, "第一題", completed_id)
    chat_graph(Models(**{**vars(models), "chat": failing_chat()}))
    failed_id = uuid.uuid4()
    send(client, conversation, "第二題", failed_id)

    def code(response) -> tuple[int, str]:
        return response.status_code, response.json()["error"]["code"]

    assert code(retry(client, completed_id)) == (409, "TURN_NOT_RETRYABLE")
    assert code(retry(client, failed_id, expected_attempt=2)) == (409, "REVISION_CONFLICT")
    running_id = running_turn(conversation, seq=3)
    assert code(retry(client, running_id)) == (409, "TURN_IN_PROGRESS")
    cancel(client, running_id)
    # A later turn exists now: the failed one is no longer the last.
    assert code(retry(client, failed_id)) == (409, "TURN_NOT_RETRYABLE")


def test_a_reply_past_its_time_limit_fails(client, as_member, chat_graph, monkeypatch):
    as_member("uid-a")
    monkeypatch.setattr(turns, "TURN_SECONDS", 0.3)
    chat_graph(Models(**{**vars(fake_models()), "chat": slow_chat(5)}))
    turn_id = uuid.uuid4()

    began = time.perf_counter()
    events = sse_events(send(client, start(client), "嗨", turn_id).text)

    assert time.perf_counter() - began < 3
    assert [name for name, _ in events] == ["accepted", "status", "title", "error"]
    assert events[-1][1]["code"] == "TURN_TIMEOUT"
    assert events[-1][1]["retryable"] is True
    row = turn_row(turn_id)
    assert (row["status"], row["error_code"], row["head_checkpoint_id"]) == (
        "failed",
        "TURN_TIMEOUT",
        None,
    )


def test_a_turn_left_running_by_a_lost_request_gives_way(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models())
    conversation = start(client)
    lost_id = running_turn(conversation, minutes_ago=4)

    shown = client.get(f"/v1/conversations/{conversation}/turns", headers=AUTH).json()
    response = send(client, conversation, "嗨")

    assert shown["turns"][0]["turn_id"] == str(lost_id)
    assert shown["turns"][0]["status"] == "failed"
    assert shown["turns"][0]["error_code"] == "STREAM_INTERRUPTED"
    assert sse_events(response.text)[-1][0] == "completed"


def test_a_turn_running_within_its_time_is_not_taken_for_lost(client, as_member, chat_graph):
    as_member("uid-a")
    chat_graph(fake_models())
    conversation = start(client)
    turn_id = running_turn(conversation, minutes_ago=2)

    response = send(client, conversation, "嗨")

    assert response.status_code == 409
    assert turn_row(turn_id)["status"] == "running"


def test_a_turn_ending_after_its_conversation_was_deleted_leaves_no_memory(
    client, as_member, chat_graph
):
    as_member("uid-a")
    chat_graph(fake_models())
    conversation = start(client)
    send(client, conversation, "第一題")
    turn_id = running_turn(conversation, seq=2)
    deleted = client.delete(f"/v1/conversations/{conversation}", headers=AUTH)

    async def saved_late() -> None:
        # What the running turn's graph saves as it ends, after the delete removed the rest.
        config = {"configurable": {"thread_id": conversation, "checkpoint_ns": ""}}
        await app.state.checkpointer.aput(
            config, empty_checkpoint(), {"source": "loop", "step": 1, "parents": {}}, {}
        )

    client.portal.call(saved_late)
    ended = end(client, started(turn_id, conversation), "completed", "回答", "late")

    assert deleted.status_code == 204
    assert ended is False
    left = asyncio.run(
        run_as_migrator(
            "SELECT count(*) AS n FROM app.checkpoints WHERE thread_id = $1", conversation
        )
    )
    assert left[0]["n"] == 0


def test_two_devices_cannot_write_in_one_conversation_at_once(client, as_member, chat_graph):
    # AC-CHAT-07, with the requests really overlapping.
    as_member("uid-a")
    chat_graph(Models(**{**vars(fake_models()), "chat": slow_chat(1.5)}))
    conversation = start(client)
    first_id = uuid.uuid4()
    first = threading.Thread(target=send, args=(client, conversation, "第一台", first_id))
    first.start()
    for _ in range(50):
        if asyncio.run(run_as_migrator("SELECT 1 FROM app.turns WHERE turn_id = $1", first_id)):
            break
        time.sleep(0.05)

    second = send(client, conversation, "第二台")
    first.join()

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "TURN_IN_PROGRESS"
    assert turn_row(first_id)["status"] == "completed"
