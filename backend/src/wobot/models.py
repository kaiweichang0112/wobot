import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
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

from wobot.assets.frames import FRAME_KEYS


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
        CheckConstraint("mode IN ('text', 'avatar')", name="turns_mode"),
        CheckConstraint("heard_upto >= 0", name="turns_heard_upto_positive"),
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
    # Avatar mode (migration 0009, DEC-089): the reply is also spoken.
    mode: Mapped[str] = mapped_column(Text, server_default=text("'text'"))
    speech: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    heard_upto: Mapped[int | None] = mapped_column(Integer)
    speech_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Assets (migration 0008, DEC-086) -----------------------------------------------------

STATUSES = ("queued", "running", "ready", "failed", "active", "retired", "deleted")
MAKING = ("queued", "running")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _version_rules(table: str) -> tuple[CheckConstraint | Index, ...]:
    return (
        CheckConstraint(_in("status", STATUSES), name=f"{table}_status"),
        CheckConstraint(
            f"({_in('status', MAKING)}) = (finished_at IS NULL)",
            name=f"{table}_finished_unless_making",
        ),
        CheckConstraint(
            "status <> 'running' OR started_at IS NOT NULL", name=f"{table}_started_when_running"
        ),
        CheckConstraint(
            f"cleaned_at IS NULL OR {_in('status', ('failed', 'retired', 'deleted'))}",
            name=f"{table}_cleaned_when_unused",
        ),
        Index(f"{table}_owner_recent_idx", "owner_id", "created_at"),
        Index(
            f"{table}_one_active",
            "owner_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            f"{table}_one_making",
            "owner_id",
            unique=True,
            postgresql_where=text(_in("status", MAKING)),
        ),
    )


class _AssetVersion:
    """What every version keeps about the job that made it: the row is the job."""

    owner_id: Mapped[str] = mapped_column(
        Text, ForeignKey("app.accounts.account_id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text)
    execution: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VoiceVersion(_AssetVersion, Base):
    """One clone of an account's voice, from one recorded sample."""

    __tablename__ = "voice_versions"
    __table_args__ = (
        *_version_rules("voice_versions"),
        CheckConstraint(
            "status NOT IN ('ready', 'active') OR provider_voice_id IS NOT NULL",
            name="voice_versions_cloned_when_usable",
        ),
        CheckConstraint("sample_seconds > 0", name="voice_versions_sample_seconds_positive"),
        {"schema": "app"},
    )

    voice_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # made by the app
    provider_voice_id: Mapped[str | None] = mapped_column(Text, unique=True)
    sample_seconds: Mapped[float] = mapped_column(Float)


class AvatarVersion(_AssetVersion, Base):
    """One set of 21 frames made from one photo, and what made them."""

    __tablename__ = "avatar_versions"
    __table_args__ = (
        *_version_rules("avatar_versions"),
        CheckConstraint(
            "expression_schema_version > 0", name="avatar_versions_expression_schema_positive"
        ),
        CheckConstraint("prompt_version > 0", name="avatar_versions_prompt_version_positive"),
        CheckConstraint(_in("quality", ("low", "medium", "high")), name="avatar_versions_quality"),
        {"schema": "app"},
    )

    avatar_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # made by the app
    expression_schema_version: Mapped[int] = mapped_column(Integer)
    prompt_version: Mapped[int] = mapped_column(Integer)
    prompt_hash: Mapped[str] = mapped_column(Text)
    image_model: Mapped[str] = mapped_column(Text)
    quality: Mapped[str] = mapped_column(Text)


class AvatarFrame(Base):
    """One of an avatar's 21 frames; the frame it is made from follows from its key."""

    __tablename__ = "avatar_frames"
    __table_args__ = (
        CheckConstraint(_in("frame_key", FRAME_KEYS), name="avatar_frames_frame_key"),
        CheckConstraint(
            _in("status", ("pending", "running", "ready", "failed")), name="avatar_frames_status"
        ),
        CheckConstraint(
            "status <> 'ready' OR (storage_key IS NOT NULL AND sha256 IS NOT NULL)",
            name="avatar_frames_stored_when_ready",
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="avatar_frames_sha256_hex"),
        CheckConstraint("attempts >= 0", name="avatar_frames_attempts_not_negative"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0", name="avatar_frames_tokens_not_negative"
        ),
        {"schema": "app"},
    )

    avatar_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app.avatar_versions.avatar_id", ondelete="CASCADE"), primary_key=True
    )
    frame_key: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
