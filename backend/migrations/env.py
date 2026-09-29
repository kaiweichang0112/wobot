import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import wobot.knowledge.models  # noqa: F401
from wobot.config import get_settings
from wobot.db import create_engine
from wobot.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        # The migrator cannot create tables in `public`.
        version_table_schema="ops",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine, connector = await create_engine(get_settings(), pooled=False)
    try:
        async with engine.connect() as connection:
            # New objects must belong to the group role, not to whoever logged in
            # (infra/README.md, section 8). Commit so the role lasts for the session and
            # Alembic starts its own transaction; given an open one, it never commits.
            await connection.exec_driver_sql("SET ROLE wobot_migrator")
            await connection.commit()
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()
        if connector is not None:
            await connector.close_async()


if context.is_offline_mode():
    raise SystemExit("Offline mode is not supported; run migrations against a database.")
asyncio.run(run_migrations_online())
