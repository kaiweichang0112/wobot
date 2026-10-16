"""Deleting an account (IAM-007, spec 04): what it owns lives in four places, the rows,
the graph's checkpoints, ElevenLabs and the media bucket, and no transaction spans them.

So the account is first marked deleted, which refuses its every request from then on;
then its conversations and their checkpoints go, one at a time, and its voices and
avatars are taken out of use and handed to the job to remove at ElevenLabs and in the
bucket. Each step is safe to repeat, and the request may be sent again to repeat them.
The account's row stays, marked, until nothing it owned is left outside; the sweep
removes it then (E2).
"""

import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, update

from wobot.api import versions
from wobot.api.accounts import register
from wobot.api.auth import Principal, allowed_principal, get_token_claims
from wobot.api.conversations import remove
from wobot.api.errors import ApiError
from wobot.models import Account, Conversation

router = APIRouter(prefix="/v1/me")

# The sign-in a deletion needs is this recent: a phone left signed in is not enough.
RECENT_SIGN_IN_SECONDS = 5 * 60

logger = logging.getLogger(__name__)


@router.delete("", status_code=202)
async def delete_account(
    request: Request,
    principal: Annotated[Principal, Depends(allowed_principal)],
    claims: Annotated[dict[str, Any], Depends(get_token_claims)],
) -> Response:
    """Deletes the account and all it owns. Accepted once the account is marked and its
    conversations are gone; its voices and avatars are removed by the job after."""
    # Sent again for an account already marked, it only carries on what was started.
    if not principal.deleted and time.time() - claims.get("auth_time", 0) > RECENT_SIGN_IN_SECONDS:
        raise ApiError(401, "REAUTH_REQUIRED", "Sign in again to delete the account.")
    state = request.app.state
    async with state.sessionmaker() as session, session.begin():
        await register(session, principal)  # an account never seen gets its row, marked
        await session.execute(
            update(Account)
            .where(Account.account_id == principal.account_id, Account.deleted_at.is_(None))
            .values(deleted_at=func.now())
        )
        conversations = (
            await session.scalars(
                select(Conversation.conversation_id).where(
                    Conversation.owner_id == principal.account_id
                )
            )
        ).all()
    # A turn still running reads within a second that its row is gone, and stops.
    for conversation_id in conversations:
        await remove(state, principal.account_id, conversation_id)
    deleted_assets = 0
    for asset in (versions.VOICE_ASSET, versions.AVATAR_ASSET):
        async with state.sessionmaker() as session:
            left = (
                await session.scalars(
                    select(asset.id_column()).where(
                        asset.model.owner_id == principal.account_id,
                        asset.model.cleaned_at.is_(None),
                    )
                )
            ).all()
        for version_id in left:
            await versions.delete(request, asset, principal, version_id)
        deleted_assets += len(left)
    logger.info(
        "account deleted",
        extra={"conversations": len(conversations), "assets": deleted_assets},
    )
    return Response(status_code=202)
