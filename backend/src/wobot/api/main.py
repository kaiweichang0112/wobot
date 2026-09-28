from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import firebase_admin
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from wobot.api.auth import Principal, current_principal
from wobot.api.errors import ApiError, handle_api_error
from wobot.config import get_settings
from wobot.db import create_engine
from wobot.models import Account


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    try:
        firebase_admin.get_app()
    except ValueError:
        # Token verification needs only the project ID, not service account keys.
        firebase_admin.initialize_app(options={"projectId": settings.google_cloud_project})
    engine, connector = await create_engine(settings)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()


app = FastAPI(title="Wobot API", lifespan=lifespan)
app.add_exception_handler(ApiError, handle_api_error)


class HealthResponse(BaseModel):
    status: str
    version: str


class MeResponse(BaseModel):
    account_id: str
    email: str


@app.get("/health")
async def health() -> HealthResponse:
    # Liveness only: no database call, so a database outage doesn't restart instances.
    return HealthResponse(status="ok", version=get_settings().app_version)


@app.get("/v1/me")
async def me(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> MeResponse:
    async with request.app.state.sessionmaker() as session, session.begin():
        await session.execute(
            insert(Account)
            .values(account_id=principal.account_id, email=principal.email)
            .on_conflict_do_update(
                index_elements=[Account.account_id],
                set_={"email": principal.email, "last_seen_at": func.now()},
            )
        )
    return MeResponse(account_id=principal.account_id, email=principal.email)
