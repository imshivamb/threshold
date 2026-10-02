"""First five tables. visits, anchors and cases arrive with the Rhythm Engine and Silence Watch."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on Postgres, plain JSON elsewhere (SQLite in unit tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    pass


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    ring_mode: Mapped[str] = mapped_column(String(16))  # playground / emulator
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_days: Mapped[int] = mapped_column(Integer, default=7)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # homeowner / caregiver / aide
    name: Mapped[str] = mapped_column(String(200))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    sees_snapshots: Mapped[bool] = mapped_column(Boolean, default=False)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # Ring device id
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="unknown")
    name: Mapped[str] = mapped_column(String(200))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    online: Mapped[bool | None] = mapped_column(Boolean)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    # Webhooks: meta.request_id. Polled history: "history:<history event id>".
    # Unique so duplicate deliveries and overlapping polls are dropped.
    ring_request_id: Mapped[str] = mapped_column(String(255), unique=True)
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    sub_type: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(16))  # playground / webhook / emulator
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessLog(Base):
    __tablename__ = "access_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    # Null when the access did not come from a Threshold member (e.g. a Ring live view).
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    # viewed_timeline / viewed_snapshot / live_view / asked / exported / deleted
    action: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(255))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
