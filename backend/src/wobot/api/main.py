from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import firebase_admin
import psycopg
import psycopg_pool
import sqlalchemy.exc
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from wobot.agent.checkpoints import open_checkpointer
from wobot.api import accounts, conversations
from wobot.api.errors import (
    ApiError,
    handle_api_error,
    handle_database_unavailable,
    handle_invalid_request,
)
from wobot.config import get_settings
from wobot.db import create_engine

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
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        # The graph's memory of each conversation, on its own psycopg pool: in the cloud
        # through the service's Cloud SQL connection (DEC-078).
        app.state.checkpointer = await stack.enter_async_context(open_checkpointer(settings))
        yield


app = FastAPI(title="Wobot API", lifespan=lifespan)
app.add_exception_handler(ApiError, handle_api_error)
app.add_exception_handler(RequestValidationError, handle_invalid_request)
for error in DATABASE_UNAVAILABLE:
    app.add_exception_handler(error, handle_database_unavailable)
app.include_router(accounts.router)
app.include_router(conversations.router)


class HealthResponse(BaseModel):
    status: str
    version: str


@app.get("/health")
async def health() -> HealthResponse:
    # Liveness only: no database call, so a database outage doesn't restart instances.
    return HealthResponse(status="ok", version=get_settings().app_version)
