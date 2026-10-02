import json

import pytest

from tests.knowledge.fakes import FakeLectureReader, MemoryAnswerCache
from tests.knowledge.openai_responses import openai_answering
from wobot.knowledge.extraction import (
    INSTRUCTIONS,
    LECTURE_FIELDS,
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    REASONING_EFFORT,
    Answer,
    CachedReader,
    DbAnswerCache,
    LectureFields,
    OpenAILectureReader,
    Question,
    grounded,
)
from wobot.knowledge.hashing import content_hash

ENTRY = "“Smart care,” keynote speech, The 3rd Care Congress, Istanbul, Turkey, 2021/11/12"
# Quotation marks inside a part: a symposium named in quotes, a webinar with its theme.
QUOTING = "Keynote speech in “Community care” Nara, Joint Webinar: “Aging well”, 2019/01/25"

# Answers are cached under the prompt version, so a changed prompt with the old version
# would return answers to another question. After changing the instructions, the schema
# or the request settings, bump PROMPT_VERSION and record the new fingerprint here.
PROMPT_FINGERPRINTS = {
    1: "aaf4974f2944a924c11707abe54fdf323556e75cc11b84b7d0030d9297914d6b",
    2: "487f6dc79ffa5c36928551da57fc7838f2ae79f8e3a8fbc5cc70abeb936c24dc",
    3: "7dc9d83ae9dc9c5d73f8be9901bebb1f83014b6bb2ab3ac137ade06787b47afa",
}


def test_a_changed_prompt_comes_with_a_new_version():
    fingerprint = content_hash(
        {
            "instructions": INSTRUCTIONS,
            "schema": LectureFields.model_json_schema(),
            "reasoning_effort": REASONING_EFFORT,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
    )

    assert PROMPT_FINGERPRINTS.get(PROMPT_VERSION) == fingerprint


# --- Grounding --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Smart care", "Smart care"),
        ("“Smart care,”", "Smart care"),  # quotation marks and separators trimmed
        ('Smart care,"', "Smart care"),  # a closing mark whose partner is outside
        ("“Community care”", "Community care"),
        ("Webinar: “Aging well”", "Webinar: “Aging well”"),  # a pair inside the span
        ("Istanbul,\n  Turkey", "Istanbul, Turkey"),  # spaces compared collapsed
        ("The Third Care Congress", None),  # reworded
        ("Smart Care", None),  # letter case is part of the text
        ("智慧照護", None),  # translated
        ("“,”", None),  # nothing left
        ("", None),
        (None, None),
    ],
)
def test_keeps_a_value_only_as_a_span_of_the_entry(value, expected):
    assert grounded(value, f"{ENTRY} {QUOTING}") == expected


# --- Asking once ------------------------------------------------------------------------


async def test_asks_the_model_once_per_distinct_entry_and_never_again():
    reader, cache = FakeLectureReader(), MemoryAnswerCache()

    answers, stats = await CachedReader(reader, cache).read_all(
        ["a “A,” x", "b “B,” y", "a “A,” x"]
    )
    again, second = await CachedReader(FakeLectureReader(), cache).read_all(["a “A,” x"])

    assert sorted(reader.calls) == ["a “A,” x", "b “B,” y"]
    assert (stats.entries, stats.cached, stats.calls) == (2, 0, 2)
    assert stats.input_tokens == len("a “A,” x") + len("b “B,” y")
    assert (second.cached, second.calls, second.input_tokens) == (1, 0, 0)
    assert again == {"a “A,” x": answers["a “A,” x"]}


async def test_another_model_or_prompt_version_asks_again():
    cache = MemoryAnswerCache()
    await CachedReader(FakeLectureReader(), cache).read_all([ENTRY])
    other = FakeLectureReader()
    other.question = Question(LECTURE_FIELDS, "fake-model", PROMPT_VERSION + 1)

    await CachedReader(other, cache).read_all([ENTRY])

    assert other.calls == [ENTRY]


async def test_keeps_the_answers_that_arrived_when_a_request_fails():
    def flaky(entry):
        if entry == "broken":
            raise TimeoutError("no answer")
        return {"title": None, "event": None, "location": None}

    cache = MemoryAnswerCache()
    with pytest.raises(TimeoutError):
        await CachedReader(FakeLectureReader(flaky), cache).read_all(["one", "broken", "two"])
    retry = FakeLectureReader()
    await CachedReader(retry, cache).read_all(["one", "broken", "two"])

    assert retry.calls == ["broken"]


async def test_a_refusal_is_kept_and_counted_like_any_answer():
    refusal = Answer(None, "refused: no", "fake-model-2026-01-01", 30, 5)
    cache = MemoryAnswerCache()

    _, stats = await CachedReader(FakeLectureReader(lambda _: refusal), cache).read_all([ENTRY])
    answers, again = await CachedReader(FakeLectureReader(), cache).read_all([ENTRY])

    assert (stats.failures, again.failures, again.calls) == (1, 1, 0)
    assert answers[ENTRY] == refusal


async def test_the_database_keeps_one_answer_per_question(ingest_db):
    cache, question = DbAnswerCache(ingest_db), Question(LECTURE_FIELDS, "test-model", 1)
    first = Answer({"title": "Smart care", "event": None, "location": None}, None, "m-1", 9, 3)
    refusal = Answer(None, "refused: no", "m-1", 9, 2)

    await cache.put(question, "hash-a", {"entry": ENTRY}, first)
    await cache.put(question, "hash-a", {"entry": ENTRY}, refusal)  # kept as it was
    await cache.put(question, "hash-b", {"entry": "other"}, refusal)

    assert await cache.get(question, ["hash-a", "hash-b", "hash-c"]) == {
        "hash-a": first,
        "hash-b": refusal,
    }
    assert await cache.get(Question(LECTURE_FIELDS, "other-model", 1), ["hash-a"]) == {}


# --- The OpenAI request -----------------------------------------------------------------


async def test_sends_the_entry_as_json_data_under_a_strict_schema():
    requests = []
    output = {"title": "Smart care", "event": "The 3rd Care Congress", "location": None}
    client = openai_answering(
        {"type": "output_text", "text": json.dumps(output), "annotations": []}, requests
    )

    answer = await OpenAILectureReader(client, "gpt-test").read(ENTRY)

    assert answer == Answer(output, None, "gpt-test-2026-01-01", 420, 31)
    sent = requests[0]
    assert sent["model"] == "gpt-test"
    assert sent["instructions"] == INSTRUCTIONS
    assert json.loads(sent["input"]) == {"entry": ENTRY}
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert sent["reasoning"] == {"effort": REASONING_EFFORT}
    assert sent["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["store"] is False


async def test_reports_a_refusal_with_its_usage():
    client = openai_answering({"type": "refusal", "refusal": "I can't help with that."}, [])

    answer = await OpenAILectureReader(client, "gpt-test").read(ENTRY)

    assert answer == Answer(
        None, "refused: I can't help with that.", "gpt-test-2026-01-01", 420, 31
    )


async def test_reports_an_answer_cut_off_at_the_token_cap():
    client = openai_answering(
        {"type": "output_text", "text": '{"title": "Smart', "annotations": []},
        [],
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )

    answer = await OpenAILectureReader(client, "gpt-test").read(ENTRY)

    assert answer.output is None
    assert answer.failure.startswith("unreadable output")
