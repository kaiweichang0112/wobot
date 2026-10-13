import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = {"schema": "app"}

    account_id: Mapped[str] = mapped_column(Text, primary_key=True)  # Firebase UID
    email: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AllowedEmail(Base):
    __tablename__ = "allowed_emails"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="allowed_emails_email_lowercase"),
        {"schema": "app"},
    )

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Conversations (migration 0007, DEC-079) ---------------------------------------------


class AccountSettings(Base):
    """What an account calls the assistant; no row means the default."""

    __tablename__ = "account_settings"
    __table_args__ = (
        CheckConstraint(
            "char_length(chatbot_name) BETWEEN 1 AND 40",
            name="account_settings_chatbot_name_length",
        ),
        CheckConstraint("version > 0", name="account_settings_version_positive"),
        {"schema": "app"},
    )

    account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("app.accounts.account_id", ondelete="CASCADE"), primary_key=True
    )
    chatbot_name: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    """One chat. Points at the checkpoint its last completed turn left: the graph's memory
    of the conversation as far as it counts."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("conversations_owner_recent_idx", "owner_id", "updated_at", "conversation_id"),
        {"schema": "app"},
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_id: Mapped[str] = mapped_column(
        Text, ForeignKey("app.accounts.account_id", ondelete="CASCADE")
    )
    title: Mapped[str | None] = mapped_column(Text)
    head_checkpoint_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Turn(Base):
    """A question and its reply, with how the attempt to answer it ended."""

    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="turns_conversation_seq"),
        CheckConstraint(
            "status IN ('running', 'completed', 'cancelled', 'failed')", name="turns_status"
        ),
        CheckConstraint("attempt > 0", name="turns_attempt_positive"),
        CheckConstraint("char_length(user_text) BETWEEN 1 AND 2000", name="turns_user_text_length"),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="turns_finished_unless_running"
        ),
        Index(
            "turns_one_running",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        {"schema": "app"},
    )

    turn_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app.conversations.conversation_id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    user_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    reply_text: Mapped[str | None] = mapped_column(Text)
    reply: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    route: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    index_version: Mapped[int | None] = mapped_column(Integer)
    app_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
