"""The agent's final reply as a schema: what it says, and the lists code shows in full."""

from pydantic import BaseModel, ConfigDict, Field

# Strict structured output: every field required, and no others.
STRICT = ConfigDict(extra="forbid")


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
    lists: list[ListRef] = Field(
        description="the query_records results to show in full, in order; empty when none"
    )
