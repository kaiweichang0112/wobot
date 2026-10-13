"""Helpers for tests that call the API as signed-in accounts."""

import json
from typing import Any

AUTH = {"Authorization": "Bearer test-token"}
# Two allowlisted accounts, by Firebase UID: the `members` fixture adds and removes them.
MEMBERS = {"uid-a": "a@example.com", "uid-b": "b@example.com"}
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def start(client) -> str:
    """A new conversation of the signed-in account."""
    response = client.post("/v1/conversations", headers=AUTH)
    assert response.status_code == 201
    return response.json()["conversation_id"]


def sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """A server-sent event stream as (event, data) pairs, keepalive comments left out."""
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if not line.startswith(":")
        )
        if fields:
            events.append((fields.get("event", "message"), json.loads(fields["data"])))
    return events
