"""Database tables."""

from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
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
    # Learned with the anchors: {"p95_gap_min": ..., "night_activity_days": ...}
    rhythm_stats: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
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
    # Aides only, e.g. {"days": ["mon", "tue"], "start": "10:00", "end": "11:30"}
    schedule: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Ring device id (~116 chars)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="unknown")
    name: Mapped[str] = mapped_column(String(200))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    online: Mapped[bool | None] = mapped_column(Boolean)


class Event(Base):
    __tablename__ = "events"
    # Cross-source dedupe looks up (device, type, time): the same moment can arrive as a
    # webhook and again from a history backfill.
    __table_args__ = (Index("ix_events_device_type_started", "device_id", "type", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    # Webhooks: meta.request_id. Polled history: "history:<history event id>".
    # Unique so duplicate deliveries and overlapping polls are dropped.
    ring_request_id: Mapped[str] = mapped_column(String(255), unique=True)
    device_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
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


class Visit(Base):
    """Someone at the door: arrival to departure. Rebuilt from events by visits.py."""

    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_ids: Mapped[list[int]] = mapped_column(JSONType)
    caption: Mapped[str | None] = mapped_column(Text)
    role_guess: Mapped[str] = mapped_column(String(16))  # aide / delivery / unknown


class Anchor(Base):
    """A daily moment the household usually produces, learned by the Rhythm Engine."""

    __tablename__ = "anchors"
    __table_args__ = (UniqueConstraint("household_id", "name", "day_group"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(32))  # first_activity / aide_visit / delivery / ...
    day_group: Mapped[str] = mapped_column(String(16))  # weekday / weekend
    window_start: Mapped[time] = mapped_column(Time)  # household local time
    window_end: Mapped[time] = mapped_column(Time)
    confidence: Mapped[float] = mapped_column(Float)  # days seen / days observed
    days_observed: Mapped[int] = mapped_column(Integer)
    days_seen: Mapped[int] = mapped_column(Integer)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONType)  # p10 / median / p90 minutes, durations
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # feedback learning (F12)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Case(Base):
    """Something worth a person's attention: a concern about the household, or a device issue."""

    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # concern / device_issue
    state: Mapped[str] = mapped_column(String(16), index=True)  # open / acked / resolved
    score: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=0)  # 0 watch, 1 gentle check, 2 primary, 3 circle
    local_date: Mapped[str] = mapped_column(String(10))  # household-local day the case belongs to
    signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONType)
    score_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONType)
    acks: Mapped[list[dict[str, Any]]] = mapped_column(JSONType)
    agent_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # starts the ack timeout
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(32))  # activity / device_back / confirmed_fine
