from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from tests.agent.fakes import fake_chat
from wobot.eval.candidates import Candidate
from wobot.eval.chat import ChatResult, Reply, chat_cases, compare, rule_pick
from wobot.eval.dataset import Case, load_dataset
from wobot.eval.report import chat_markdown


def case(case_id: str, user_input: str = "你好") -> Case:
    return Case(
        case_id=case_id,
        scenario="small_talk",
        split="dev",
        user_input=user_input,
        expected_behavior="",
        reference=None,
        check={"kind": "route", "expect": ["chat"]},
        pending=None,
    )


def test_the_chat_cases_are_those_chat_may_answer():
    cases = chat_cases([load_dataset("route-v1")], ["dev"])

    assert {c.case_id for c in cases} >= {"RT-001", "RT-003", "RT-022"}
    assert all("chat" in c.check["expect"] for c in cases)


async def test_a_reply_is_streamed_and_timed():
    lines: list[str] = []

    [result] = await compare(
        [(Candidate("gpt-6-luna", "none"), fake_chat("你好！ 今天 想 聊 什麼？"))],
        [case("RT-001")],
        runs=1,
        progress=lines.append,
    )

    [reply] = result.replies
    assert reply.text == "你好！ 今天 想 聊 什麼？"
    assert 0 <= reply.first_token_seconds <= reply.seconds
    assert lines == [f"run 1/1 case 1/1 RT-001: gpt-6-luna:none {reply.seconds:.1f}s"]


async def test_a_failed_reply_is_an_error_left_out_of_the_latencies():
    broken = GenericFakeChatModel(messages=iter([]))  # nothing to say: it raises

    [result] = await compare([(Candidate("gpt-6-luna", "none"), broken)], [case("RT-001")], 1)

    assert result.errors == 1 and result.done == []


def result(model: str, seconds: float) -> ChatResult:
    r = ChatResult(Candidate(model, "none"))
    r.replies = [Reply(f"RT-{i}", 0, "嗨", 0.3, seconds, 300, 40) for i in range(5)]
    return r


def test_the_rule_takes_the_fastest_or_the_cheapest_close_to_it():
    luna = result("gpt-6-luna", seconds=1.05)
    older = result("gpt-5.6-luna", seconds=1.00)
    slow = result("gpt-5.5", seconds=3.0)

    assert rule_pick([luna, older, slow]) is luna  # within a tenth of the fastest, cheaper


def test_the_report_shows_each_models_reply_to_each_case():
    cases = [case("RT-0", "你叫什麼名字？")]

    markdown = chat_markdown([result("gpt-6-luna", 1.0)], cases, {"runs": 1})

    assert "**User:** 你叫什麼名字？" in markdown
    assert "- **gpt-6-luna:none**: 嗨" in markdown
    assert "picks **gpt-6-luna:none**" in markdown


def test_a_reply_after_earlier_turns_shows_them():
    earlier = Case(
        **{**case("RT-1").__dict__, "history": [{"user": "列出學生", "answer": "共 5 位"}]}
    )

    markdown = chat_markdown([result("gpt-6-luna", 1.0)], [earlier], {})

    assert "> 列出學生" in markdown and "> — 共 5 位" in markdown
