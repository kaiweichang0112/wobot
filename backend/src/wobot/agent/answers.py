"""The agent's final reply as a schema: what it says, what it rests on, and the lists code
shows in full."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Strict structured output: every field required, and no others.
STRICT = ConfigDict(extra="forbid")

Grounding = Literal["grounded", "no_info", "general"]


class ListRef(BaseModel):
    """One query_records result to show in full below the answer."""

    model_config = STRICT

    result_id: str = Field(description="the result_id query_records returned this turn")
    item_ids: list[str] | None = Field(
        description="when the question narrows the list by meaning, the r- IDs of the items "
        "that match, chosen from that result; null to show the whole list"
    )


class Answer(BaseModel):
    """The reply to the user."""

    model_config = STRICT

    answer: str = Field(
        description="what to say, in plain text. When lists are attached, introduce them in "
        "a sentence or two and do not repeat their items: code shows every item below"
    )
    grounding: Grounding = Field(
        description="grounded: the answer rests on what this turn's tools returned, "
        "including when they found what was asked about but it lacks what the user wants; "
        "no_info: you looked it up and the tools returned nothing relevant at all; "
        "general: small talk, general knowledge or writing, with nothing looked up"
    )
    citations: list[str] = Field(
        description="the k- and r- IDs from this turn's tool results that the answer rests "
        "on; code shows their sources. Attached lists need no citations"
    )
    lists: list[ListRef] = Field(
        description="the query_records results to show in full, in order; empty when none"
    )
