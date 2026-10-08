"""The chat graph: a router, three fixed paths and two bounded agent loops.

Every turn starts at `classify`, which only names the kind of question. Small talk goes
to `chat_reply`; facts go through `rewrite_query` and `retrieve` to `answer`; lists loop
between `list_agent` and `list_tools` before `write_list`; recommendations loop between
`rec_agent` and `rec_tools` until the agent replies or a recommendation passes code's
guards (DEC-066). A database or provider failure on any path ends at `report_failure`.

Nodes that call a model are made inside `build_graph`, so they hold the model they were
given; the rest are stubs until their path is built. The edges are final, and a test
holds the drawing to `tests/agent/expected_graph.mmd`.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from wobot.agent.answer import answer_prompt
from wobot.agent.chat import chat_prompt
from wobot.agent.list_agent import (
    LIST_TOOLS,
    list_agent_prompt,
    lists_will_do,
    run_list_call,
    turn_date,
)
from wobot.agent.models import openai_model
from wobot.agent.rec_agent import (
    REC_TOOLS,
    RecommendProducts,
    SearchProducts,
    answered,
    own_words,
    read_call,
    rec_agent_prompt,
    search_products,
)
from wobot.agent.recommend import (
    fallback_reply,
    recommended_before,
    recommended_reply,
    refusal,
    review,
)
from wobot.agent.retrieval import UNAVAILABLE, LookupFailed, records_named, search_knowledge
from wobot.agent.rewrite import SearchPlan, read_plan, rewrite_prompt, writes_chinese
from wobot.agent.route import ROUTING_FAILED, Route, Router, chat_router
from wobot.agent.write_list import ListIntro, list_reply, lists_that_count, write_list_prompt
from wobot.config import Settings
from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.repository import Database

logger = logging.getLogger(__name__)

# Tool rounds one turn's loop may take before it answers from what it has.
MAX_LIST_ROUNDS = 3
MAX_REC_ROUNDS = 3


class ChatState(TypedDict):
    # Kept across turns by the checkpointer: the conversation as text, without tool
    # results.
    messages: Annotated[list[BaseMessage], add_messages]
    pending_question: str | None  # the last turn's question, read by the next one's route

    # Given by the caller each turn.
    index_version: int
    query_time: datetime
    chatbot_name: str

    # This turn's work, cleared by classify.
    route: Route
    route_confidence: float | None  # Jev's, when Jev routes
    route_fallback: bool  # Jev failed, and the fallback model routed
    question: str  # the question, standalone, in the user's language
    queries: list[str]  # the question in Chinese and in English, as searched
    name: str | None  # a person, talk or project asked about by name
    evidence: dict[str, Any]  # what the tools found, by handle
    list_messages: list[BaseMessage]  # the list loop's scratch: replaced, never added to
    list_results: dict[str, Any]  # every list found, in full, by result_id
    rec_messages: list[BaseMessage]  # the recommendation loop's scratch
    tool_rounds: int
    status: Literal["ok", "failed"]
    reply: dict[str, Any]  # what the user is shown or told


def fresh_turn() -> dict[str, Any]:
    """What classify sets before any path runs: a turn starts from none of the last
    one's work. New lists each time, so no two turns share one."""
    return {
        "question": "",
        "queries": [],
        "name": None,
        "evidence": {},
        "list_messages": [],
        "list_results": {},
        "rec_messages": [],
        "tool_rounds": 0,
        "status": "ok",
        "reply": {},
    }


# --- Nodes not built yet: stubs that change nothing -----------------------------------


def latest_user_text(messages: Sequence[BaseMessage]) -> str:
    return next(m.text for m in reversed(messages) if isinstance(m, HumanMessage))


FAILURE_TEXT = {
    "zh": "抱歉，資料暫時查不到，請稍後再試一次。",
    "en": "Sorry, I could not look that up just now. Please try again in a moment.",
}


def failure_reply(messages: Sequence[BaseMessage]) -> dict[str, Any]:
    """A reply that the lookup or the model failed and may be tried again, in the user's
    language as far as code can tell: Chinese if the message has a Chinese character."""
    text = FAILURE_TEXT["zh" if writes_chinese(latest_user_text(messages)) else "en"]
    return {"messages": [AIMessage(text)], "reply": {"text": text, "retryable": True}}


def report_failure(state: ChatState) -> dict[str, Any]:
    """Says the lookup failed and can be tried again; never that nothing was found."""
    return failure_reply(state["messages"])


# --- Routing: each returns a label that the edges map to the next node ---------------


def route_by_intent(state: ChatState) -> Route | Literal["failed"]:
    return "failed" if state["status"] == "failed" else state["route"]


def ok_or_failed(state: ChatState) -> Literal["ok", "failed"]:
    return "failed" if state["status"] == "failed" else "ok"


def _wants_tools(messages: list[BaseMessage], rounds: int, limit: int) -> bool:
    """The agent's last message called tools, and the loop has rounds left."""
    return bool(messages and getattr(messages[-1], "tool_calls", None)) and rounds < limit


def list_should_continue(state: ChatState) -> Literal["tools", "done"]:
    if _wants_tools(state["list_messages"], state["tool_rounds"], MAX_LIST_ROUNDS):
        return "tools"
    return "done"


def rec_should_continue(state: ChatState) -> Literal["tools", "answered", "failed"]:
    """The agent called tools, or replied itself; code replies when its rounds ran out."""
    if state["status"] == "failed":
        return "failed"
    if not state["reply"] and getattr(state["rec_messages"][-1], "tool_calls", None):
        return "tools"
    return "answered"


def after_rec_tools(state: ChatState) -> Literal["next", "recommended", "failed"]:
    """A recommendation the guards passed is the reply; anything else goes back."""
    if state["status"] == "failed":
        return "failed"
    return "recommended" if state["reply"] else "next"


# --- The graph -------------------------------------------------------------------------


@dataclass(frozen=True)
class Models:
    """The model each model node calls (DV11)."""

    router: Router
    chat: BaseChatModel
    rewrite: BaseChatModel
    answer: BaseChatModel
    list_agent: BaseChatModel
    write_list: BaseChatModel
    rec_agent: BaseChatModel


def models_for(settings: Settings) -> Models:
    return Models(
        router=chat_router(settings),
        chat=openai_model(settings, settings.chat_model, settings.chat_effort),
        rewrite=openai_model(settings, settings.rewrite_model, settings.rewrite_effort),
        answer=openai_model(settings, settings.answer_model, settings.answer_effort),
        list_agent=openai_model(settings, settings.list_agent_model, settings.list_agent_effort),
        write_list=openai_model(settings, settings.write_list_model, settings.write_list_effort),
        rec_agent=openai_model(settings, settings.rec_agent_model, settings.rec_agent_effort),
    )


def build_graph(
    models: Models,
    db: Database,
    embedder: Embedder,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """The graph, its nodes holding the models, database and embedder given. With a
    checkpointer, each thread's state is kept between turns; without, every call starts
    from the input alone."""
    planner = models.rewrite.with_structured_output(SearchPlan, method="json_schema")
    # The first round must call a tool: a list question is always looked up (DV3). Later
    # rounds may stop once the lists found will do.
    list_first = models.list_agent.bind_tools(LIST_TOOLS, tool_choice="required")
    list_next = models.list_agent.bind_tools(LIST_TOOLS)
    list_writer = models.write_list.with_structured_output(ListIntro, method="json_schema")
    # Not made to call a tool: the agent may ask what the user needs first (DEC-066).
    rec_model = models.rec_agent.bind_tools(REC_TOOLS)

    async def classify(state: ChatState) -> dict[str, Any]:
        """Names the kind of question: chat, knowledge, list or recommend. The last turn's
        question is read here, then cleared: whatever the route, this message answers it."""
        try:
            routed = await models.router(state["messages"], state.get("pending_question"))
        except ROUTING_FAILED:
            # Neither router answered: the question stays for the message sent again.
            logger.exception("routing failed")
            return {**fresh_turn(), "status": "failed"}
        return {
            "route": routed.route,
            "route_confidence": routed.confidence,
            "route_fallback": routed.fallback,
            "pending_question": None,
            **fresh_turn(),
        }

    async def chat_reply(state: ChatState) -> dict[str, Any]:
        """Small talk, general knowledge and writing, with no lookup."""
        prompt = chat_prompt(state["chatbot_name"], state["messages"])
        try:
            response = await models.chat.ainvoke(prompt)
        except UNAVAILABLE:
            logger.exception("chat reply failed")
            return failure_reply(state["messages"])
        # Only the words are kept: the conversation holds text, not the provider's blocks.
        text = response.text
        return {"messages": [AIMessage(text)], "reply": {"text": text}}

    async def rewrite_query(state: ChatState) -> dict[str, Any]:
        """The question as it stands alone, and the queries to search for it."""
        latest = latest_user_text(state["messages"])
        try:
            plan = await planner.ainvoke(rewrite_prompt(state["messages"]))
        except UNAVAILABLE:
            # Searching the user's own words still finds something: phase A's baseline.
            logger.exception("rewriting the question failed; searching the message as it is")
            return {"question": latest, "queries": [latest], "name": None}
        return read_plan(plan, latest)

    async def retrieve(state: ChatState) -> dict[str, Any]:
        """The passages closest to the queries, and the records holding the name asked."""
        version = state["index_version"]
        try:
            passages = await search_knowledge(db, embedder, version, state["queries"])
            records = await records_named(db, version, state["name"]) if state["name"] else []
        except LookupFailed:
            return {"status": "failed"}
        return {"evidence": {"passages": passages, "records": records}, "status": "ok"}

    async def answer(state: ChatState) -> dict[str, Any]:
        """The answer, from this turn's evidence only."""
        prompt = answer_prompt(
            latest_user_text(state["messages"]),
            state["question"],
            state["evidence"],
            state["chatbot_name"],
        )
        try:
            response = await models.answer.ainvoke(prompt)
        except UNAVAILABLE:
            logger.exception("answer failed")
            return failure_reply(state["messages"])
        text = response.text
        return {"messages": [AIMessage(text)], "reply": {"text": text}}

    async def list_agent(state: ChatState) -> dict[str, Any]:
        """Picks the list tool and filters, or decides the lists found will do."""
        scratch = state["list_messages"]
        # Neither asks the model: no round is left, or every list was found. Either way
        # the loop ends on the tools' answers, and write_list answers from the lists.
        if state["tool_rounds"] >= MAX_LIST_ROUNDS or lists_will_do(scratch):
            return {}
        model = list_next if scratch else list_first
        prompt = list_agent_prompt(state["messages"], turn_date(state["query_time"]))
        try:
            response = await model.ainvoke([*prompt, *scratch])
        except UNAVAILABLE:
            logger.exception("list agent failed")
            return {"status": "failed"}
        return {"list_messages": [*scratch, response]}

    async def list_tools(state: ChatState) -> dict[str, Any]:
        """Runs the list tools the agent called and keeps every list in full."""
        scratch = state["list_messages"]
        today = turn_date(state["query_time"])
        results = dict(state["list_results"])
        answers = []
        try:
            for call in scratch[-1].tool_calls:
                answer, found = await run_list_call(db, state["index_version"], today, call)
                answers.append(answer)
                if found is not None:
                    results[found["result_id"]] = found
        except LookupFailed:
            return {"status": "failed"}
        return {
            "list_messages": [*scratch, *answers],
            "list_results": results,
            "tool_rounds": state["tool_rounds"] + 1,
            "status": "ok",
        }

    async def write_list(state: ChatState) -> dict[str, Any]:
        """Introduces the lists to show; code renders their items."""
        if state["status"] == "failed":  # the agent's model could not be reached
            return failure_reply(state["messages"])
        latest = latest_user_text(state["messages"])
        results = lists_that_count(state["list_results"])
        try:
            intro = await list_writer.ainvoke(write_list_prompt(latest, results))
        except UNAVAILABLE:
            logger.exception("writing the list failed")
            return failure_reply(state["messages"])
        reply, remembered = list_reply(intro, results, writes_chinese(latest))
        return {"messages": [AIMessage(remembered)], "reply": reply}

    async def rec_agent(state: ChatState) -> dict[str, Any]:
        """Asks the user, calls a tool or says why nothing fits. With no round left, code
        names what was found instead."""
        messages = state["messages"]
        chinese = writes_chinese(latest_user_text(messages))
        if state["tool_rounds"] >= MAX_REC_ROUNDS:
            reply = fallback_reply(state["evidence"], chinese)
        else:
            scratch = state["rec_messages"]
            prompt = rec_agent_prompt(messages, state["chatbot_name"])
            try:
                response = await rec_model.ainvoke([*prompt, *scratch])
            except UNAVAILABLE:
                logger.exception("recommendation agent failed")
                return {"status": "failed"}
            if response.tool_calls:
                return {"rec_messages": [*scratch, response]}
            text = own_words(response.text)
            if text:
                reply = {"text": text, "action": "replied", "products": []}
            else:  # neither a tool nor a word: code says what it has
                reply = fallback_reply(state["evidence"], chinese)
        # A reply asks or explains; either way the user's next message may answer it.
        return {
            "reply": reply,
            "messages": [AIMessage(reply["text"])],
            "pending_question": reply["text"],
        }

    async def rec_tools(state: ChatState) -> dict[str, Any]:
        """Runs the agent's tool calls in turn: a search adds the products it found to the
        evidence; a recommendation is held to the guards and, once passed, is the reply."""
        scratch = state["rec_messages"]
        evidence = state["evidence"]
        chinese = writes_chinese(latest_user_text(state["messages"]))
        answers = []
        reply: dict[str, Any] = {}
        for call in scratch[-1].tool_calls:
            args = read_call(call)
            if isinstance(args, SearchProducts):
                try:
                    answer, evidence = await search_products(
                        db, embedder, state["index_version"], call, args, evidence
                    )
                except LookupFailed:
                    return {"status": "failed"}
            elif isinstance(args, RecommendProducts):
                earlier = (m.text for m in state["messages"] if isinstance(m, AIMessage))
                found = review(args, evidence, frozenset(recommended_before(earlier)))
                if found.shown:
                    reply = recommended_reply(args, found.shown, evidence, chinese)
                    answer = answered(call, {"shown": [p.product_id for p in found.shown]})
                else:
                    answer = answered(call, {"refused": refusal(found.problems)})
            else:  # the call was refused: the agent reads why
                answer = args
            answers.append(answer)
        update: dict[str, Any] = {
            "rec_messages": [*scratch, *answers],
            "evidence": evidence,
            "tool_rounds": state["tool_rounds"] + 1,
            "status": "ok",
        }
        if reply:
            update |= {
                "reply": reply,
                "messages": [AIMessage(reply["text"])],
                "pending_question": None,
            }
        return update

    graph = StateGraph(ChatState)

    # The order nodes are added in is the order they are drawn in.
    graph.add_node("classify", classify)
    graph.add_node("chat_reply", chat_reply)
    graph.add_node("rewrite_query", rewrite_query)
    graph.add_node("retrieve", retrieve)
    graph.add_node("answer", answer)
    graph.add_node("list_agent", list_agent)
    graph.add_node("list_tools", list_tools)
    graph.add_node("write_list", write_list)
    graph.add_node("rec_agent", rec_agent)
    graph.add_node("rec_tools", rec_tools)
    graph.add_node("report_failure", report_failure)

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        route_by_intent,
        {
            "chat": "chat_reply",
            "knowledge": "rewrite_query",
            "list": "list_agent",
            "recommend": "rec_agent",
            "failed": "report_failure",
        },
    )

    # Knowledge: a fixed pipeline.
    graph.add_edge("rewrite_query", "retrieve")
    graph.add_conditional_edges(
        "retrieve", ok_or_failed, {"ok": "answer", "failed": "report_failure"}
    )

    # Lists: the agent loops through the tools until the lists will do.
    graph.add_conditional_edges(
        "list_agent", list_should_continue, {"tools": "list_tools", "done": "write_list"}
    )
    graph.add_conditional_edges(
        "list_tools", ok_or_failed, {"ok": "list_agent", "failed": "report_failure"}
    )

    # Recommendations: the agent loops through the tools until it replies, or until a
    # recommendation passes the guards and is shown.
    graph.add_conditional_edges(
        "rec_agent",
        rec_should_continue,
        {"tools": "rec_tools", "answered": END, "failed": "report_failure"},
    )
    graph.add_conditional_edges(
        "rec_tools",
        after_rec_tools,
        {"next": "rec_agent", "recommended": END, "failed": "report_failure"},
    )

    for last in ("chat_reply", "answer", "write_list", "report_failure"):
        graph.add_edge(last, END)

    return graph.compile(checkpointer=checkpointer)
