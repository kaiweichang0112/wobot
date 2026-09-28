import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.database import API_USER, MIGRATOR_USER

# Set unconditionally so a shell that exports cloud settings can never point tests at
# Cloud SQL. DB_HOST and DB_PORT stay configurable, e.g. for a database on another port.
os.environ["GOOGLE_CLOUD_PROJECT"] = "wobot-test"
os.environ["DB_MODE"] = "local"
os.environ["DB_USER"] = API_USER
os.environ["DB_PASSWORD"] = "wobot"

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
