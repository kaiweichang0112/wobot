"""Reading a list on (D9, DEC-096): the next items of a list reply, spoken a page at a
time from the reply kept, without the graph."""

import asyncio
import json
import uuid

import pytest

from tests.agent.fakes import fake_models
from tests.api import AUTH, sse_events, start
from tests.database import run_as_migrator
from tests.test_asset_state_api import voice
from tests.test_turn_speech_api import FakeVoice, ask, ready
from wobot.api.main import app

pytestmark = pytest.mark.usefixtures("members", "published_version")

LECTURES = "共 10 場演講。\n\n" + "\n".join(
    f"{i}. 2024/01/{i:02d}　演講 {i}，PDF：https://a.example/{i}" for i in range(1, 11)
)


@pytest.fixture
def speaking(client):
    before = app.state.eleven
    app.state.eleven = FakeVoice()
    yield app.state.eleven
    app.state.eleven = before


def list_turn(client, chat_graph) -> uuid.UUID:
    """A completed Avatar-mode turn whose kept reply is a list of ten talks."""
    chat_graph(fake_models(chat=("好。",)))
    turn_id = uuid.uuid4()
    ask(client, start(client), "列出演講", turn_id)
    reply = {"text": LECTURES, "lists": [{"result_id": "q-1", "kind": "lecture", "keys": []}]}
    asyncio.run(
        run_as_migrator(
            "UPDATE app.turns SET reply = $2::jsonb, route = 'list' WHERE turn_id = $1",
            turn_id,
            json.dumps(reply, ensure_ascii=False),
        )
    )
    return turn_id


def read_on(client, turn_id: uuid.UUID, start_at: int, attempt: int = 1):
    return client.post(
        f"/v1/turns/{turn_id}/continue-reading",
        json={"attempt": attempt, "from": start_at},
        headers=AUTH,
    )


def test_an_avatar_turn_says_where_reading_goes_on(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    chat_graph(fake_models(route="list"))

    avatar = sse_events(ask(client, start(client), "列出演講").text)
    text = sse_events(ask(client, start(client), "列出演講", mode="text").text)

    assert avatar[-1] == ("completed", {**avatar[-1][1], "next": None})  # nothing found
    assert "next" not in text[-1][1]


def test_a_list_is_read_on_to_its_end_without_the_graph(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    turn_id = list_turn(client, chat_graph)
    app.state.graph = None  # reading on runs no graph
    speaking.spoken.clear()

    first = sse_events(read_on(client, turn_id, 3).text)
    second = sse_events(read_on(client, turn_id, first[-1][1]["next"]).text)

    said = [data["text"] for name, data in first if name == "speech"]
    assert said[:2] == ["2024/01/04，", "演講 4"]  # the first sentence ends early (D7)
    assert said[-1] == "還有 2 筆可以繼續聽。"
    assert all(data["emotion"] == "neutral" for name, data in first if name == "speech")
    assert first[-1] == ("completed", {"turn_id": str(turn_id), "attempt": 1, "next": 8})
    assert [data["text"] for name, data in second if name == "speech"] == [
        "2024/01/09，",
        "演講 9",
        "2024/01/10，演講 10",
    ]
    assert second[-1][1]["next"] is None
    assert all("https" not in text for _, text, _ in speaking.spoken)
    assert read_on(client, turn_id, 10).json()["error"]["code"] == "NOTHING_TO_READ"


def test_only_a_completed_list_of_the_attempt_shown_is_read_on(
    client, as_member, chat_graph, speaking
):
    as_member("uid-a")
    start(client)
    ready()
    chat_graph(fake_models(chat=("你好！",)))
    chat_turn = uuid.uuid4()
    ask(client, start(client), "嗨", chat_turn)
    turn_id = list_turn(client, chat_graph)

    not_a_list = read_on(client, chat_turn, 0)
    old_attempt = read_on(client, turn_id, 3, attempt=2)

    assert not_a_list.status_code == 409
    assert not_a_list.json()["error"]["code"] == "NOTHING_TO_READ"
    assert old_attempt.status_code == 409


def test_reading_on_needs_the_voice_in_use(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    turn_id = list_turn(client, chat_graph)
    asyncio.run(
        run_as_migrator(
            "UPDATE app.voice_versions SET status = 'deleted', finished_at = now() "
            "WHERE owner_id = 'uid-a' AND status = 'active'"
        )
    )

    response = read_on(client, turn_id, 3)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VOICE_NOT_READY"


def test_reading_on_speaks_in_the_voice_in_use_now(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    turn_id = list_turn(client, chat_graph)
    asyncio.run(
        run_as_migrator(
            "UPDATE app.voice_versions SET status = 'retired' "
            "WHERE owner_id = 'uid-a' AND status = 'active'"
        )
    )
    replaced = voice("uid-a", "active")
    speaking.spoken.clear()

    read_on(client, turn_id, 3)

    assert speaking.spoken[0][0] == f"provider-{replaced}"


def test_another_account_s_list_is_not_found(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    turn_id = list_turn(client, chat_graph)
    as_member("uid-b")
    start(client)
    ready("uid-b")

    response = read_on(client, turn_id, 3)

    assert response.status_code == 404
