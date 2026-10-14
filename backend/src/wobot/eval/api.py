"""The deployed API's latency, as a client sees it (Phase C, D21).

Each case's message is sent as the first turn of a new conversation, over the network to
Cloud Run, and the client notes when each event arrives: `accepted`, the first `status`,
the first word, the `reply`, and the turn's last event. The conversation is deleted
afterwards. Only first turns are played: a case with scripted history would need its
earlier answers written into a real conversation, which the API cannot do.

What the graph's nodes took is not visible to a client; each turn's `turn finished` line
in Cloud Logging has it, and `nodes_of` joins those lines to a run by turn ID.

A cold start is measured apart, on `/health`, which needs no token: an idle instance
answers it only after the same start a first message waits for, the app imported and
the lifespan run, and the first turn adds its own seconds to it.
"""

import json
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2

from wobot.eval.candidates import percentile
from wobot.eval.dataset import Case, Dataset

# The warm targets the latency is read against (D10): targets, not gates (spec 02).
# Lists and recommendations aim at Phase B's p50 plus half a second (agent-v1-v2.md).
TARGETS = {
    "accepted": 0.3,
    "first_status": 0.8,
    "first_token": {"chat": 1.5, "knowledge": 3.5},
    "total": {"list": 4.29 + 0.5, "recommend": 4.2 + 0.5},
}
ROUTES = ("chat", "knowledge", "list", "recommend")
TURN_TIMEOUT = 120.0  # beyond the server's 90 s limit
COLD_TIMEOUT = 60.0


def api_cases(datasets: Iterable[Dataset], splits: Sequence[str]) -> list[Case]:
    """The cases a new conversation can play: a first message, as the account's own
    assistant. Timing does not choose models, so a held-out case may be played too."""
    return [
        case
        for dataset in datasets
        for case in dataset.cases
        if case.split in splits
        and not case.history
        and not case.turns
        and case.pending_question is None
        and case.chatbot_name is None
    ]


@dataclass
class Timed:
    """One turn as the client saw it; seconds from sending the message."""

    case_id: str
    run: int
    expected: list[str]
    turn_id: str
    status: str = "unsent"  # completed, failed, or the error code of a refusal
    route: str | None = None  # the path classify chose, read back from the turn
    accepted: float | None = None
    first_status: float | None = None
    first_token: float | None = None
    reply: float | None = None
    total: float | None = None
    nodes: list[list[Any]] = field(default_factory=list)  # from Cloud Logging, if joined


async def play(client: httpx2.AsyncClient, case: Case, run: int) -> Timed:
    """Sends the case's message as the first turn of a new conversation, then deletes
    the conversation."""
    timed = Timed(case.case_id, run, list(case.check["expect"]), str(uuid.uuid4()))
    created = await client.post("/v1/conversations")
    created.raise_for_status()
    conversation = created.json()["conversation_id"]
    try:
        await _stream(client, conversation, case.user_input, timed)
        turns = await client.get(f"/v1/conversations/{conversation}/turns")
        turns.raise_for_status()
        for turn in turns.json()["turns"]:
            if turn["turn_id"] == timed.turn_id:
                timed.route = turn.get("route")
    finally:
        (await client.delete(f"/v1/conversations/{conversation}")).raise_for_status()
    return timed


async def _stream(client: httpx2.AsyncClient, conversation: str, text: str, timed: Timed) -> None:
    body = {"turn_id": timed.turn_id, "text": text}
    began = time.perf_counter()
    async with client.stream(
        "POST", f"/v1/conversations/{conversation}/turns", json=body, timeout=TURN_TIMEOUT
    ) as response:
        if response.status_code != 200:
            await response.aread()
            try:
                timed.status = response.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                timed.status = f"HTTP {response.status_code}"
            return
        event = None
        async for line in response.aiter_lines():
            now = round(time.perf_counter() - began, 3)
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
                continue
            if not line.startswith("data:"):
                continue
            match event:
                case "accepted":
                    timed.accepted = now
                case "status" if timed.first_status is None:
                    timed.first_status = now
                case "text_delta" if timed.first_token is None:
                    timed.first_token = now
                case "reply":
                    timed.reply = now
                case "completed":
                    timed.status, timed.total = "completed", now
                case "error":
                    data = json.loads(line.removeprefix("data:"))
                    timed.status, timed.total = data.get("code", "error"), now


async def play_all(
    client: httpx2.AsyncClient,
    cases: Sequence[Case],
    runs: int,
    progress: Callable[[str], None] = lambda _: None,
) -> list[Timed]:
    """Each case `runs` times, one at a time: turns in parallel would compete for the
    same instances and measure that instead."""
    played = []
    for run in range(1, runs + 1):
        for case in cases:
            timed = await play(client, case, run)
            played.append(timed)
            progress(
                f"run {run} {case.case_id} {timed.route or '-':<9} {timed.status:<10} "
                f"accepted {_s(timed.accepted)}  first word {_s(timed.first_token)}  "
                f"total {_s(timed.total)}"
            )
    return played


@dataclass
class Cold:
    """A request that waited for an instance to start."""

    idle_minutes: float
    seconds: float
    version: str | None


async def health(client: httpx2.AsyncClient) -> tuple[float, str | None]:
    began = time.perf_counter()
    response = await client.get("/health", timeout=COLD_TIMEOUT)
    seconds = round(time.perf_counter() - began, 3)
    response.raise_for_status()
    return seconds, response.json().get("version")


def nodes_of(played: Sequence[Timed], log_entries: Iterable[Mapping[str, Any]]) -> int:
    """Joins `turn finished` lines from Cloud Logging (`gcloud logging read --format
    json`) to the run's turns by turn ID. How many were joined."""
    by_turn = {t.turn_id: t for t in played}
    joined = 0
    for entry in log_entries:
        payload = entry.get("jsonPayload") or {}
        timed = by_turn.get(payload.get("turn_id", ""))
        if timed is not None and payload.get("message") == "turn finished":
            timed.nodes = payload.get("nodes") or []
            joined += 1
    return joined


def by_route(played: Sequence[Timed]) -> dict[str, list[Timed]]:
    groups: dict[str, list[Timed]] = defaultdict(list)
    for timed in played:
        if timed.status == "completed" and timed.route:
            groups[timed.route].append(timed)
    return {route: groups[route] for route in ROUTES if route in groups}


def spread(values: Sequence[float | None]) -> tuple[float, float] | None:
    """p50 and p95 of the values present; None when there are none."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return percentile(present, 0.5), percentile(present, 0.95)


def node_spread(turns: Sequence[Timed]) -> dict[str, tuple[float, float]]:
    """Each node's p50 and p95 seconds, a node run twice in a turn counted as one."""
    seconds: dict[str, list[float]] = defaultdict(list)
    for timed in turns:
        per_turn: dict[str, float] = defaultdict(float)
        for node, took in timed.nodes:
            per_turn[node] += took
        for node, took in per_turn.items():
            seconds[node].append(took)
    return {node: spread(values) for node, values in seconds.items()}  # type: ignore[misc]


def _s(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"
