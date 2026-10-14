"""The avatar's frames: seven expressions, each with three mouths (spec 21).

The API, the worker and the app share these keys. Migration 0008 holds a frozen copy in
a CHECK constraint; a new set of keys is a new schema version and a new migration.
"""

EXPRESSION_SCHEMA_VERSION = 1
EXPRESSIONS = ("neutral", "joy", "sadness", "anger", "fear", "surprise", "thinking")
MOUTHS = ("closed", "half", "wide")
FRAME_KEYS = tuple(f"{expression}_{mouth}" for expression in EXPRESSIONS for mouth in MOUTHS)
