"""wobot-chat: talk with the chat graph in a terminal, one conversation per thread.

Conversations are kept in the local database's checkpoint tables, as the API's role,
the way the app will keep them; a thread can be taken up again later. Each turn shows
the nodes it passed through, their seconds and what they found, then the reply. Run from
backend/ (with --env-file, LangSmith traces are sent if .env turns them on):

    DB_USER=wobot_api_user uv run --env-file .env wobot-chat [--thread <id>] [--name <name>]
"""

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import takewhile
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from openai import AsyncOpenAI
from sqlalchemy import select

from wobot.agent.checkpoints import open_checkpointer
from wobot.agent.graph import build_graph, models_for
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
    engine, connector = await create_engine(settings)
    thread_id = args.thread or f"chat-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}
    try:
        async with open_checkpointer(settings) as saver:
            embedder = OpenAIEmbedder(client, settings.embedding_model)
            app = build_graph(models_for(settings), engine, embedder, saver)
            earlier = (await app.aget_state(config)).values.get("messages", [])
            turns = sum(isinstance(m, HumanMessage) for m in earlier)
            print(
                f"thread {thread_id}"
                + (f", {turns} earlier turns" if turns else "")
                + ". Ctrl-D quits; --thread takes it up again."
            )
            while (message := await _ask()) is not None:
                if not message:
                    continue
                async with engine.connect() as conn:
                    version = await conn.scalar(select(ActiveKnowledge.index_version_id))
                if version is None:
                    print("no active knowledge version: run wobot-ingest first")
                    return 1
                turn = {
                    "messages": [HumanMessage(message)],
                    "index_version": version,
                    "query_time": datetime.now(UTC),
                    "chatbot_name": args.name,
                }
                await play_turn(app, turn, config)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()
    return 0


async def play_turn(app: CompiledStateGraph, turn: dict[str, Any], config: dict) -> None:
    """One turn, each node printed as it finishes, then the reply."""
    final: dict[str, Any] = {}
    last = time.perf_counter()
    async for mode, chunk in app.astream(turn, config, stream_mode=["updates", "values"]):
        if mode == "values":
            final = chunk
            continue
        now = time.perf_counter()
        for node, update in chunk.items():
            note = describe(node, update or {})
            print(f"  · {node:<14} {now - last:5.2f}s" + (f"  {note}" if note else ""))
        last = now
    print(f"\n{final.get('reply', {}).get('text', '(no reply)')}")


def describe(node: str, update: dict[str, Any]) -> str:
    """What a node found, in a line: enough to follow the path a turn took."""
    if node == "classify":
        confidence = update.get("route_confidence")
        return f"route {update['route']}" + (f" ({confidence:.2f})" if confidence else "")
    if node == "rewrite_query":
        name = f", name {update['name']}" if update.get("name") else ""
        return f"{update['question']}{name}"
    if node in ("retrieve", "list_tools") and update.get("status") == "failed":
        return "failed"
    if node == "retrieve":
        evidence = update["evidence"]
        return f"{len(evidence['passages'])} passages, {len(evidence['records'])} records"
    if node == "list_agent":
        if not update:
            return "no model call: the lists will do"
        if update.get("status") == "failed":
            return "failed"
        calls = update["list_messages"][-1].tool_calls
        return "; ".join(_call(c["name"], c["args"]) for c in calls) if calls else "done"
    if node == "list_tools":
        answers = takewhile(lambda m: isinstance(m, ToolMessage), reversed(update["list_messages"]))
        return "; ".join(_answer(m) for m in reversed(list(answers)))
    return ""


def _answer(message: ToolMessage) -> str:
    """A tool's answer as the model read it: the list and its count, or why it was refused."""
    if message.status == "error":
        return message.text
    content = json.loads(message.text)
    return f"{content['list']} {content['count']}"


def _call(name: str, args: dict[str, Any]) -> str:
    given = {k: v for k, v in args.items() if v is not None}
    return f"{name} {json.dumps(given, ensure_ascii=False)}"


async def _ask() -> str | None:
    """The next message, or None at the end of input."""
    try:
        return (await asyncio.to_thread(input, "\n> ")).strip()
    except EOFError:
        return None
