"""Rebuild visits and anchors for a household from its stored events."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Anchor, Event, Household, Member, Visit
from rhythm import AnchorResult, activity_stats, daily_observations, learn_anchors
from visits import AideSlot, DoorEvent, group_visits

LOOKBACK_DAYS = 28
DOOR_EVENT_TYPES = ("motion_detected", "button_press")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def rebuild(session: AsyncSession, household: Household, *, now: datetime | None = None) -> dict:
    """Regroup visits over the lookback window and relearn every anchor from full past days."""
    now = now or datetime.now(tz=UTC)
    tz = ZoneInfo(household.timezone)
    today = now.astimezone(tz).date()
    window_start = datetime.combine(today - timedelta(days=LOOKBACK_DAYS), time(0), tz).astimezone(UTC)
    today_start = datetime.combine(today, time(0), tz).astimezone(UTC)

    rows = (
        await session.execute(
            select(Event.id, Event.device_id, Event.type, Event.sub_type, Event.started_at)
            .where(
                Event.household_id == household.id,
                Event.type.in_(DOOR_EVENT_TYPES),
                Event.started_at >= window_start,
                Event.started_at <= now,
            )
            .order_by(Event.started_at)
        )
    ).all()
    events = [DoorEvent(r.id, r.device_id, r.type, r.sub_type, _aware(r.started_at)) for r in rows]

    schedules = (
        await session.execute(
            select(Member.schedule).where(
                Member.household_id == household.id, Member.role == "aide", Member.schedule.is_not(None)
            )
        )
    ).scalars()
    aide_slots = [AideSlot.from_schedule(s) for s in schedules]

    groups, resident = group_visits(events, tz, aide_slots)
    await session.execute(delete(Visit).where(Visit.household_id == household.id, Visit.started_at >= window_start))
    session.add_all(
        Visit(
            household_id=household.id,
            started_at=g.started_at,
            ended_at=g.ended_at,
            event_ids=g.event_ids,
            role_guess=g.role_guess,
        )
        for g in groups
    )

    # Learn only from full days: today is still in progress.
    past = [e for e in events if e.at < today_start]
    observations = daily_observations(
        past,
        [g for g in groups if g.started_at < today_start],
        [b for b in resident if b.start < today_start],
        tz,
    )
    observed = _observed_days(past, tz, today)
    anchors = learn_anchors(observations, observed)
    await _store_anchors(session, household.id, anchors, now)
    household.rhythm_stats = activity_stats(past, tz)
    return {
        "events": len(events),
        "visits": len(groups),
        "days_observed": len(observed),
        "anchors": len(anchors),
    }


def _observed_days(past: list[DoorEvent], tz: ZoneInfo, today: date) -> list[date]:
    """Every day from the first recorded day up to yesterday, including silent ones."""
    if not past:
        return []
    first = min(e.at for e in past).astimezone(tz).date()
    return [first + timedelta(days=i) for i in range((today - first).days)]


async def _store_anchors(session: AsyncSession, household_id: int, anchors: list[AnchorResult], now: datetime) -> None:
    existing = {
        (a.name, a.day_group): a
        for a in (await session.execute(select(Anchor).where(Anchor.household_id == household_id))).scalars()
    }
    learned = {(a.name, a.day_group) for a in anchors}
    for key, row in existing.items():
        if key not in learned:
            await session.delete(row)
    for a in anchors:
        row = existing.get((a.name, a.day_group))
        if row is None:
            row = Anchor(household_id=household_id, name=a.name, day_group=a.day_group, overrides={})
            session.add(row)
        # Overrides (feedback learning) survive a relearn.
        row.window_start, row.window_end = a.window_start, a.window_end
        row.confidence, row.days_observed, row.days_seen = a.confidence, a.days_observed, a.days_seen
        row.stats, row.computed_at = a.stats, now
