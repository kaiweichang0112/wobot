"""wobot-chat: talk with the chat agent in a terminal, one conversation per thread.

Conversations are kept in the local database's checkpoint tables, as the API's role,
the way the app will keep them; a thread can be taken up again later. Run from backend/:

    DB_USER=wobot_api_user uv run --env-file .env wobot-chat [--thread <id>] [--name <name>]
"""

import argparse
import asyncio
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage
from openai import AsyncOpenAI
from sqlalchemy import select

from wobot.agent.build import build_agent, chat_model
from wobot.agent.checkpoints import open_checkpointer
from wobot.agent.guard import this_turn
from wobot.agent.render import ReplyStatus, turn_reply
from wobot.agent.tools import TurnContext, build_tools
from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.knowledge.embeddings import OpenAIEmbedder
from wobot.knowledge.models import ActiveKnowledge


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wobot-chat", description=__doc__.splitlines()[0])
    parser.add_argument("--thread", help="take up this conversation again (default: a new one)")
    parser.add_argument("--name", default="Wobot", help="what you call the assistant")
    args = parser.parse_args(argv)
    return asyncio.run(_chat(args, get_settings()))


async def _chat(args: argparse.Namespace, settings: Settings) -> int:
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY is not set")
        return 1
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=3,
    )
    tools_engine, connector = await create_engine(settings)
    config = {"configurable": {"thread_id": args.thread or f"chat-{uuid.uuid4()}"}}
    try:
        async with open_checkpointer(settings) as saver:
            tools = build_tools(tools_engine, OpenAIEmbedder(client, settings.embedding_model))
            agent = build_agent(chat_model(settings), tools, saver)
            earlier = (await agent.aget_state(config)).values.get("messages", [])
            turns = sum(isinstance(m, HumanMessage) for m in earlier)
            print(
                f"{settings.agent_model} ({settings.agent_reasoning_effort}), "
                f"thread {config['configurable']['thread_id']}"
                + (f", {turns} earlier turns" if turns else "")
                + ". Ctrl-D quits; --thread takes it up again."
            )
            while (question := await _ask()) is not None:
                if not question:
                    continue
                async with tools_engine.connect() as conn:
                    version = await conn.scalar(select(ActiveKnowledge.index_version_id))
                if version is None:
                    print("no active knowledge version: run wobot-ingest first")
                    return 1
                context = TurnContext("local-chat", datetime.now(UTC), version, args.name)
                state = await agent.ainvoke(
                    {"messages": [HumanMessage(question)]}, config, context=context
                )
                messages = this_turn(state["messages"])
                for message in messages:
                    if isinstance(message, AIMessage):
                        for call in message.tool_calls:
                            args_text = json.dumps(call["args"], ensure_ascii=False)
                            print(f"  · {call['name']} {args_text}")
                for held in state.get("retries", []):
                    print(f"  · asked again, {held['reason']}: {'; '.join(held['detail'])}")
                reply = turn_reply(
                    messages,
                    state.get("structured_response"),
                    context.artifacts,
                    state.get("requirements"),
                )
                print(f"\n{reply.text}")
                if reply.status is not ReplyStatus.ANSWERED:
                    print(f"  [{reply.status}: {'; '.join(reply.problems)}]")
    finally:
        await tools_engine.dispose()
        if connector is not None:
            await connector.close_async()
    return 0


async def _ask() -> str | None:
    """The next question, or None at the end of input."""
    try:
        return (await asyncio.to_thread(input, "\n> ")).strip()
    except EOFError:
        return None
