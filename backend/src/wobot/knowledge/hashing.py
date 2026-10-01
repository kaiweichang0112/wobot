"""Content fingerprints: equal content always hashes the same."""

import hashlib
import json
from datetime import date
from typing import Any


def canonical_json(value: Any) -> str:
    """One serialization per value: sorted keys, no spaces, non-ASCII kept as is."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
    )


def _json_default(value: Any) -> str:
    if isinstance(value, date):  # project periods; datetime is a date too
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} has no canonical JSON form")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
