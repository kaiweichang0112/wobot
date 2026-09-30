"""Content fingerprints: equal content always hashes the same."""

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """One serialization per value: sorted keys, no spaces, non-ASCII kept as is."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
