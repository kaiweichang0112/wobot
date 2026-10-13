import asyncio
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.api import CHECKPOINT_TABLES, MEMBERS
from tests.database import API_USER, MIGRATOR_USER, run_as_migrator

# Set unconditionally so a shell that exports cloud settings can never point tests at
# Cloud SQL. DB_HOST and DB_PORT stay configurable, e.g. for a database on another port.
os.environ["GOOGLE_CLOUD_PROJECT"] = "wobot-test"
os.environ["DB_MODE"] = "local"
os.environ["DB_USER"] = API_USER
os.environ["DB_PASSWORD"] = "wobot"
# LangSmith sends traces off the machine; tests never do (DEC-057). TRACING_V2 is read
# before TRACING, so this wins over a LANGCHAIN_TRACING_V2=true left in the shell.
os.environ["LANGSMITH_TRACING_V2"] = "false"

from wobot.config import Settings  # noqa: E402

# Tests never read .env: a provider key there would let a test call a paid service,
# which CI, without one, never could. The app then starts without a chat graph, and a
# test that needs one builds it from fake models.
Settings.model_config["env_file"] = None
for key in ("OPENAI_API_KEY", "TYPESAFE_API_KEY"):  # nor from the shell
    os.environ.pop(key, None)

from wobot.agent.graph import Models, build_graph  # noqa: E402
from wobot.api.auth import get_token_claims  # noqa: E402
from wobot.api.main import app  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    env = {**os.environ, "DB_USER": MIGRATOR_USER}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND_DIR, env=env, check=True
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in() -> Callable[[str, str], None]:
    """Replace Firebase token verification with fixed claims."""

    def _sign_in(uid: str, email: str) -> None:
        claims = {"uid": uid, "email": email, "email_verified": True}
        app.dependency_overrides[get_token_claims] = lambda: claims

    return _sign_in


@pytest.fixture
def members() -> Iterator[dict[str, str]]:
    """Two allowlisted accounts; afterwards they are deleted with all they own, the graph's
    checkpoints of their conversations included."""

    async def setup() -> None:
        for email in MEMBERS.values():
            await run_as_migrator(
                "INSERT INTO app.allowed_emails (email) VALUES ($1) ON CONFLICT DO NOTHING", email
            )

    async def cleanup() -> None:
        rows = await run_as_migrator(
            "SELECT conversation_id::text AS id FROM app.conversations WHERE owner_id = ANY($1)",
            list(MEMBERS),
        )
        threads = [row["id"] for row in rows]
        for table in CHECKPOINT_TABLES:
            await run_as_migrator(f"DELETE FROM app.{table} WHERE thread_id = ANY($1)", threads)
        await run_as_migrator("DELETE FROM app.accounts WHERE account_id = ANY($1)", list(MEMBERS))
        await run_as_migrator(
            "DELETE FROM app.allowed_emails WHERE email = ANY($1)", list(MEMBERS.values())
        )

    asyncio.run(setup())
    yield MEMBERS
    asyncio.run(cleanup())


@pytest.fixture
def as_member(sign_in) -> Callable[[str], None]:
    """Signs in as one of the members, by UID."""

    def _as(uid: str) -> None:
        sign_in(uid, MEMBERS[uid])

    return _as


@pytest.fixture
def published_version() -> Iterator[int]:
    """A published knowledge version for a turn to run on. The developer's database has
    one; CI's empty one gets an empty stand-in, published for the test and then removed,
    so the pointer is left as it was."""

    async def ensure() -> tuple[int, object]:
        [active] = await run_as_migrator("SELECT index_version_id FROM knowledge.active_knowledge")
        if active["index_version_id"] is not None:
            return active["index_version_id"], None
        [run] = await run_as_migrator(
            "INSERT INTO ops.ingestion_runs (triggered_by, policy, status, code_version) "
            "VALUES ('manual', 'dry_run', 'running', 'test') RETURNING run_id"
        )
        [version] = await run_as_migrator(
            "INSERT INTO knowledge.index_versions (status, embedding_config_id, strategies, "
            "run_id) VALUES ('published', 'openai/text-embedding-3-small/1536/cosine', '{}', $1) "
            "RETURNING index_version_id",
            run["run_id"],
        )
        await run_as_migrator(
            "UPDATE knowledge.active_knowledge SET index_version_id = $1",
            version["index_version_id"],
        )
        return version["index_version_id"], run["run_id"]

    async def remove(version_id: int, run_id: object) -> None:
        await run_as_migrator("UPDATE knowledge.active_knowledge SET index_version_id = NULL")
        await run_as_migrator(
            "DELETE FROM knowledge.index_versions WHERE index_version_id = $1", version_id
        )
        await run_as_migrator("DELETE FROM ops.ingestion_runs WHERE run_id = $1", run_id)

    version_id, run_id = asyncio.run(ensure())
    yield version_id
    if run_id is not None:
        asyncio.run(remove(version_id, run_id))


@pytest.fixture
def chat_graph(client):
    """Gives the app a graph with the models given, as the lifespan gives it real ones."""

    def _with(models: Models) -> Models:
        state = app.state
        state.graph = build_graph(models, state.engine, None, state.checkpointer)
        return models

    yield _with
    app.state.graph = None
