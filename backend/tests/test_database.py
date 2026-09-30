import asyncpg
import pytest
from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, cast, select

from tests.database import API_USER, connect
from wobot.config import get_settings
from wobot.db import create_engine


@pytest.mark.parametrize("schema", ["public", "app", "knowledge", "ops"])
async def test_api_role_cannot_create_tables(schema):
    connection = await connect(API_USER)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(f"CREATE TABLE {schema}.not_allowed (x int)")
    finally:
        await connection.close()


async def test_api_role_cannot_edit_the_allowlist():
    connection = await connect(API_USER)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "INSERT INTO app.allowed_emails (email) VALUES ('x@example.com')"
            )
    finally:
        await connection.close()


async def test_vectors_round_trip_through_the_engine():
    engine, _ = await create_engine(get_settings(), pooled=False)
    embedding = bindparam("embedding", [1.0, 2.0, 3.0], type_=Vector(3))
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(select(cast(embedding, Vector(3))))
    finally:
        await engine.dispose()

    assert value == [1.0, 2.0, 3.0]
