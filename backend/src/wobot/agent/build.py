"""The chat agent: one model that picks tools and writes answers (DEC-054)."""

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from wobot.agent.answers import Answer
from wobot.agent.guard import AnswerGuard, TurnArtifacts
from wobot.agent.history import earlier_turns
from wobot.agent.prompts import turn_prompt
from wobot.agent.requirements import ChatState
from wobot.agent.tools import TurnContext
from wobot.config import Settings

# The LangSmith name of every turn's root run.
AGENT_NAME = "wobot-chat"
# Per turn. Of 123 evaluated turns none took more than 5 model calls or 5 tool calls; past
# these the loop is stuck, and the turn ends as a failure to try again.
MODEL_CALLS = 8
TOOL_CALLS = 10


def chat_model(settings: Settings) -> ChatOpenAI:
    effort = settings.agent_reasoning_effort
    return ChatOpenAI(
        model=settings.agent_model,
        reasoning=None if effort == "default" else {"effort": effort},
        # Explicit: without a reasoning setting ChatOpenAI would fall back to Chat
        # Completions, and a comparison of efforts would also compare two APIs.
        use_responses_api=True,
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
        max_retries=3,
    )


def build_agent(
    model: BaseChatModel, tools: list[BaseTool], checkpointer: Checkpointer = None
) -> CompiledStateGraph:
    """The agent graph; each turn is invoked with a TurnContext."""
    return create_agent(
        model,
        tools,
        middleware=[
            turn_prompt,
            earlier_turns(),
            # Ends the turn with no answer, which the reply shows as retryable.
            ModelCallLimitMiddleware(run_limit=MODEL_CALLS, exit_behavior="end"),
            # Refuses the calls past the limit, so the model answers from what it has.
            ToolCallLimitMiddleware(run_limit=TOOL_CALLS),
            AnswerGuard(),
            TurnArtifacts(),
        ],
        # The provider's own structured output, strict, so the reply always parses; every
        # candidate model passed tools and this together in B1.
        response_format=ProviderStrategy(Answer, strict=True),
        state_schema=ChatState,
        context_schema=TurnContext,
        checkpointer=checkpointer,
        name=AGENT_NAME,
    )
