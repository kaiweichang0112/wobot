import asyncio
from collections.abc import Iterator

import pytest

from tests.database import run_as_migrator

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def allowlisted() -> Iterator[str]:
    email = "member@example.com"
    # Idempotent, so a run interrupted before its cleanup doesn't break the next one.
    asyncio.run(
        run_as_migrator(
            "INSERT INTO app.allowed_emails (email) VALUES ($1) ON CONFLICT DO NOTHING", email
        )
    )
    yield email
    asyncio.run(run_as_migrator("DELETE FROM app.allowed_emails WHERE email = $1", email))
    asyncio.run(run_as_migrator("DELETE FROM app.accounts WHERE email = $1", email))


def test_health_reports_version(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "dev"}


def test_me_requires_a_token(client):
    response = client.get("/v1/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_me_rejects_an_email_outside_the_allowlist(client, sign_in):
    sign_in("uid-stranger", "stranger@example.com")

    response = client.get("/v1/me", headers=AUTH)

    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "ACCOUNT_NOT_ALLOWED", "message": "This account is not authorized."}
    }


def test_me_registers_an_allowlisted_account(client, sign_in, allowlisted):
    sign_in("uid-member", "Member@Example.com")

    response = client.get("/v1/me", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"account_id": "uid-member", "email": allowlisted}
    # The response alone would not reveal a write that was never committed.
    rows = asyncio.run(
        run_as_migrator("SELECT email FROM app.accounts WHERE account_id = $1", "uid-member")
    )
    assert [row["email"] for row in rows] == [allowlisted]


def test_me_rejects_an_account_removed_from_the_allowlist(client, sign_in, allowlisted):
    sign_in("uid-member", allowlisted)
    assert client.get("/v1/me", headers=AUTH).status_code == 200

    asyncio.run(run_as_migrator("DELETE FROM app.allowed_emails WHERE email = $1", allowlisted))

    assert client.get("/v1/me", headers=AUTH).status_code == 403
