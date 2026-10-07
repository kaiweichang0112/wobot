from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.checkpoint.memory import MemorySaver

from tests.agent.conftest import knowledge  # noqa: F401  # a small published version
from tests.agent.fakes import calls, fake_models
from tests.agent.test_graph import Broken
from wobot.agent.graph import build_graph
from wobot.eval.dataset import Case, load_dataset
from wobot.eval.report import turns_markdown
from wobot.eval.turns import TurnCase, play, play_all, turn_cases

QUERY_TIME = datetime(2026, 10, 7, 10, tzinfo=ZoneInfo("Asia/Taipei"))
MASTERS_2022 = {"tool": "list_students", "degree": "master", "year_from": 2022, "year_to": 2022}


def item(turns=("列出 2021 年的碩士畢業生",), last="那 2022 年的呢？", routes=("list",), **check):
    case = Case("TN-1", "test", "dev", last, "", None, {"kind": "turns"}, None, turns=list(turns))
    accepted = check.get("accepted")
    return TurnCase(
        case,
        QUERY_TIME,
        tuple(routes),
        accepted,
        tuple(check.get("question_mentions", ())),
        tuple(check.get("mentions", ())),
    )


def test_the_cases_are_turns_v1s_with_their_earlier_messages():
    cases = turn_cases([load_dataset("turns-v1")], ["dev"])
    by_id = {c.case.case_id: c for c in cases}

    assert len(cases) == 8 and "TN-101" not in by_id
    assert by_id["TN-003"].case.turns == ["請列出 2013 年的碩士畢業生", "張凱維的論文題目是什麼"]
    assert by_id["TN-003"].routes == ("knowledge", "list")
    assert by_id["TN-002"].question_mentions == ("Agicare",) and by_id["TN-002"].accepted is None
    assert by_id["TN-001"].accepted == (({**MASTERS_2022},),)


async def test_the_graph_answers_the_earlier_messages_and_the_last_is_scored(knowledge):  # noqa: F811
    models = fake_models(
        "list",
        list_calls=[
            calls(("list_students", {"degree": "master"})),  # the fixture has no 2021
            calls(("list_students", {"degree": "master", "year_from": 2022, "year_to": 2022})),
        ],
    )
    app = build_graph(models, knowledge.db, knowledge.embedder, MemorySaver())

    played = await play(app, item(accepted=((MASTERS_2022,),)), knowledge.version_id, 0)

    assert played.error is None and played.right
    assert played.earlier == ("沒有找到。",)  # the graph's own reply to the first message
    assert played.route == "list" and played.calls == (MASTERS_2022,)
    assert set(played.nodes) == {"classify", "list_agent", "list_tools", "write_list"}
    # The last turn read the first: the conversation was kept between them.
    [_, (seen, _)] = models.router.calls
    assert [m.text for m in seen] == ["列出 2021 年的碩士畢業生", "沒有找到。", "那 2022 年的呢？"]


async def test_a_last_turn_is_wrong_on_its_path_its_question_or_its_words():
    models = fake_models("chat", chat=["你好！", "好熱喔。"])
    app = build_graph(models, db=None, embedder=None, checkpointer=MemorySaver())

    played = await play(
        app,
        item(turns=["今天好熱"], last="GRC 的電話？", routes=["knowledge"], mentions=["455-5726"]),
        1,
        0,
    )

    assert played.route == "chat" and not played.route_right
    assert played.lacking == ("455-5726",) and played.calls_right is None
    assert not played.right


async def test_conversations_are_played_each_run_and_reported():
    models = replace(fake_models("chat", chat=["嗨"] * 4), chat=Broken())
    app = build_graph(models, db=None, embedder=None, checkpointer=MemorySaver())
    lines: list[str] = []

    result = await play_all(app, [item(turns=[], last="你好", routes=["chat"])], 1, 2, lines.append)

    assert result.correct_per_run() == [0, 0] and result.errors == 2
    assert lines[0].startswith("run 1/2 case 1/1 TN-1: error")
    markdown = turns_markdown(result, [item(turns=[], last="你好", routes=["chat"])], {"runs": 2})
    assert "**0.0 / 1** (0–0), 2 errors." in markdown
    assert "| TN-1 | 2× error |" in markdown
