from typing import Any

from google.cloud.sql.connector import Connector, create_async_connector
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from wobot.config import Settings


async def create_engine(
    settings: Settings, *, pooled: bool = True
) -> tuple[AsyncEngine, Connector | None]:
    """Create the process-wide engine; the caller disposes it and closes the connector.

    Unpooled engines suit one-shot processes such as migrations.
    """
    pool_options: dict[str, Any] = (
        {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_timeout": settings.db_pool_timeout_seconds,
            "pool_pre_ping": True,
        }
        if pooled
        else {"poolclass": NullPool}
    )

    connector: Connector | None = None
    if settings.db_mode == "local":
        url = URL.create(
            "postgresql+asyncpg",
            username=settings.db_user,
            password=settings.db_password,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        )
        engine = create_async_engine(
            url, connect_args={"timeout": settings.db_connect_timeout_seconds}, **pool_options
        )
    else:
        # Lazy refresh: Cloud Run throttles CPU outside requests, which would starve
        # the connector's background certificate refresh.
        connector = await create_async_connector(refresh_strategy="lazy")

        async def connect():
            return await connector.connect_async(
                settings.instance_connection_name,
                "asyncpg",
                user=settings.db_user,
                db=settings.db_name,
                enable_iam_auth=True,
                timeout=settings.db_connect_timeout_seconds,
            )

        engine = create_async_engine("postgresql+asyncpg://", async_creator=connect, **pool_options)

    # No pgvector codec for asyncpg: the Vector column type already sends vectors as text,
    # which a binary codec would reject (tests/test_database.py).
    return engine, connector
