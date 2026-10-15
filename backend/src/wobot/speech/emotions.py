"""The feeling of each sentence spoken: the avatar's face and the voice's tone (DEC-090).

Jev answers one Choice question per sentence, with its confidence (P20). It was chosen
without a comparison, to move faster (DEC-095): a feeling shown wrongly is worse than
none, so anything short of THRESHOLD, slower than the timeout, or failed is neutral, and
a sentence is never held up long for its feeling.
"""

import asyncio
import json
import logging
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core._api import LangChainBetaWarning
from langchain_typesafe import Choice, TypeSafeClassifier

from wobot.config import Settings

logger = logging.getLogger(__name__)

# Bump with any change to the instructions or criteria; a test pins each version.
PROMPT_VERSION = 1

NEUTRAL = "neutral"

INSTRUCTIONS = (
    "Which feeling the assistant shows in this sentence of its spoken reply, as its "
    "avatar's face and its voice's tone would show it. Judge the sentence's tone and "
    "meaning with the sentence before it and the user's question, not its words alone: "
    "naming a feeling is not showing it. When unsure, choose neutral."
)

CRITERIA = {
    "neutral": "Plain information, facts, numbers, names or lists, or no clear feeling.",
    "joy": "Delight, excitement or warmth: something good was found or happened.",
    "sadness": "Sorrow or sympathy: someone is struggling, or something hoped for is missing.",
    "anger": (
        "Indignation or mock annoyance, such as a playful huff at a tricky question or at "
        "missing information."
    ),
    "fear": "Worry or alarm about someone's safety or a risk.",
    "surprise": "Astonishment at something unexpected.",
    "thinking": "Weighing, wondering or asking back before deciding.",
}
EMOTIONS = tuple(CRITERIA)

# How a feeling is spoken: an audio tag before the sentence, for models that take tags.
TAGS = {
    "joy": "[excited]",
    "sadness": "[sad]",
    "anger": "[annoyed]",
    "fear": "[nervous]",
    "surprise": "[surprised]",
    "thinking": "[thoughtful]",
}

# The feeling of a sentence, from it and the one before.
Feel = Callable[[str, str | None], Awaitable[str]]


class EmotionClassifier:
    """Asks Jev for a sentence's feeling; neutral whenever it is not sure, slow or down."""

    def __init__(self, classifier: Any, threshold: float, timeout: float):
        self.classifier = classifier
        self.threshold = threshold
        self.timeout = timeout

    def for_question(self, question: str) -> Feel:
        """The feeling of each sentence of the reply to this question."""

        async def feel(sentence: str, previous: str | None) -> str:
            return await self.classify(sentence, previous, question)

        return feel

    async def classify(self, sentence: str, previous: str | None, question: str) -> str:
        request = {
            "state": {
                "sentence": sentence,
                "previous_sentence": previous,
                "user_question": question,
            },
            "questions": {"emotion": Choice(instructions=INSTRUCTIONS, criteria=CRITERIA)},
        }
        try:
            response = await asyncio.wait_for(self.classifier.ainvoke(request), self.timeout)
            answer = response.answers["emotion"]
        except Exception as exc:  # the sentence is spoken all the same, as neutral
            logger.warning("no feeling for a sentence: %s", type(exc).__name__)
            return NEUTRAL
        if answer.choice not in EMOTIONS or (answer.confidence or 0) < self.threshold:
            return NEUTRAL
        return answer.choice


def emotion_classifier(settings: Settings) -> EmotionClassifier | None:
    """Jev, when its key is set; without it every sentence is neutral."""
    if settings.typesafe_api_key is None:
        return None
    with warnings.catch_warnings():  # the package says it is in beta: its pin knows
        warnings.simplefilter("ignore", LangChainBetaWarning)
        jev = TypeSafeClassifier(
            model=settings.emotion_model,
            api_key=settings.typesafe_api_key,
            timeout=settings.emotion_timeout_seconds,
        )
    return EmotionClassifier(jev, settings.emotion_threshold, settings.emotion_timeout_seconds)


def criteria_fingerprint() -> str:
    """What the version pins: the instructions and the criteria."""
    return json.dumps([INSTRUCTIONS, CRITERIA], ensure_ascii=False)
