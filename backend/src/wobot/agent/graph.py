"""The chat graph: a router, three fixed paths and two bounded agent loops.

Every turn starts at `classify`, which only names the kind of question. Small talk goes
to `chat_reply`; facts go through `rewrite_query` and `retrieve` to `answer`; lists loop
between `list_agent` and `list_tools` before `write_list`; recommendations record the
user's needs, loop between `rec_agent` and `rec_tools`, then `decide` and `check`. A
database or provider failure on any path ends at `report_failure`.

Nodes that call a model are made inside `build_graph`, so they hold the model they were
given; the rest are stubs until their path is built. The edges are final, and a test
holds the drawing to `tests/agent/expected_graph.mmd`.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from wobot.agent.chat import chat_prompt
from wobot.agent.route import Route, Router

# Tool rounds one turn's loop may take before it answers from what it has.
MAX_LIST_ROUNDS = 3
MAX_REC_ROUNDS = 4


class ChatState(TypedDict):
    # Kept across turns by the checkpointer: the conversation as text, without tool
    # results, and what the user needs from a product.
    messages: Annotated[list[BaseMessage], add_messages]
    requirements: dict[str, Any] | None
    pending_question: str | None  # the last turn's clarifying question, until answered

    # Given by the caller each turn.
    index_version: int
    query_time: datetime
    chatbot_name: str

    # This turn's work, cleared by classify.
    route: Route
    route_confidence: float | None  # Jev's, when Jev routes
    question: str  # the question, standalone
    queries: list[str]  # what to search for, in the user's language and English
    name: str | None  # a person, talk or project asked about by name
    evidence: dict[str, Any]  # what the tools found, by handle
    list_messages: list[BaseMessage]  # the list loop's scratch: replaced, never added to
    list_results: dict[str, Any]  # every list found, in full, by result_id
    rec_messages: list[BaseMessage]  # the recommendation loop's scratch
    tool_rounds: int
    decision: dict[str, Any] | None
    problems: list[str]  # what check found wrong with the decision
    retries: int
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
        "decision": None,
        "problems": [],
        "retries": 0,
        "status": "ok",
        "reply": {},
    }


# --- Nodes not built yet: stubs that change nothing -----------------------------------


def rewrite_query(state: ChatState) -> dict[str, Any]:
    """The question as it stands alone, and the queries to search for it."""
    return {}


def retrieve(state: ChatState) -> dict[str, Any]:
    """The passages closest to the queries, and the records holding the name asked."""
    return {}


def answer(state: ChatState) -> dict[str, Any]:
    """The answer, from this turn's evidence only."""
    return {}


def list_agent(state: ChatState) -> dict[str, Any]:
    """Picks the list tool and filters, or decides the lists found will do."""
    return {}


def list_tools(state: ChatState) -> dict[str, Any]:
    """Runs the list tools the agent called and keeps every list in full."""
    return {}


def write_list(state: ChatState) -> dict[str, Any]:
    """Introduces the lists to show; code renders their items."""
    return {}


def update_needs(state: ChatState) -> dict[str, Any]:
    """Records what the user needs from a product, numbered and versioned."""
    return {}


def rec_agent(state: ChatState) -> dict[str, Any]:
    """Picks what to look up next about the candidate products."""
    return {}


def rec_tools(state: ChatState) -> dict[str, Any]:
    """Runs the product tools the agent called and gathers their evidence."""
    return {}


def decide(state: ChatState) -> dict[str, Any]:
    """Clarify, recommend or explain the limitation, checking each need per product."""
    return {}


def check(state: ChatState) -> dict[str, Any]:
    """Holds the decision to the runtime guards, and shows it once it passes."""
    return {}


def report_failure(state: ChatState) -> dict[str, Any]:
    """Says the lookup failed and can be tried again; never that nothing was found."""
    return {}


# --- Routing: each returns a label that the edges map to the next node ---------------


def route_by_intent(state: ChatState) -> Route:
    return state["route"]


def ok_or_failed(state: ChatState) -> Literal["ok", "failed"]:
    return "failed" if state["status"] == "failed" else "ok"


def search_or_ask(state: ChatState) -> Literal["search", "ask"]:
    """Search once the needs say what the product is for and one thing it must do."""
    needs = state.get("requirements")
    if needs and needs["goal"] and needs["must_have"]:
        return "search"
    return "ask"


def _wants_tools(messages: list[BaseMessage], rounds: int, limit: int) -> bool:
    """The agent's last message called tools, and the loop has rounds left."""
    return bool(messages and getattr(messages[-1], "tool_calls", None)) and rounds < limit


def list_should_continue(state: ChatState) -> Literal["tools", "done"]:
    if _wants_tools(state["list_messages"], state["tool_rounds"], MAX_LIST_ROUNDS):
        return "tools"
    return "done"


def rec_should_continue(state: ChatState) -> Literal["tools", "done"]:
    if _wants_tools(state["rec_messages"], state["tool_rounds"], MAX_REC_ROUNDS):
        return "tools"
    return "done"


def pass_or_retry(state: ChatState) -> Literal["pass", "retry"]:
    return "retry" if state["problems"] else "pass"


# --- The graph -------------------------------------------------------------------------


def build_graph(router: Router, chat_model: BaseChatModel) -> CompiledStateGraph:
    """The graph, its model nodes holding the router and chat model given."""

    async def classify(state: ChatState) -> dict[str, Any]:
        """Names the kind of question: chat, knowledge, list or recommend."""
        routed = await router(state["messages"], state.get("pending_question"))
        return {"route": routed.route, "route_confidence": routed.confidence, **fresh_turn()}

    async def chat_reply(state: ChatState) -> dict[str, Any]:
        """Small talk, general knowledge and writing, with no lookup."""
        response = await chat_model.ainvoke(chat_prompt(state["chatbot_name"], state["messages"]))
        # Only the words are kept: the conversation holds text, not the provider's blocks.
        text = response.text
        return {"messages": [AIMessage(text)], "reply": {"text": text}}

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
    graph.add_node("update_needs", update_needs)
    graph.add_node("rec_agent", rec_agent)
    graph.add_node("rec_tools", rec_tools)
    graph.add_node("decide", decide)
    graph.add_node("check", check)
    graph.add_node("report_failure", report_failure)

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        route_by_intent,
        {
            "chat": "chat_reply",
            "knowledge": "rewrite_query",
            "list": "list_agent",
            "recommend": "update_needs",
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

    # Recommendations: needs first, then the loop, the decision and its check.
    graph.add_conditional_edges(
        "update_needs", search_or_ask, {"search": "rec_agent", "ask": "decide"}
    )
    graph.add_conditional_edges(
        "rec_agent", rec_should_continue, {"tools": "rec_tools", "done": "decide"}
    )
    graph.add_conditional_edges(
        "rec_tools", ok_or_failed, {"ok": "rec_agent", "failed": "report_failure"}
    )
    graph.add_edge("decide", "check")
    graph.add_conditional_edges("check", pass_or_retry, {"pass": END, "retry": "rec_agent"})

    for last in ("chat_reply", "answer", "write_list", "report_failure"):
        graph.add_edge(last, END)

    return graph.compile()
