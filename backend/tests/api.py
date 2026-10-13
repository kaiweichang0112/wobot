"""Helpers for tests that call the API as signed-in accounts."""

import asyncio
import json
import uuid
from typing import Any

from langchain_core.runnables import RunnableLambda

from tests.database import run_as_migrator

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


def send(client, conversation: str, text: str, turn_id: uuid.UUID | None = None):
    return client.post(
        f"/v1/conversations/{conversation}/turns",
        json={"turn_id": str(turn_id or uuid.uuid4()), "text": text},
        headers=AUTH,
    )


def turn_row(turn_id: uuid.UUID) -> dict:
    rows = asyncio.run(
        run_as_migrator(
            "SELECT t.status, t.reply_text, t.route, t.error_code, t.seq, c.head_checkpoint_id "
            "FROM app.turns t JOIN app.conversations c USING (conversation_id) "
            "WHERE t.turn_id = $1",
            turn_id,
        )
    )
    return dict(rows[0])


def failing_chat() -> RunnableLambda:
    """A chat model whose provider cannot be reached."""

    def fail(_messages):
        raise TimeoutError("the provider did not answer")

    return RunnableLambda(fail)
