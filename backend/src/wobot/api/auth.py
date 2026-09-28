from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from firebase_admin import auth as firebase_auth
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from wobot.api.errors import ApiError
from wobot.models import AllowedEmail


@dataclass(frozen=True)
class Principal:
    account_id: str  # Firebase UID
    email: str


def not_allowed() -> ApiError:
    # A new instance per raise: re-raising a shared one grows its traceback on every request.
    return ApiError(403, "ACCOUNT_NOT_ALLOWED", "This account is not authorized.")


async def get_token_claims(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Verify the Firebase ID token in `Authorization: Bearer <token>`."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "UNAUTHENTICATED", "Sign in to continue.")
    try:
        # Blocking: may fetch Google's public signing keys.
        return await run_in_threadpool(firebase_auth.verify_id_token, token)
    except (ValueError, firebase_auth.InvalidIdTokenError) as exc:
        raise ApiError(401, "UNAUTHENTICATED", "Sign in again to continue.") from exc
    except firebase_auth.CertificateFetchError as exc:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", "Sign-in is unavailable. Try again.") from exc


async def current_principal(
    request: Request, claims: Annotated[dict[str, Any], Depends(get_token_claims)]
) -> Principal:
    """The signed-in account, if its verified email is on the allowlist."""
    email = str(claims.get("email", "")).lower()
    if not email or not claims.get("email_verified"):
        raise not_allowed()
    # Checked on every request, so removing an email takes effect immediately.
    async with request.app.state.sessionmaker() as session:
        allowed = await session.scalar(
            select(AllowedEmail.email).where(AllowedEmail.email == email)
        )
    if allowed is None:
        raise not_allowed()
    return Principal(account_id=claims["uid"], email=email)
