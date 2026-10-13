import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import firebase_admin
import psycopg
import psycopg_pool
import sqlalchemy.exc
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from wobot.agent.checkpoints import open_checkpointer
from wobot.agent.graph import build_graph, models_for
from wobot.api import accounts, conversations, turns
from wobot.api.errors import (
    ApiError,
    handle_api_error,
    handle_database_unavailable,
    handle_invalid_request,
)
from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.knowledge.embeddings import Embedder, OpenAIEmbedder
from wobot.logs import configure_logging

logger = logging.getLogger(__name__)

# Raised when the database cannot be reached or a connection cannot be had in time.
DATABASE_UNAVAILABLE = (
    sqlalchemy.exc.OperationalError,
    sqlalchemy.exc.InterfaceError,
    sqlalchemy.exc.TimeoutError,
    psycopg.OperationalError,
    psycopg_pool.PoolTimeout,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One JSON line per entry on Cloud Run, so each turn's timings are fields to query.
    configure_logging()
    settings = get_settings()
    try:
        firebase_admin.get_app()
    except ValueError:
        # Token verification needs only the project ID, not service account keys.
        firebase_admin.initialize_app(options={"projectId": settings.google_cloud_project})
    async with AsyncExitStack() as stack:
        engine, connector = await create_engine(settings)
        stack.push_async_callback(engine.dispose)
        if connector is not None:
            stack.push_async_callback(connector.close_async)
        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        # The graph's memory of each conversation, on its own psycopg pool: in the cloud
        # through the service's Cloud SQL connection (DEC-078).
        app.state.checkpointer = await stack.enter_async_context(open_checkpointer(settings))
        app.state.graph = None
        if settings.openai_api_key is not None:
            client = AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                timeout=settings.openai_timeout_seconds,
                max_retries=3,
            )
            stack.push_async_callback(client.close)
            embedder = OpenAIEmbedder(client, settings.embedding_model)
            app.state.graph = chat_graph(settings, engine, embedder, app.state.checkpointer)
            await warm_up(embedder)
        yield


def chat_graph(settings: Settings, engine, embedder: Embedder, checkpointer):
    """The graph every turn runs, built once. Without a model it cannot reach, such as Jev
    without its key, the API still starts; turns answer that chat is unavailable."""
    try:
        return build_graph(models_for(settings), engine, embedder, checkpointer)
    except ValueError:
        logger.exception("the chat graph could not be built")
        return None


async def warm_up(embedder: Embedder) -> None:
    """Opens the connection to OpenAI before the first turn needs it: the first embedding
    in a process took 2.5 s, most of it setting the connection up (DEC-071). At most a
    few seconds of startup, and a failure only means the first turn pays instead."""
    started = time.perf_counter()
    try:
        await asyncio.wait_for(embedder.embed(["warm-up"]), timeout=10)
    except Exception:
        logger.warning("warming up the OpenAI connection failed", exc_info=True)
        return
    logger.info(
        "warmed up the OpenAI connection",
        extra={"seconds": round(time.perf_counter() - started, 3)},
    )


app = FastAPI(title="Wobot API", lifespan=lifespan)
app.add_exception_handler(ApiError, handle_api_error)
app.add_exception_handler(RequestValidationError, handle_invalid_request)
for error in DATABASE_UNAVAILABLE:
    app.add_exception_handler(error, handle_database_unavailable)
app.include_router(accounts.router)
app.include_router(conversations.router)
app.include_router(turns.router)
app.include_router(turns.control)


class HealthResponse(BaseModel):
    status: str
    version: str


@app.get("/health")
async def health() -> HealthResponse:
    # Liveness only: no database call, so a database outage doesn't restart instances.
    return HealthResponse(status="ok", version=get_settings().app_version)
