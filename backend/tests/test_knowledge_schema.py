import uuid

import asyncpg
import pytest

from tests.database import API_USER, INGEST_USER, MIGRATOR_USER, connect, rolled_back

EMBEDDING_CONFIG = "openai/text-embedding-3-small/1536/cosine"
INSERT_SOURCE = "INSERT INTO knowledge.sources (source_id, kind) VALUES ('test', 'xlsx')"
ADD_MEMBER = (
    "INSERT INTO knowledge.index_version_records "
    "(index_version_id, record_id, logical_key, snapshot_id, locator) "
    "VALUES ($1, $2, $3, $4, '{}')"
)


async def _new_version(connection: asyncpg.Connection) -> tuple[int, uuid.UUID]:
    """A building version and a snapshot to cite, the minimum a membership row needs."""
    run_id = await connection.fetchval(
        "INSERT INTO ops.ingestion_runs (triggered_by, policy, status, code_version) "
        "VALUES ('manual', 'dry_run', 'running', 'test') RETURNING run_id"
    )
    version_id = await connection.fetchval(
        "INSERT INTO knowledge.index_versions (status, embedding_config_id, strategies, run_id) "
        "VALUES ('building', $1, '{}', $2) RETURNING index_version_id",
        EMBEDDING_CONFIG,
        run_id,
    )
    await connection.execute(INSERT_SOURCE)
    snapshot_id = await connection.fetchval(
        "INSERT INTO knowledge.source_snapshots "
        "(source_id, locator, content_sha256, storage_key, byte_size) "
        "VALUES ('test', 'test', 'test', 'test', 0) RETURNING snapshot_id"
    )
    return version_id, snapshot_id


async def _new_record(
    connection: asyncpg.Connection, logical_key: str, content_hash: str
) -> uuid.UUID:
    return await connection.fetchval(
        "INSERT INTO knowledge.records (record_type, logical_key, content_hash, raw) "
        "VALUES ('product', $1, $2, '{}') RETURNING record_id",
        logical_key,
        content_hash,
    )


async def test_ingest_role_appends_to_knowledge():
    async with rolled_back(INGEST_USER) as connection:
        await connection.execute(INSERT_SOURCE)
        count = await connection.fetchval(
            "SELECT count(*) FROM knowledge.sources WHERE source_id = 'test'"
        )
    assert count == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE knowledge.sources SET kind = 'pdf' WHERE source_id = 'test'",
        "DELETE FROM knowledge.sources WHERE source_id = 'test'",
    ],
)
async def test_ingest_role_cannot_rewrite_stored_content(statement):
    async with rolled_back(INGEST_USER) as connection:
        await connection.execute(INSERT_SOURCE)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement)


async def test_ingest_role_moves_the_active_pointer():
    async with rolled_back(INGEST_USER) as connection:
        status = await connection.execute(
            "UPDATE knowledge.active_knowledge SET revision = revision + 1"
        )
    assert status == "UPDATE 1"


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO knowledge.active_knowledge (singleton) VALUES (true)",
        "DELETE FROM knowledge.active_knowledge",
        "INSERT INTO knowledge.embedding_configs VALUES ('x', 'x', 'x', 1536, 'cosine')",
    ],
)
async def test_only_migrations_create_pointers_and_embedding_spaces(statement):
    async with rolled_back(INGEST_USER) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM app.accounts",
        "CREATE TABLE knowledge.not_allowed (x int)",
        "CREATE TABLE ops.not_allowed (x int)",
    ],
)
async def test_ingest_role_stays_out_of_private_data_and_ddl(statement):
    connection = await connect(INGEST_USER)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement)
    finally:
        await connection.close()


@pytest.mark.parametrize(
    "statement",
    [
        INSERT_SOURCE,
        "UPDATE knowledge.active_knowledge SET revision = revision + 1",
        "SELECT * FROM ops.ingestion_runs",
    ],
)
async def test_api_role_only_reads_knowledge(statement):
    async with rolled_back(API_USER) as connection:
        assert await connection.fetchval("SELECT revision FROM knowledge.active_knowledge") >= 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement)


@pytest.mark.parametrize(
    ("singleton", "error"),
    [("true", asyncpg.UniqueViolationError), ("false", asyncpg.CheckViolationError)],
)
async def test_active_knowledge_holds_a_single_row(singleton, error):
    # As the schema owner: the constraint, not a privilege, stops a second row.
    async with rolled_back(MIGRATOR_USER) as connection:
        with pytest.raises(error):
            await connection.execute(
                f"INSERT INTO knowledge.active_knowledge (singleton) VALUES ({singleton})"
            )


async def test_embeddings_reject_other_dimensions():
    async with rolled_back(INGEST_USER) as connection:
        chunk_id = await connection.fetchval(
            "INSERT INTO knowledge.chunks "
            "(content_hash, strategy, strategy_version, context_header, body, "
            "embedding_input, token_count) "
            "VALUES ('test', 'test', 1, '', 'x', 'x', 1) RETURNING chunk_id"
        )
        with pytest.raises(asyncpg.DataError, match="expected 1536 dimensions"):
            await connection.execute(
                "INSERT INTO knowledge.embeddings (chunk_id, embedding_config_id, embedding) "
                "VALUES ($1, $2, '[1,2,3]')",
                chunk_id,
                EMBEDDING_CONFIG,
            )


async def test_a_version_holds_one_revision_per_item():
    async with rolled_back(INGEST_USER) as connection:
        version_id, snapshot_id = await _new_version(connection)
        first = await _new_record(connection, "product:test", "a")
        second = await _new_record(connection, "product:test", "b")
        await connection.execute(ADD_MEMBER, version_id, first, "product:test", snapshot_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(ADD_MEMBER, version_id, second, "product:test", snapshot_id)


async def test_membership_keeps_the_record_logical_key():
    async with rolled_back(INGEST_USER) as connection:
        version_id, snapshot_id = await _new_version(connection)
        record_id = await _new_record(connection, "product:test", "a")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await connection.execute(
                ADD_MEMBER, version_id, record_id, "product:other", snapshot_id
            )
