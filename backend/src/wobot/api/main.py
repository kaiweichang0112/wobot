import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import firebase_admin
import httpx2
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
from wobot.agent.models import openai_model
from wobot.api import accounts, avatars, conversations, turns, voices
from wobot.api.errors import (
    ApiError,
    handle_api_error,
    handle_database_unavailable,
    handle_invalid_request,
)
from wobot.assets.avatar import painter_of
from wobot.assets.dispatch import CloudRunJob, Dispatcher, GoogleToken, InProcess
from wobot.assets.eleven import ElevenLabs
from wobot.assets.media import media_store
from wobot.assets.worker import Worker, perform
from wobot.config import Settings, get_settings
from wobot.db import create_engine
from wobot.knowledge.embeddings import Embedder, OpenAIEmbedder
from wobot.logs import configure_logging
from wobot.speech.emotions import emotion_classifier

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
        app.state.title_model = None  # titles are then the question, shortened
        if settings.openai_api_key is not None:
            client = AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                timeout=settings.openai_timeout_seconds,
                max_retries=3,
            )
            stack.push_async_callback(client.close)
            embedder = OpenAIEmbedder(client, settings.embedding_model)
            app.state.graph = chat_graph(settings, engine, embedder, app.state.checkpointer)
            # A title is worth a short wait at most: the question, shortened, does too.
            app.state.title_model = openai_model(
                settings, settings.title_model, settings.title_effort, timeout=10, max_retries=1
            )
            await warm_up(embedder)
        # Voices and avatars: their media, ElevenLabs, and the job that makes them.
        app.state.media = media_store(settings)
        app.state.eleven = None
        if settings.elevenlabs_api_key is not None:
            client = await stack.enter_async_context(
                httpx2.AsyncClient(timeout=settings.elevenlabs_timeout_seconds)
            )
            app.state.eleven = ElevenLabs(client, settings.elevenlabs_api_key.get_secret_value())
        # The feeling of each sentence an Avatar-mode reply speaks; neutral without Jev.
        app.state.emotions = emotion_classifier(settings)
        # Paints avatars in this process when there is no job, as in development.
        app.state.painter = None
        if settings.openai_api_key is not None and settings.assets_job is None:
            app.state.painter = await stack.enter_async_context(painter_of(settings))
        run_client = await stack.enter_async_context(httpx2.AsyncClient(timeout=30))
        app.state.dispatcher = asset_dispatcher(settings, app, run_client)
        yield


def chat_graph(settings: Settings, engine, embedder: Embedder, checkpointer):
    """The graph every turn runs, built once. Without a model it cannot reach, such as Jev
    without its key, the API still starts; turns answer that chat is unavailable."""
    try:
        return build_graph(models_for(settings), engine, embedder, checkpointer)
    except ValueError:
        logger.exception("the chat graph could not be built")
        return None


def asset_dispatcher(settings: Settings, app: FastAPI, client: httpx2.AsyncClient) -> Dispatcher:
    """The `wobot-assets` job in the cloud. Without one, as in development, the API does
    the work itself, as its own role: a job's work does not survive the process."""
    if settings.assets_job is None:
        worker = Worker(
            app.state.sessionmaker,
            app.state.media,
            app.state.eleven,
            "in-process",
            painter=app.state.painter,
            concurrency=settings.avatar_concurrency,
        )

        async def work(command: str, kind: str, version_id: str) -> str:
            return await perform(worker, command, kind, uuid.UUID(version_id))

        return InProcess(work)
    return CloudRunJob(
        client,
        settings.google_cloud_project,
        settings.assets_job_region,
        settings.assets_job,
        GoogleToken(),
    )


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
app.include_router(voices.router)
app.include_router(avatars.router)


class HealthResponse(BaseModel):
    status: str
    version: str


@app.get("/health")
async def health() -> HealthResponse:
    # Liveness only: no database call, so a database outage doesn't restart instances.
    return HealthResponse(status="ok", version=get_settings().app_version)
