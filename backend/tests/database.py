import asyncpg

from wobot.config import get_settings

# Login users created by infra/sql/local-init.sh.
API_USER = "wobot_api_user"
MIGRATOR_USER = "wobot_migrator_user"


async def connect(user: str) -> asyncpg.Connection:
    """Connect to the local test database as one of its login users."""
    settings = get_settings()
    return await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=user,
        password=settings.db_password,
        database=settings.db_name,
    )


async def run_as_migrator(sql: str, *args: object) -> list[asyncpg.Record]:
    """Run one statement as the schema owner, the way a maintainer edits data."""
    connection = await connect(MIGRATOR_USER)
    try:
        await connection.execute("SET ROLE wobot_migrator")
        return await connection.fetch(sql, *args)
    finally:
        await connection.close()
