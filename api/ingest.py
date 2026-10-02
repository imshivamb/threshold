"""Writes Ring data into Postgres. Both sources (webhooks, polled history) land here."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from models import AccessLog, Device, Event, Household
from ring import HistoryEvent, RingDevice, ms_to_utc

# History uses short names; webhooks use the long ones. Store one vocabulary.
HISTORY_TYPE_MAP = {"motion": "motion_detected", "ding": "button_press"}


async def ensure_household(session: AsyncSession, settings: Settings) -> Household:
    """Single-household app: create the household on first boot."""
    household = (await session.execute(select(Household).order_by(Household.id).limit(1))).scalar_one_or_none()
    if household is None:
        household = Household(
            name=settings.household_name,
            timezone=settings.household_timezone,
            ring_mode=settings.ring_mode,
        )
        session.add(household)
        await session.flush()
    elif household.ring_mode != settings.ring_mode:
        household.ring_mode = settings.ring_mode
    return household


async def upsert_device(
    session: AsyncSession, household_id: int, device_id: str, *, name: str | None = None, kind: str | None = None
) -> Device:
    # Insert-or-ignore first: concurrent webhooks for a new device must not collide on the key.
    await session.execute(
        _insert(session, Device)
        .values(id=device_id, household_id=household_id, name=name or device_id, kind=kind or "unknown")
        .on_conflict_do_nothing(index_elements=["id"])
    )
    device = await session.get(Device, device_id)
    assert device is not None
    if name:
        device.name = name
    if kind and kind != "unknown":
        device.kind = kind
    return device


async def sync_devices(session: AsyncSession, household_id: int, devices: list[RingDevice]) -> list[Device]:
    out = []
    for d in devices:
        device = await upsert_device(session, household_id, d.id, name=d.name, kind=d.kind)
        if d.online is not None:
            device.online = d.online
        if d.status_reported_at:
            touch_heartbeat(device, d.status_reported_at)
        out.append(device)
    return out


def touch_heartbeat(device: Device, at: datetime) -> None:
    if device.last_heartbeat_at is None or _aware(device.last_heartbeat_at) < at:
        device.last_heartbeat_at = at


async def insert_event(session: AsyncSession, values: dict[str, Any]) -> bool:
    """Insert unless ring_request_id was seen before. Returns True if a row was written."""
    stmt = (
        _insert(session, Event)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["ring_request_id"])
        .returning(Event.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def store_webhook(
    session: AsyncSession, household_id: int, payload: dict[str, Any], *, source: str
) -> bool:
    """Store one Ring webhook payload (v1.1). Returns False for a duplicate request id."""
    meta, data = payload["meta"], payload["data"]
    attrs = data.get("attributes") or {}
    event_type = data["type"]
    device_id = attrs.get("source") if attrs.get("source_type", "devices") == "devices" else None
    started_at = ms_to_utc(attrs["timestamp"]) if attrs.get("timestamp") else _parse_meta_time(meta)

    if device_id:
        device = await upsert_device(session, household_id, device_id)
        touch_heartbeat(device, started_at)
        if event_type == "device_online":
            device.online = True
        elif event_type == "device_offline":
            device.online = False

    return await insert_event(
        session,
        {
            "household_id": household_id,
            "ring_request_id": meta["request_id"],
            "device_id": device_id,
            "type": event_type,
            "sub_type": attrs.get("sub_type"),
            "started_at": started_at,
            "source": source,
            "raw": payload,
        },
    )


async def store_history(
    session: AsyncSession, household_id: int, events: list[HistoryEvent], *, source: str
) -> int:
    """Store polled history events. on_demand (someone opened live view) also goes to access_log."""
    written = 0
    for ev in events:
        await upsert_device(session, household_id, ev.device_id)
        inserted = await insert_event(
            session,
            {
                "household_id": household_id,
                "ring_request_id": f"history:{ev.id}",
                "device_id": ev.device_id,
                "type": HISTORY_TYPE_MAP.get(ev.event_type, ev.event_type),
                "sub_type": ev.sub_type,
                "started_at": ev.started_at,
                "source": source,
                "raw": ev.raw,
            },
        )
        if inserted:
            written += 1
            if ev.event_type == "on_demand":
                session.add(
                    AccessLog(household_id=household_id, action="live_view", target=ev.device_id, at=ev.started_at)
                )
    return written


def _insert(session: AsyncSession, model: type):
    return (pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert)(model)


def _parse_meta_time(meta: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(meta["time"].replace("Z", "+00:00"))


def _aware(dt: datetime) -> datetime:
    # SQLite drops tzinfo; Postgres keeps it. Treat naive values as UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
