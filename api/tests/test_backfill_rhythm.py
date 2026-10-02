"""Synthesized persona history -> emulator -> Ring history backfill -> visits and anchors."""

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ring_sandbox import webhooks as sandbox_webhooks
from ring_sandbox.emulator import create_app
from sqlalchemy import func, select

from ingest import backfill, ensure_household, store_webhook
from models import Anchor, Event, Visit
from routine import rebuild

ringsynth = pytest.importorskip("ringsynth")
PERSONA = Path(__file__).resolve().parents[2] / "tools" / "synth" / "personas" / "aging_in_place.yml"


@pytest.fixture
async def household(settings, sessions):
    async with sessions() as session:
        h = await ensure_household(session, settings)  # Asia/Kolkata, like the persona
        await session.commit()
        return h


def synthesize(world, days=21, anomalies=()):
    events = ringsynth.plan(ringsynth.load_persona(PERSONA), days, seed=1, anomalies=list(anomalies))
    with TestClient(create_app(world)) as http:
        ringsynth.apply(events, client=http)
    return events


async def backfill_all(session, ring, household_id):
    devices = await ring.list_devices()
    since = datetime.now(tz=UTC) - timedelta(days=28)
    return await backfill(session, ring, household_id, [d.id for d in devices], since=since, source="emulator")


async def test_three_weeks_of_history_teach_the_routine(emulator, ring, sessions, household):
    _, world = emulator
    planned = synthesize(world)
    door_events = [e for e in planned if e.type in ("motion_detected", "button_press")]

    async with sessions() as session:
        written = await backfill_all(session, ring, household.id)
        assert sum(written.values()) == len(door_events)
        summary = await rebuild(session, household)
        await session.commit()
        anchors = {(a.name, a.day_group): a for a in (await session.execute(select(Anchor))).scalars()}
        roles = (await session.execute(select(Visit.role_guess, func.count()).group_by(Visit.role_guess))).all()

    assert summary["days_observed"] == 21
    # Bounds follow from the persona (07:25, jitter sd 10 min clipped at 3 sd, window pad 15 min),
    # not from one draw: the synthesized days shift with today's date.
    first = anchors[("first_activity", "weekday")]
    assert time(6, 40) <= first.window_start <= time(7, 20) and time(7, 30) <= first.window_end <= time(8, 15)
    assert first.confidence >= 0.9 and first.days_observed == 15

    aide = anchors[("aide_visit", "weekday")]
    assert time(10, 0) <= aide.window_start and aide.window_end <= time(11, 0)
    assert aide.confidence >= 0.9 and 70 <= aide.stats["median_duration_min"] <= 105
    assert ("aide_visit", "weekend") not in anchors

    delivery = anchors[("delivery", "weekday")]
    assert delivery.confidence < 0.6  # couriers come on ~1 day in 3: shown, never alerts

    walk = anchors[("out_and_back", "weekday")]
    assert time(17, 0) <= walk.window_start and walk.window_end <= time(18, 30)
    assert time(21, 0) <= anchors[("last_activity", "weekday")].window_start

    assert dict(roles)["aide"] >= 14 and dict(roles)["delivery"] >= 5


async def test_backfill_twice_and_webhook_then_history_never_double_count(emulator, ring, sessions, household):
    _, world = emulator
    front = next(d for d in world.devices.values() if d.name == "Front Door")
    when = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(hours=2)
    world.record_event(front.id, "motion_detected", sub_type="human", at_ms=int(when.timestamp() * 1000))
    payload = sandbox_webhooks.build_event(
        event_type="motion_detected", device_id=front.id, sub_type="human", occurred_at=when
    )

    async with sessions() as session:
        assert await store_webhook(session, household.id, payload, source="emulator")  # arrived live
        assert sum((await backfill_all(session, ring, household.id)).values()) == 0  # same moment in history
        assert sum((await backfill_all(session, ring, household.id)).values()) == 0
        await session.commit()
        assert (await session.execute(select(func.count(Event.id)))).scalar_one() == 1


async def test_a_silent_morning_shows_as_a_missed_first_activity(emulator, ring, sessions, household):
    _, world = emulator
    today = datetime.now(tz=UTC).astimezone(ringsynth.load_persona(PERSONA).timezone).date()
    silent = ringsynth.parse_anomaly("silent_morning@-1", today)
    synthesize(world, anomalies=[silent])
    async with sessions() as session:
        await backfill_all(session, ring, household.id)
        await rebuild(session, household)
        await session.commit()
        first = (
            await session.execute(select(Anchor).where(Anchor.name == "first_activity"))
        ).scalars().all()
    # Yesterday's first activity came only after noon, outside every learned window.
    assert all(a.window_end < time(12, 0) for a in first)
