import asyncio
import hashlib
from types import SimpleNamespace

from wobot.speech import emotions
from wobot.speech.emotions import EmotionClassifier


class FakeJev:
    """Answers each sentence with the feeling and confidence given, after a delay."""

    def __init__(self, choice="joy", confidence=0.9, delay=0.0, fails=False):
        self.choice, self.confidence, self.delay, self.fails = choice, confidence, delay, fails
        self.requests = []

    async def ainvoke(self, request):
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        if self.fails:
            raise ConnectionError("Jev is down")
        answer = SimpleNamespace(choice=self.choice, confidence=self.confidence)
        return SimpleNamespace(answers={"emotion": answer})


def feel(jev, sentence="找到了！", previous=None, question="GRC 在哪？"):
    classifier = EmotionClassifier(jev, threshold=0.7, timeout=0.2)
    return asyncio.run(classifier.for_question(question)(sentence, previous))


def test_the_prompt_version_moves_with_the_criteria():
    fingerprint = hashlib.sha256(emotions.criteria_fingerprint().encode()).hexdigest()[:12]

    assert (emotions.PROMPT_VERSION, fingerprint) == (1, "0c0146fe96e6")


def test_a_sure_feeling_is_kept():
    jev = FakeJev("joy", 0.9)

    assert feel(jev, "太好了！", "GRC 於 2003 年成立。", "GRC 哪年成立？") == "joy"
    assert jev.requests[0]["state"] == {
        "sentence": "太好了！",
        "previous_sentence": "GRC 於 2003 年成立。",
        "user_question": "GRC 哪年成立？",
    }


def test_an_unsure_slow_failed_or_unknown_answer_is_neutral():
    assert feel(FakeJev("anger", 0.69)) == "neutral"
    assert feel(FakeJev("joy", 0.9, delay=0.5)) == "neutral"
    assert feel(FakeJev(fails=True)) == "neutral"
    assert feel(FakeJev("boredom", 0.99)) == "neutral"


def test_every_feeling_but_neutral_has_an_audio_tag():
    assert set(emotions.TAGS) == set(emotions.EMOTIONS) - {"neutral"}
