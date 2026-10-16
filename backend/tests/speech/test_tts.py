import asyncio
import base64

from wobot.assets.eleven import ElevenLabsError
from wobot.speech.tts import Speaker, language_of


class FakeVoice:
    """Speaks each sentence after the delay given for it; counts how many at once."""

    def __init__(self, delays: dict[str, float] | None = None, fails: set[str] | None = None):
        self.delays = delays or {}
        self.fails = fails or set()
        self.started: list[str] = []
        self.languages: list[str | None] = []
        self.busy = 0
        self.most = 0

    async def speak(self, voice_id, text, model, output_format, language=None):
        self.started.append(text)
        self.languages.append(language)
        self.busy += 1
        self.most = max(self.most, self.busy)
        try:
            await asyncio.sleep(self.delays.get(text, 0.01))
            if text in self.fails:
                raise ElevenLabsError("down", 503)
            return text.encode()
        finally:
            self.busy -= 1


async def speak_all(voice: FakeVoice, sentences: list[str]) -> list[tuple[str, dict]]:
    told: list[tuple[str, dict]] = []
    done = asyncio.Event()

    def emit(name, data):
        told.append((name, data))
        if name == "speech_end":
            done.set()

    speaker = Speaker(voice, "v-1", "eleven_v4_turbo", emit)
    for sentence in sentences:
        speaker.say(sentence)
    speaker.close()
    await asyncio.wait_for(done.wait(), 5)
    return told


async def test_sentences_are_told_in_order_though_a_later_one_is_ready_first():
    voice = FakeVoice({"第一句。": 0.2, "第二句。": 0.01})

    told = await speak_all(voice, ["第一句。", "第二句。"])

    assert [name for name, _ in told] == ["speech", "speech", "speech_end"]
    assert [data["index"] for _, data in told[:2]] == [0, 1]
    assert base64.b64decode(told[0][1]["audio"]) == "第一句。".encode()
    assert told[0][1]["emotion"] == "neutral"


async def test_at_most_two_sentences_are_spoken_at_once():
    voice = FakeVoice({f"{n}。": 0.05 for n in range(6)})

    await speak_all(voice, [f"{n}。" for n in range(6)])

    assert voice.most == 2


async def test_a_sentence_that_fails_is_told_as_an_error_and_the_rest_go_on():
    voice = FakeVoice(fails={"壞的。"})

    told = await speak_all(voice, ["好的。", "壞的。", "也好。"])

    assert [(name, data.get("index")) for name, data in told] == [
        ("speech", 0),
        ("speech_error", 1),
        ("speech", 2),
        ("speech_end", None),
    ]


async def test_a_stop_tells_nothing_more_and_starts_nothing_more():
    voice = FakeVoice({f"{n}。": 0.2 for n in range(5)})
    told = []
    speaker = Speaker(voice, "v-1", "m", lambda name, data: told.append(name))
    for n in range(5):
        speaker.say(f"{n}。")

    await asyncio.sleep(0.05)
    await speaker.stop()
    speaker.say("之後的。")
    await asyncio.sleep(0.3)

    assert told == []
    assert len(voice.started) == 2  # the two under way; the rest never started
    assert speaker.stopped


async def test_a_feeling_is_spoken_with_its_tag_by_a_model_that_takes_tags():
    voice = FakeVoice()
    told = []
    done = asyncio.Event()

    async def feel(sentence, previous):
        return "joy" if "好" in sentence else "neutral"

    def emit(name, data):
        told.append((name, data))
        if name == "speech_end":
            done.set()

    speaker = Speaker(voice, "v-1", "eleven_v4_turbo", emit, feel=feel)
    speaker.say("太好了！")
    speaker.say("地址在桃園。")
    speaker.close()
    await asyncio.wait_for(done.wait(), 5)

    assert voice.started == ["[excited] 太好了！", "地址在桃園。"]
    assert [data["emotion"] for name, data in told if name == "speech"] == ["joy", "neutral"]
    assert told[0][1]["text"] == "太好了！"  # the tag is spoken, never shown


async def test_a_model_without_tags_gets_the_sentence_alone():
    voice = FakeVoice()
    done = asyncio.Event()

    async def feel(sentence, previous):
        return "joy"

    speaker = Speaker(
        voice,
        "v-1",
        "eleven_flash_v2_5",
        lambda name, data: name == "speech_end" and done.set(),
        feel=feel,
    )
    speaker.say("太好了！")
    speaker.close()
    await asyncio.wait_for(done.wait(), 5)

    assert voice.started == ["太好了！"]


def test_a_sentence_with_a_chinese_character_is_spoken_in_mandarin():
    assert language_of("這款產品可以幫忙。") == "zh"
    assert language_of("WhizPad 是一款床墊。") == "zh"  # a name in English, said in Mandarin
    assert language_of("WhizPad is a mattress.") == "en"
    # A date alone is said as the sentence before it was.
    assert language_of("2023/11/20，") == "zh"
    assert language_of("2023/11/20,", previous="en") == "en"


async def test_each_sentence_is_spoken_in_its_own_language():
    voice = FakeVoice()

    await speak_all(voice, ["你好。", "Hello there.", "2023/11/20,", "再見。"])

    assert voice.languages == ["zh", "en", "en", "zh"]
