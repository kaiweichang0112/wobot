"""The avatar's frames: seven expressions, each with three mouths (spec 21).

The API, the worker and the app share these keys. Migration 0008 holds a frozen copy in
a CHECK constraint; a new set of keys is a new schema version and a new migration.
"""

EXPRESSION_SCHEMA_VERSION = 1
EXPRESSIONS = ("neutral", "joy", "sadness", "anger", "fear", "surprise", "thinking")
MOUTHS = ("closed", "half", "wide")
FRAME_KEYS = tuple(f"{expression}_{mouth}" for expression in EXPRESSIONS for mouth in MOUTHS)
BASE = "neutral_closed"  # made from the photo


def parent_of(frame_key: str) -> str | None:
    """The frame a frame is made from: the base from the photo, each expression's closed
    frame from the base, and its half and wide frames from that closed frame, so the eyes
    and brows of a mouth frame are its expression's own."""
    if frame_key == BASE:
        return None
    expression, mouth = frame_key.split("_")
    return BASE if mouth == "closed" else f"{expression}_closed"


def descendants(frame_key: str) -> list[str]:
    """The frames made, directly or not, from this one: redone with it."""
    children = [key for key in FRAME_KEYS if parent_of(key) == frame_key]
    return children + [d for child in children for d in descendants(child)]
