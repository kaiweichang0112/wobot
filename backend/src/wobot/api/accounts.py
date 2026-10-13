"""The signed-in account: who it is, and what it calls the assistant (spec 12)."""

import unicodedata
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from wobot.api.auth import Principal, current_principal
from wobot.api.errors import ApiError
from wobot.models import Account, AccountSettings

router = APIRouter(prefix="/v1/me")

DEFAULT_CHATBOT_NAME = "Wobot"
NAME_LIMIT = 40  # characters; the table's CHECK holds the same
# Line breaks and control characters: a name is one line of plain text.
NOT_IN_A_NAME = {"Cc", "Zl", "Zp"}


class SettingsResponse(BaseModel):
    chatbot_name: str
    version: int  # 0 until the name is first changed


class MeResponse(BaseModel):
    account_id: str
    email: str
    settings: SettingsResponse


class SettingsUpdate(BaseModel):
    chatbot_name: str
    # The version the client last read: a change made meanwhile on another device is not
    # overwritten unseen.
    expected_version: int = Field(ge=0)


async def register(session: AsyncSession, principal: Principal) -> None:
    """Records the account, or that it was seen again; whatever it owns refers to it."""
    await session.execute(
        insert(Account)
        .values(account_id=principal.account_id, email=principal.email)
        .on_conflict_do_update(
            index_elements=[Account.account_id],
            set_={"email": principal.email, "last_seen_at": func.now()},
        )
    )


async def settings_of(session: AsyncSession, account_id: str) -> SettingsResponse:
    row = await session.get(AccountSettings, account_id)
    if row is None:
        return SettingsResponse(chatbot_name=DEFAULT_CHATBOT_NAME, version=0)
    return SettingsResponse(chatbot_name=row.chatbot_name, version=row.version)


def chatbot_name(raw: str) -> str:
    """The name as kept: trimmed, one line, 1 to 40 characters. Any language goes; the
    name enters prompts as data, never as instructions (spec 12)."""
    name = raw.strip()
    if not 1 <= len(name) <= NAME_LIMIT or any(
        unicodedata.category(c) in NOT_IN_A_NAME for c in name
    ):
        raise ApiError(
            422,
            "INVALID_INPUT",
            f"The chatbot name must be 1 to {NAME_LIMIT} characters on one line.",
        )
    return name


@router.get("")
async def me(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> MeResponse:
    async with request.app.state.sessionmaker() as session, session.begin():
        await register(session, principal)
        settings = await settings_of(session, principal.account_id)
    return MeResponse(account_id=principal.account_id, email=principal.email, settings=settings)


@router.patch("/settings")
async def update_settings(
    body: SettingsUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> SettingsResponse:
    """Renames the assistant from the next reply on; replies already written keep theirs."""
    name = chatbot_name(body.chatbot_name)
    async with request.app.state.sessionmaker() as session, session.begin():
        await register(session, principal)
        if body.expected_version == 0:
            # The first change: only one device may make it.
            version = await session.scalar(
                insert(AccountSettings)
                .values(account_id=principal.account_id, chatbot_name=name, version=1)
                .on_conflict_do_nothing()
                .returning(AccountSettings.version)
            )
        else:
            version = await session.scalar(
                update(AccountSettings)
                .where(
                    AccountSettings.account_id == principal.account_id,
                    AccountSettings.version == body.expected_version,
                )
                .values(
                    chatbot_name=name, version=AccountSettings.version + 1, updated_at=func.now()
                )
                .returning(AccountSettings.version)
            )
    if version is None:
        raise ApiError(
            409,
            "REVISION_CONFLICT",
            "The name was changed on another device. Reload and try again.",
        )
    return SettingsResponse(chatbot_name=name, version=version)
