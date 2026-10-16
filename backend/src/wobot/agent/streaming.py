"""One turn of the chat graph as the events a client is sent (DEC-080).

The graph runs once, streamed in three modes at the same time:

- `updates`, each node's output as it finishes: which path the turn took, so the client
  can say what is happening (`status`), how long each node took, and the reply.
- `messages`, the tokens of every model call, tagged with the node that made it. Only
  the nodes in STREAMED_NODES are let through, as `text_delta`.
- `checkpoints`, the checkpoint after each step. With `durability="exit"` only the last
  is saved: the one the conversation's head moves to when the turn completes.

The freeCodeCamp course streams with `stream_mode="values"`, the whole state after each
step: enough to print, but it says neither which node finished nor any token.

The deltas are a preview. The reply, sent whole at the end, is what counts: what the
turn keeps, and what the client shows once it arrives.
"""

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessageChunk
from langgraph.graph.state import CompiledStateGraph

from wobot.agent.graph import STREAMED_NODES

STREAM_MODES = ["updates", "messages", "checkpoints"]


@dataclass
class Played:
    """What a turn did, filled in as it streams."""

    reply: dict[str, Any] = field(default_factory=dict)
    route: str | None = None
    checkpoint_id: str | None = None  # the checkpoint the run saved
    nodes: list[tuple[str, float]] = field(default_factory=list)  # each node, its seconds
    shown: list[str] = field(default_factory=list)  # the deltas sent, in order
    first_status: float | None = None  # seconds from the start of the run
    first_token: float | None = None

    @property
    def shown_text(self) -> str:
        return "".join(self.shown)


def status_after(node: str, update: dict[str, Any]) -> str | None:
    """What the client may say is happening once this node has finished: searching while
    a path looks things up, writing once only words are left to come."""
    if node == "classify" and update.get("status") != "failed":
        return "writing" if update.get("route") == "chat" else "searching"
    if node == "retrieve" and update.get("status") == "ok":
        return "writing"
    return None


async def play(
    graph: CompiledStateGraph,
    turn: dict[str, Any],
    config: dict[str, Any],
    played: Played,
    clock: Callable[[], float] = time.perf_counter,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Runs the turn, yielding (event, data) for `status` and `text_delta` as they happen.
    The reply and how the run went are left in `played` for the caller to keep."""
    started = last = clock()
    status = None
    async for mode, chunk in graph.astream(
        turn, config, stream_mode=STREAM_MODES, durability="exit"
    ):
        now = clock()
        if mode == "messages":
            message, metadata = chunk
            # Chunks only: the whole message a node returns is streamed too, afterwards.
            if (
                isinstance(message, AIMessageChunk)
                and metadata.get("langgraph_node") in STREAMED_NODES
                and message.text
            ):
                if played.first_token is None:
                    played.first_token = now - started
                played.shown.append(message.text)
                yield "text_delta", {"text": message.text}
        elif mode == "checkpoints":
            played.checkpoint_id = chunk["config"]["configurable"]["checkpoint_id"]
        else:
            for node, update in chunk.items():
                update = update or {}
                played.nodes.append((node, round(now - last, 3)))
                if node == "classify" and "route" in update:
                    played.route = update["route"]
                if update.get("reply"):
                    played.reply = update["reply"]
                new = status_after(node, update)
                if new and new != status:
                    status = new
                    if played.first_status is None:
                        played.first_status = now - started
                    yield "status", {"stage": status, "route": played.route}
            last = now
