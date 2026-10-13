"""The conversation tables hold their rules themselves (migration 0007, DEC-079): the API
writes them as its own role, and a broken row is refused by the database, whatever the
code that tried."""

import uuid

import asyncpg
import pytest

from tests.database import API_USER, INGEST_USER, connect, rolled_back

ADD_ACCOUNT = "INSERT INTO app.accounts (account_id, email) VALUES ('uid-1', 'a@example.com')"
ADD_CONVERSATION = (
    "INSERT INTO app.conversations (owner_id) VALUES ('uid-1') RETURNING conversation_id"
)
ADD_TURN = (
    "INSERT INTO app.turns (turn_id, conversation_id, seq, user_text, status, app_version) "
    "VALUES ($1, $2, $3, 'hi', 'running', 'test')"
)
FINISH = "UPDATE app.turns SET status = $2, finished_at = now() WHERE turn_id = $1"


async def _conversation(connection: asyncpg.Connection) -> uuid.UUID:
    await connection.execute(ADD_ACCOUNT)
    return await connection.fetchval(ADD_CONVERSATION)


async def test_api_role_keeps_conversations_and_their_turns():
    async with rolled_back(API_USER) as connection:
        conversation = await _conversation(connection)
        turn = uuid.uuid4()
        await connection.execute(ADD_TURN, turn, conversation, 1)
        await connection.execute(FINISH, turn, "completed")
        await connection.execute(
            "UPDATE app.conversations SET head_checkpoint_id = 'c-1' WHERE conversation_id = $1",
            conversation,
        )
        status = await connection.fetchval("SELECT status FROM app.turns WHERE turn_id = $1", turn)
    assert status == "completed"


async def test_a_conversation_generates_one_turn_at_a_time():
    async with rolled_back(API_USER) as connection:
        conversation = await _conversation(connection)
        first = uuid.uuid4()
        await connection.execute(ADD_TURN, first, conversation, 1)
        with pytest.raises(asyncpg.UniqueViolationError, match="turns_one_running"):
            async with connection.transaction():  # a savepoint: the test goes on after it
                await connection.execute(ADD_TURN, uuid.uuid4(), conversation, 2)
        # Once the first has ended, the next may run.
        await connection.execute(FINISH, first, "cancelled")
        await connection.execute(ADD_TURN, uuid.uuid4(), conversation, 2)


async def test_two_conversations_run_at_once():
    async with rolled_back(API_USER) as connection:
        conversation = await _conversation(connection)
        other = await connection.fetchval(ADD_CONVERSATION)
        await connection.execute(ADD_TURN, uuid.uuid4(), conversation, 1)
        await connection.execute(ADD_TURN, uuid.uuid4(), other, 1)


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        ("UPDATE app.turns SET status = 'done', finished_at = now()", "turns_status"),
        ("UPDATE app.turns SET status = 'completed'", "turns_finished_unless_running"),
        ("UPDATE app.turns SET finished_at = now()", "turns_finished_unless_running"),
        ("UPDATE app.turns SET attempt = 0", "turns_attempt_positive"),
        ("UPDATE app.turns SET user_text = ''", "turns_user_text_length"),
        ("UPDATE app.turns SET user_text = repeat('x', 2001)", "turns_user_text_length"),
    ],
)
async def test_a_turn_cannot_be_left_inconsistent(statement, constraint):
    async with rolled_back(API_USER) as connection:
        conversation = await _conversation(connection)
        await connection.execute(ADD_TURN, uuid.uuid4(), conversation, 1)
        with pytest.raises(asyncpg.CheckViolationError, match=constraint):
            await connection.execute(statement)


async def test_turn_order_is_unique_within_a_conversation():
    async with rolled_back(API_USER) as connection:
        conversation = await _conversation(connection)
        first = uuid.uuid4()
        await connection.execute(ADD_TURN, first, conversation, 1)
        await connection.execute(FINISH, first, "completed")
        with pytest.raises(asyncpg.UniqueViolationError, match="turns_conversation_seq"):
            await connection.execute(ADD_TURN, uuid.uuid4(), conversation, 1)


async def test_deleting_a_conversation_deletes_its_turns():
    async with rolled_back(API_USER) as connection:
        conversation = await _conversation(connection)
        await connection.execute(ADD_TURN, uuid.uuid4(), conversation, 1)
        await connection.execute(
            "DELETE FROM app.conversations WHERE conversation_id = $1", conversation
        )
        left = await connection.fetchval(
            "SELECT count(*) FROM app.turns WHERE conversation_id = $1", conversation
        )
    assert left == 0


@pytest.mark.parametrize(("name", "allowed"), [("", False), ("x" * 41, False), ("小幫手", True)])
async def test_the_assistant_s_name_has_a_length(name, allowed):
    async with rolled_back(API_USER) as connection:
        await connection.execute(ADD_ACCOUNT)
        insert = connection.execute(
            "INSERT INTO app.account_settings (account_id, chatbot_name, version) "
            "VALUES ('uid-1', $1, 1)",
            name,
        )
        if allowed:
            await insert
        else:
            with pytest.raises(asyncpg.CheckViolationError):
                await insert


@pytest.mark.parametrize(
    "statement",
    ["SELECT * FROM app.conversations", "SELECT * FROM app.turns"],
)
async def test_ingestion_cannot_read_conversations(statement):
    connection = await connect(INGEST_USER)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement)
    finally:
        await connection.close()
