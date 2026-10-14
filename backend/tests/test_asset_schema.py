"""The asset tables hold their rules themselves (migration 0008, DEC-086): the API and the
asset worker write them from different instances and jobs, and only the database sees
every write."""

import asyncio
import uuid
from collections.abc import Iterator

import asyncpg
import pytest

from tests.database import API_USER, ASSETS_USER, INGEST_USER, rolled_back, run_as_migrator
from wobot.assets.frames import FRAME_KEYS

ADD_ACCOUNT = "INSERT INTO app.accounts (account_id, email) VALUES ($1, $2)"
ADD_VOICE = (
    "INSERT INTO app.voice_versions (voice_id, owner_id, status, sample_seconds) "
    "VALUES ($1, $2, 'queued', 90)"
)
ADD_AVATAR = (
    "INSERT INTO app.avatar_versions (avatar_id, owner_id, status, expression_schema_version, "
    "prompt_version, prompt_hash, image_model, quality) "
    "VALUES ($1, $2, 'queued', 1, 1, 'hash', 'gpt-image-2.5-sunburst', 'medium')"
)
ADD_FRAMES = "INSERT INTO app.avatar_frames (avatar_id, frame_key) SELECT $1, unnest($2::text[])"
# The worker's claim, and the end of its work.
CLAIM = (
    "UPDATE app.voice_versions SET status = 'running', started_at = now(), execution = 'e-1' "
    "WHERE voice_id = $1 AND status = 'queued'"
)
CLONED = (
    "UPDATE app.voice_versions SET status = 'ready', provider_voice_id = $2, "
    "finished_at = now() WHERE voice_id = $1"
)
SET_STATUS = "UPDATE app.voice_versions SET status = $2 WHERE voice_id = $1"
SHA256 = "0" * 64


async def _account(connection: asyncpg.Connection, account_id: str = "uid-1") -> str:
    await connection.execute(ADD_ACCOUNT, account_id, f"{account_id}@example.com")
    return account_id


async def _ready_voice(connection: asyncpg.Connection, owner: str) -> uuid.UUID:
    voice = uuid.uuid4()
    await connection.execute(ADD_VOICE, voice, owner)
    await connection.execute(CLAIM, voice)
    await connection.execute(CLONED, voice, f"provider-{voice}")
    return voice


async def test_api_role_makes_and_activates_a_voice():
    async with rolled_back(API_USER) as connection:
        owner = await _account(connection)
        voice = await _ready_voice(connection, owner)
        await connection.execute(SET_STATUS, voice, "active")
        status = await connection.fetchval(
            "SELECT status FROM app.voice_versions WHERE voice_id = $1", voice
        )
    assert status == "active"


async def test_a_claim_is_taken_once():
    async with rolled_back(API_USER) as connection:
        voice = uuid.uuid4()
        await connection.execute(ADD_VOICE, voice, await _account(connection))
        first = await connection.execute(CLAIM, voice)
        second = await connection.execute(CLAIM, voice)
    assert (first, second) == ("UPDATE 1", "UPDATE 0")


async def test_an_account_has_one_active_voice_and_activation_retires_the_old_first():
    async with rolled_back(API_USER) as connection:
        owner = await _account(connection)
        old = await _ready_voice(connection, owner)
        await connection.execute(SET_STATUS, old, "active")
        new = await _ready_voice(connection, owner)
        # Activating the new one first would leave two active for a moment: refused.
        with pytest.raises(asyncpg.UniqueViolationError, match="voice_versions_one_active"):
            async with connection.transaction():  # a savepoint: the test goes on after it
                await connection.execute(SET_STATUS, new, "active")
        await connection.execute(SET_STATUS, old, "retired")
        await connection.execute(SET_STATUS, new, "active")


async def test_an_account_makes_one_voice_at_a_time():
    async with rolled_back(API_USER) as connection:
        owner = await _account(connection)
        first = uuid.uuid4()
        await connection.execute(ADD_VOICE, first, owner)
        with pytest.raises(asyncpg.UniqueViolationError, match="voice_versions_one_making"):
            async with connection.transaction():
                await connection.execute(ADD_VOICE, uuid.uuid4(), owner)
        # A running one counts too; once it has ended, the next may start.
        await connection.execute(CLAIM, first)
        with pytest.raises(asyncpg.UniqueViolationError, match="voice_versions_one_making"):
            async with connection.transaction():
                await connection.execute(ADD_VOICE, uuid.uuid4(), owner)
        await connection.execute(
            "UPDATE app.voice_versions SET status = 'failed', finished_at = now() "
            "WHERE voice_id = $1",
            first,
        )
        await connection.execute(ADD_VOICE, uuid.uuid4(), owner)


async def test_two_accounts_each_have_their_own_active_voice():
    async with rolled_back(API_USER) as connection:
        for owner in ("uid-1", "uid-2"):
            voice = await _ready_voice(connection, await _account(connection, owner))
            await connection.execute(SET_STATUS, voice, "active")


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (
            "UPDATE app.voice_versions SET status = 'done', finished_at = now()",
            "voice_versions_status",
        ),
        ("UPDATE app.voice_versions SET status = 'running'", "voice_versions_started_when_running"),
        (
            "UPDATE app.voice_versions SET finished_at = now()",
            "voice_versions_finished_unless_making",
        ),
        (
            "UPDATE app.voice_versions SET status = 'failed'",
            "voice_versions_finished_unless_making",
        ),
        (
            "UPDATE app.voice_versions SET status = 'ready', finished_at = now()",
            "voice_versions_cloned_when_usable",
        ),
        (
            "UPDATE app.voice_versions SET status = 'ready', finished_at = now(), "
            "provider_voice_id = 'p', cleaned_at = now()",
            "voice_versions_cleaned_when_unused",
        ),
        (
            "UPDATE app.voice_versions SET sample_seconds = 0",
            "voice_versions_sample_seconds_positive",
        ),
    ],
)
async def test_a_voice_version_cannot_be_left_inconsistent(statement, constraint):
    async with rolled_back(API_USER) as connection:
        await connection.execute(ADD_VOICE, uuid.uuid4(), await _account(connection))
        with pytest.raises(asyncpg.CheckViolationError, match=constraint):
            await connection.execute(statement)


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        ("UPDATE app.avatar_versions SET quality = 'auto'", "avatar_versions_quality"),
        (
            "UPDATE app.avatar_versions SET expression_schema_version = 0",
            "avatar_versions_expression_schema_positive",
        ),
        (
            "UPDATE app.avatar_versions SET status = 'active'",
            "avatar_versions_finished_unless_making",
        ),
    ],
)
async def test_an_avatar_version_cannot_be_left_inconsistent(statement, constraint):
    async with rolled_back(API_USER) as connection:
        await connection.execute(ADD_AVATAR, uuid.uuid4(), await _account(connection))
        with pytest.raises(asyncpg.CheckViolationError, match=constraint):
            await connection.execute(statement)


async def test_an_avatar_has_exactly_the_21_canonical_frames():
    async with rolled_back(API_USER) as connection:
        avatar = uuid.uuid4()
        await connection.execute(ADD_AVATAR, avatar, await _account(connection))
        await connection.execute(ADD_FRAMES, avatar, list(FRAME_KEYS))
        with pytest.raises(asyncpg.UniqueViolationError):  # no key twice
            async with connection.transaction():
                await connection.execute(ADD_FRAMES, avatar, ["joy_half"])
        # The reference app's five-expression keys are not this schema's (AC-AVA-04).
        for key in ("happy_closed", "surprised_wide", "shy_half", "joy_open"):
            with pytest.raises(asyncpg.CheckViolationError, match="avatar_frames_frame_key"):
                async with connection.transaction():
                    await connection.execute(ADD_FRAMES, avatar, [key])
        count = await connection.fetchval(
            "SELECT count(*) FROM app.avatar_frames WHERE avatar_id = $1", avatar
        )
    assert count == len(FRAME_KEYS) == 21


@pytest.mark.parametrize(
    ("assignment", "constraint"),
    [
        ("status = 'ready'", "avatar_frames_stored_when_ready"),
        ("status = 'done'", "avatar_frames_status"),
        ("sha256 = 'not-a-hash'", "avatar_frames_sha256_hex"),
        ("attempts = -1", "avatar_frames_attempts_not_negative"),
        ("input_tokens = -1, output_tokens = 1", "avatar_frames_tokens_not_negative"),
    ],
)
async def test_a_frame_cannot_be_left_inconsistent(assignment, constraint):
    async with rolled_back(API_USER) as connection:
        avatar = uuid.uuid4()
        await connection.execute(ADD_AVATAR, avatar, await _account(connection))
        await connection.execute(ADD_FRAMES, avatar, ["neutral_closed"])
        with pytest.raises(asyncpg.CheckViolationError, match=constraint):
            await connection.execute(f"UPDATE app.avatar_frames SET {assignment}")


async def test_deleting_an_avatar_version_deletes_its_frames():
    async with rolled_back(API_USER) as connection:
        avatar = uuid.uuid4()
        await connection.execute(ADD_AVATAR, avatar, await _account(connection))
        await connection.execute(ADD_FRAMES, avatar, list(FRAME_KEYS))
        await connection.execute("DELETE FROM app.avatar_versions WHERE avatar_id = $1", avatar)
        left = await connection.fetchval(
            "SELECT count(*) FROM app.avatar_frames WHERE avatar_id = $1", avatar
        )
    assert left == 0


# --- The asset worker's role -------------------------------------------------------------

WORKER_ACCOUNT = "uid-asset-worker"


@pytest.fixture
def queued_assets() -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """A queued voice and avatar, committed: the worker's role cannot add rows itself.
    Deleting the account afterwards deletes them."""
    voice, avatar = uuid.uuid4(), uuid.uuid4()

    async def setup() -> None:
        await run_as_migrator(ADD_ACCOUNT, WORKER_ACCOUNT, "worker@example.com")
        await run_as_migrator(ADD_VOICE, voice, WORKER_ACCOUNT)
        await run_as_migrator(ADD_AVATAR, avatar, WORKER_ACCOUNT)
        await run_as_migrator(ADD_FRAMES, avatar, list(FRAME_KEYS))

    asyncio.run(setup())
    yield voice, avatar
    asyncio.run(run_as_migrator("DELETE FROM app.accounts WHERE account_id = $1", WORKER_ACCOUNT))


async def test_the_worker_reads_and_moves_the_versions_it_makes(queued_assets):
    voice, avatar = queued_assets
    async with rolled_back(ASSETS_USER) as connection:
        assert await connection.execute(CLAIM, voice) == "UPDATE 1"
        await connection.execute(CLONED, voice, "provider-voice")
        await connection.execute(
            "UPDATE app.avatar_frames SET status = 'ready', storage_key = 'k', sha256 = $2, "
            "attempts = 1 WHERE avatar_id = $1 AND frame_key = 'neutral_closed'",
            avatar,
            SHA256,
        )
        ready = await connection.fetchval(
            "SELECT count(*) FROM app.avatar_frames WHERE avatar_id = $1 AND status = 'ready'",
            avatar,
        )
    assert ready == 1


@pytest.mark.parametrize(
    "statement",
    [
        ADD_VOICE.replace("$1, $2", "gen_random_uuid(), 'uid-asset-worker'"),
        "DELETE FROM app.voice_versions",
        "DELETE FROM app.avatar_frames",
        "CREATE TABLE app.worker_notes (note text)",
        "SELECT * FROM app.turns",
        "SELECT * FROM app.conversations",
        "SELECT * FROM app.accounts",
        "SELECT * FROM knowledge.records",
    ],
)
async def test_the_worker_cannot_add_remove_or_read_anything_else(queued_assets, statement):
    async with rolled_back(ASSETS_USER) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement)


async def test_ingestion_cannot_read_assets():
    async with rolled_back(INGEST_USER) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("SELECT * FROM app.voice_versions")
