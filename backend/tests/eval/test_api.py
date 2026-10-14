import asyncio
import json

import httpx2

from wobot.eval.api import Timed, api_cases, by_route, node_spread, nodes_of, play
from wobot.eval.dataset import Case, Dataset
from wobot.eval.report import api_markdown


def case(case_id: str = "RT-006", **extra) -> Case:
    fields = {
        "case_id": case_id,
        "scenario": "fact",
        "split": "dev",
        "user_input": "GRC 是哪一年成立的？",
        "expected_behavior": "",
        "reference": None,
        "check": {"kind": "route", "expect": ["knowledge"]},
        "pending": None,
    }
    return Case(**(fields | extra))


def server(stream: str, status: int = 200) -> tuple[httpx2.AsyncClient, list[str]]:
    """The API as the client meets it; each request is noted as `METHOD path`."""
    seen: list[str] = []
    turn: dict[str, str] = {}

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path == "/v1/conversations":
            return httpx2.Response(201, json={"conversation_id": "c1"})
        if request.method == "POST":
            turn["id"] = json.loads(request.content)["turn_id"]
            return httpx2.Response(status, text=stream)
        if request.method == "GET":
            return httpx2.Response(
                200, json={"turns": [{"turn_id": turn.get("id"), "route": "knowledge"}]}
            )
        return httpx2.Response(204)

    client = httpx2.AsyncClient(base_url="https://api.test", transport=httpx2.MockTransport(handle))
    return client, seen


def test_a_turn_is_timed_by_its_events_and_its_conversation_deleted():
    stream = (
        'event: accepted\ndata: {"attempt": 1}\n\n'
        'event: status\ndata: {"stage": "searching"}\n\n'
        'event: status\ndata: {"stage": "writing"}\n\n'
        'event: text_delta\ndata: {"text": "GRC"}\n\n'
        'event: reply\ndata: {"text": "GRC 成立於 2003 年。"}\n\n'
        "event: completed\ndata: {}\n\n"
    )
    client, seen = server(stream)

    timed = asyncio.run(play(client, case(), run=1))

    assert (timed.status, timed.route, timed.expected) == ("completed", "knowledge", ["knowledge"])
    times = [timed.accepted, timed.first_status, timed.first_token, timed.reply, timed.total]
    assert all(t is not None for t in times) and times == sorted(times)
    assert seen == [
        "POST /v1/conversations",
        "POST /v1/conversations/c1/turns",
        "GET /v1/conversations/c1/turns",
        "DELETE /v1/conversations/c1",
    ]


def test_a_refused_turn_keeps_its_code_and_its_conversation_is_deleted():
    refused = json.dumps({"error": {"code": "TURN_IN_PROGRESS", "message": "busy"}})
    client, seen = server(refused, status=409)

    timed = asyncio.run(play(client, case(), run=1))

    assert timed.status == "TURN_IN_PROGRESS" and timed.total is None
    assert seen[-1] == "DELETE /v1/conversations/c1"


def test_only_first_messages_are_played():
    dataset = Dataset(
        "route-v1",
        "2026-10-06T10:00:00+08:00",
        "Asia/Taipei",
        [
            case("RT-001"),
            case("RT-002", split="heldout"),
            case("RT-019", history=[{"user": "q", "assistant": "a"}]),
            case("RT-040", pending_question="哪裡用？"),
            case("RT-050", turns=["前一句"]),
        ],
        "0" * 64,
    )

    assert [c.case_id for c in api_cases([dataset], ["dev", "heldout"])] == ["RT-001", "RT-002"]
    assert [c.case_id for c in api_cases([dataset], ["dev"])] == ["RT-001"]


def test_cloud_logging_lines_give_each_turn_its_nodes():
    played = [
        Timed("RT-010", 1, ["recommend"], "t1", "completed", "recommend", total=5.0),
        Timed("RT-011", 1, ["recommend"], "t2", "completed", "recommend", total=6.0),
    ]
    entries = [
        {
            "jsonPayload": {
                "message": "turn finished",
                "turn_id": "t1",
                "nodes": [
                    ["classify", 0.3],
                    ["rec_agent", 2.0],
                    ["rec_tools", 0.5],
                    ["rec_agent", 1.5],
                ],
            }
        },
        {"jsonPayload": {"message": "turn finished", "turn_id": "elsewhere", "nodes": []}},
        {"jsonPayload": {"message": "warmed up the OpenAI connection"}},
    ]

    assert nodes_of(played, entries) == 1
    # A node run twice in a turn counts once, with both its times.
    assert node_spread(by_route(played)["recommend"])["rec_agent"] == (3.5, 3.5)


def test_the_report_sets_each_path_against_its_target():
    played = [
        Timed("RT-001", 1, ["chat"], "t1", "completed", "chat", 0.1, 0.4, 1.2, 2.0, 2.1),
        Timed("RT-006", 1, ["knowledge"], "t2", "PROVIDER_UNAVAILABLE", "knowledge"),
    ]

    markdown = api_markdown(played, [], {"cases": 2})

    assert "| chat | 1 | 0.10s / 0.10s (≤ 0.30s) | 0.40s / 0.40s (≤ 0.80s) " in markdown
    assert "1.20s / 1.20s (≤ 1.50s)" in markdown
    assert "- RT-006 run 1: PROVIDER_UNAVAILABLE" in markdown
