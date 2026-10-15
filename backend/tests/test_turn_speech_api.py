"""A turn in Avatar mode (D7, DEC-089): only with the account's own avatar and voice in
use; its reply spoken sentence by sentence in the same stream; and a stop from the app
ending the speech, not the turn."""

import asyncio
import json
import uuid

import pytest
from langchain_core.messages import AIMessage

from tests.agent.fakes import fake_models
from tests.api import AUTH, sse_events, start, turn_row
from tests.database import run_as_migrator
from tests.test_asset_state_api import avatar, voice
from wobot.api.main import app

pytestmark = pytest.mark.usefixtures("members", "published_version")


class FakeVoice:
    """ElevenLabs as the turn uses it: each sentence becomes its own bytes."""

    def __init__(self, on_speak=None):
        self.spoken: list[tuple[str, str, str]] = []
        self.on_speak = on_speak

    async def speak(self, voice_id, text, model, output_format="mp3"):
        self.spoken.append((voice_id, text, model))
        if self.on_speak:
            await self.on_speak(len(self.spoken))
        await asyncio.sleep(0.01)
        return f"audio:{text}".encode()


@pytest.fixture
def speaking(client):
    """The app with a fake voice; restored afterwards."""
    before = app.state.eleven
    app.state.eleven = FakeVoice()
    yield app.state.eleven
    app.state.eleven = before


def ask(client, conversation: str, text: str, turn_id: uuid.UUID | None = None, mode="avatar"):
    return client.post(
        f"/v1/conversations/{conversation}/turns",
        json={"turn_id": str(turn_id or uuid.uuid4()), "text": text, "mode": mode},
        headers=AUTH,
    )


def ready(owner: str = "uid-a") -> str:
    """The account's avatar and voice, both in use; the voice's ID."""
    avatar(owner, "active")
    return voice(owner, "active")


def speech_of(turn_id: uuid.UUID) -> dict:
    rows = asyncio.run(
        run_as_migrator(
            "SELECT mode, speech, heard_upto, speech_stopped_at FROM app.turns WHERE turn_id = $1",
            turn_id,
        )
    )
    row = dict(rows[0])
    if row["speech"] is not None:
        row["speech"] = json.loads(row["speech"])
    return row


def test_avatar_mode_needs_the_account_s_own_avatar_and_voice(
    client, as_member, chat_graph, speaking
):
    as_member("uid-a")
    chat_graph(fake_models())
    conversation = start(client)

    no_avatar = ask(client, conversation, "你好")
    avatar("uid-a", "active")
    no_voice = ask(client, conversation, "你好")

    assert no_avatar.status_code == 409
    assert no_avatar.json()["error"]["code"] == "AVATAR_NOT_READY"
    assert no_voice.status_code == 409
    assert no_voice.json()["error"]["code"] == "VOICE_NOT_READY"
    turns = client.get(f"/v1/conversations/{conversation}/turns", headers=AUTH).json()
    assert turns["turns"] == []


def test_the_reply_is_spoken_sentence_by_sentence_before_the_turn_ends(
    client, as_member, chat_graph, speaking
):
    as_member("uid-a")
    start(client)
    voice_id = ready()
    chat_graph(fake_models(chat=("太好了，我知道答案！GRC 在桃園。歡迎來玩。",)))
    conversation = start(client)
    turn_id = uuid.uuid4()

    events = sse_events(ask(client, conversation, "GRC 在哪裡？", turn_id).text)

    names = [name for name, _ in events]
    speeches = [data for name, data in events if name == "speech"]
    assert [s["text"] for s in speeches] == ["太好了，我知道答案！", "GRC 在桃園。", "歡迎來玩。"]
    assert [s["index"] for s in speeches] == [0, 1, 2]
    assert all(s["emotion"] == "neutral" for s in speeches)
    assert names.index("reply") < names.index("completed") == len(names) - 1
    assert names[-1] == "completed" and "speech_end" not in names
    assert speaking.spoken[0][0] == f"provider-{voice_id}"
    row = speech_of(turn_id)
    assert row["mode"] == "avatar"
    assert [s["text"] for s in row["speech"]["sentences"]] == [s["text"] for s in speeches]
    assert row["speech"]["voice_id"] == voice_id
    assert turn_row(turn_id)["status"] == "completed"


def test_a_text_turn_is_not_spoken(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    chat_graph(fake_models(chat=("你好！",)))
    conversation = start(client)

    events = sse_events(ask(client, conversation, "嗨", mode="text").text)

    assert "speech" not in [name for name, _ in events]
    assert speaking.spoken == []


def test_a_stop_from_the_app_ends_the_speech_and_the_text_is_kept(
    client, as_member, chat_graph, speaking
):
    as_member("uid-a")
    start(client)
    ready()
    turn_id = uuid.uuid4()

    async def stop_on_the_first(count: int) -> None:
        if count == 1:
            # The app's playback request, as another request would write it.
            await run_as_migrator(
                "UPDATE app.turns SET speech_stopped_at = now() WHERE turn_id = $1", turn_id
            )
            await asyncio.sleep(1.2)  # a check reads it within a second

    speaking.on_speak = stop_on_the_first
    sentences = "".join(f"第{n}句。" for n in range(1, 9))
    chat_graph(fake_models(chat=(sentences,)))
    conversation = start(client)

    events = sse_events(ask(client, conversation, "說很多話", turn_id).text)

    names = [name for name, _ in events]
    assert names[-1] == "completed"
    assert "reply" in names
    assert names.count("speech") < 8
    assert turn_row(turn_id)["status"] == "completed"
    assert speech_of(turn_id)["speech"]["stopped"] is True


def test_playback_only_moves_forward_and_a_stop_is_kept(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    chat_graph(fake_models(chat=("好。",)))
    conversation = start(client)
    turn_id = uuid.uuid4()
    ask(client, conversation, "嗨", turn_id)

    def play(**body):
        response = client.post(
            f"/v1/turns/{turn_id}/playback", json={"attempt": 1, **body}, headers=AUTH
        )
        assert response.status_code == 200
        return response.json()

    assert play(heard=2)["heard_upto"] == 2
    assert play(heard=1)["heard_upto"] == 2  # late, so nothing changes
    stopped = play(stopped=True)
    again = play(stopped=True)

    assert stopped["speech_stopped"] is True and again["speech_stopped"] is True
    assert speech_of(turn_id)["heard_upto"] == 2


def test_another_account_s_playback_is_not_found(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    chat_graph(fake_models(chat=("好。",)))
    conversation = start(client)
    turn_id = uuid.uuid4()
    ask(client, conversation, "嗨", turn_id)
    as_member("uid-b")

    response = client.post(
        f"/v1/turns/{turn_id}/playback", json={"attempt": 1, "stopped": True}, headers=AUTH
    )

    assert response.status_code == 404
    assert speech_of(turn_id)["speech_stopped_at"] is None


def test_a_retry_is_spoken_again_in_avatar_mode(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    turn_id = uuid.uuid4()
    chat_graph(fake_models(chat=("第一次。", "第二次。")))
    conversation = start(client)
    ask(client, conversation, "嗨", turn_id)
    asyncio.run(
        run_as_migrator("UPDATE app.turns SET status = 'cancelled' WHERE turn_id = $1", turn_id)
    )

    events = sse_events(
        client.post(f"/v1/turns/{turn_id}/retry", json={"expected_attempt": 1}, headers=AUTH).text
    )

    assert "speech" in [name for name, _ in events]
    assert speech_of(turn_id)["heard_upto"] is None


class FakeEmotions:
    """Joy for a sentence with 好, neutral otherwise; keeps the question each reply asked."""

    def __init__(self):
        self.questions = []

    def for_question(self, question):
        self.questions.append(question)

        async def feel(sentence, previous):
            return "joy" if "好" in sentence else "neutral"

        return feel


def test_each_sentence_is_spoken_with_its_feeling(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    before = getattr(app.state, "emotions", None)
    app.state.emotions = FakeEmotions()
    try:
        chat_graph(fake_models(chat=("GRC 在桃園。太好了，找到了！",)))
        conversation = start(client)
        turn_id = uuid.uuid4()

        events = sse_events(ask(client, conversation, "GRC 在哪？", turn_id).text)
    finally:
        feelings, app.state.emotions = app.state.emotions, before

    speeches = [data for name, data in events if name == "speech"]
    assert [(s["text"], s["emotion"]) for s in speeches] == [
        ("GRC 在桃園。", "neutral"),
        ("太好了，找到了！", "joy"),
    ]
    assert feelings.questions == ["GRC 在哪？"]
    assert speaking.spoken[1][1] == "[excited] 太好了，找到了！"
    sentences = speech_of(turn_id)["speech"]["sentences"]
    assert [s["emotion"] for s in sentences] == ["neutral", "joy"]


def test_only_small_talk_is_spoken_with_feeling(client, as_member, chat_graph, speaking):
    as_member("uid-a")
    start(client)
    ready()
    before = getattr(app.state, "emotions", None)
    app.state.emotions = FakeEmotions()
    try:
        # The recommendation agent asks back, a sentence with 好 in it.
        chat_graph(fake_models(route="recommend", rec_calls=(AIMessage("好的！你想用在家裡嗎？"),)))
        conversation = start(client)

        events = sse_events(ask(client, conversation, "推薦床墊").text)
    finally:
        app.state.emotions = before

    speeches = [data for name, data in events if name == "speech"]
    assert speeches and all(s["emotion"] == "neutral" for s in speeches)
    assert not any(text.startswith("[") for _, text, _ in speaking.spoken)
