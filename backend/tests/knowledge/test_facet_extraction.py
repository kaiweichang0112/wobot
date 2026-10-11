import json

import pytest

from tests.knowledge.fakes import FakeFacetReader, fake_facets
from tests.knowledge.openai_responses import openai_answering
from tests.knowledge.workbooks import catalog_row
from wobot.knowledge.extraction import Answer
from wobot.knowledge.facet_extraction import (
    INSTRUCTIONS,
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    REASONING_EFFORT,
    OpenAIFacetReader,
    ProductFacets,
    ProductText,
    grounded_facets,
)
from wobot.knowledge.facets import FACETS, FacetValue
from wobot.knowledge.hashing import content_hash
from wobot.knowledge.records.products import normalize_product, with_facets

# Answers are cached under the prompt version, so a changed prompt with the old version
# would return answers to another question. After changing the instructions, the
# vocabulary, the schema or the request settings, bump PROMPT_VERSION and record the new
# fingerprint here.
PROMPT_FINGERPRINTS = {
    1: "e1d35836e44c0a112fe76f8c10f5a2103f96aa10f3456d504cd591488f61abc4",
    2: "0383be8dd93748c397454b0d1c557cce8a6741ea2450867555fea0590158c479",
    3: "77300fa40a62cb4b84192121708d90a0fd09b74536cbb29c0306b0d3d6012d91",
}

TEXT = {
    "product_name": "測試床墊 TM-1",
    "features_text": "提供離床提醒與翻身提醒。",
    "usage_text": "感測墊鋪於床墊下，提供離床提醒。",
    "summary_text": None,
}


def answer(**facets):
    return {facet: [] for facet in FACETS} | facets


def test_a_changed_prompt_comes_with_a_new_version():
    fingerprint = content_hash(
        {
            "instructions": INSTRUCTIONS,
            "schema": ProductFacets.model_json_schema(),
            "reasoning_effort": REASONING_EFFORT,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
    )

    assert PROMPT_FINGERPRINTS.get(PROMPT_VERSION) == fingerprint


def test_the_schema_offers_only_the_vocabulary():
    schema = ProductFacets.model_json_schema()
    value = schema["$defs"]["FormValue"]["properties"]["value"]

    assert set(schema["required"]) == set(FACETS)
    assert value["enum"] == ["worn", "bed", "room", "device", "equipment", "software"] + [
        "service",
        "robot",
    ]
    assert "- 離床偵測（離床預警、離床警示、離床提醒、離床通知、在床狀態）" in INSTRUCTIONS


# --- Grounding --------------------------------------------------------------------------


def test_keeps_a_value_only_where_the_text_states_it():
    output = answer(
        functions=[
            {"value": "離床偵測", "span": " 離床提醒 "},  # in both fields: the first wins
            {"value": "翻身監測", "span": "翻身提醒"},
            {"value": "離床偵測", "span": "離床"},  # given twice: kept once
            {"value": "睡眠監測", "span": "睡眠品質"},  # not in the text
        ],
        form=[{"value": "bed", "span": "鋪於床墊下"}],
    )

    kept, dropped = grounded_facets(output, TEXT)

    assert kept == [
        FacetValue("functions", "離床偵測", "離床提醒", "features_text"),
        FacetValue("functions", "翻身監測", "翻身提醒", "features_text"),
        FacetValue("form", "bed", "鋪於床墊下", "usage_text"),
    ]
    assert dropped == ["facet functions 睡眠監測: '睡眠品質' is not in the text"]


def test_a_product_read_is_a_new_revision_with_its_facets():
    draft = normalize_product(catalog_row())
    question = FakeFacetReader.question
    read = Answer(answer(functions=[{"value": "離床偵測", "span": "離床偵測"}]), None, "m", 0, 0)

    product = with_facets(draft, read, question)

    assert product.fields["facets_model"] == "fake-facets"
    assert product.fields["facets_prompt_version"] == PROMPT_VERSION
    assert product.facets == (FacetValue("functions", "離床偵測", "離床偵測", "features_text"),)
    assert product.content_hash != draft.content_hash
    unread = with_facets(draft, Answer(None, "refused: no", "m", 0, 0), question)
    assert unread.content_hash == draft.content_hash and unread.fields["facets_model"] is None
    assert unread.warnings[-1] == "no facets: refused: no"


async def test_each_product_text_is_read_once():
    reader = FakeFacetReader()
    facets = fake_facets(reader)
    text = ProductText.of(TEXT)

    await facets.read_all([text, text])
    answers, stats = await facets.read_all([text])

    assert len(reader.calls) == 1 and stats.cached == 1
    assert answers[text].output["functions"] == []  # no function named word for word


# --- The OpenAI request -----------------------------------------------------------------


async def test_sends_the_product_as_json_data_under_a_strict_schema():
    requests = []
    output = answer(functions=[{"value": "離床偵測", "span": "離床提醒"}])
    client = openai_answering(
        {"type": "output_text", "text": json.dumps(output), "annotations": []}, requests
    )
    text = ProductText.of(TEXT)

    read = await OpenAIFacetReader(client, "gpt-test").read(text)

    assert read == Answer(output, None, "gpt-test-2026-01-01", 420, 31)
    sent = requests[0]
    assert sent["model"] == "gpt-test"
    assert sent["instructions"] == INSTRUCTIONS
    assert json.loads(sent["input"]) == {
        "product": "測試床墊 TM-1",
        "features": "提供離床提醒與翻身提醒。",
        "usage": "感測墊鋪於床墊下，提供離床提醒。",
        "summary": "",
    }
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert sent["reasoning"] == {"effort": REASONING_EFFORT}
    assert sent["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["store"] is False


@pytest.mark.parametrize(
    ("content", "response", "failure"),
    [
        ({"type": "refusal", "refusal": "No."}, {}, "refused: No."),
        (
            {"type": "output_text", "text": '{"functions": [', "annotations": []},
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            "unreadable output",
        ),
    ],
    ids=["refused", "cut off"],
)
async def test_an_answer_without_facets_says_why(content, response, failure):
    client = openai_answering(content, [], **response)

    read = await OpenAIFacetReader(client, "gpt-test").read(ProductText.of(TEXT))

    assert read.output is None and read.failure.startswith(failure)
