"""Silence Watch end to end: synthesized history with an anomaly on the last day ->
emulator -> backfill -> relearn -> one-minute ticks on that day."""

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ring_sandbox import webhooks as sandbox_webhooks
from ring_sandbox.emulator import create_app
from sqlalchemy import select

from ingest import backfill, ensure_household, store_webhook
from models import Anchor, Case
from routine import rebuild
from watch import acknowledge, tick

ringsynth = pytest.importorskip("ringsynth")
PERSONA = ringsynth.load_persona(Path(__file__).resolve().parents[2] / "tools" / "synth" / "personas" / "aging_in_place.yml")
TZ = PERSONA.timezone


@pytest.fixture
async def household(settings, sessions):
    async with sessions() as session:
        h = await ensure_household(session, settings)
        await session.commit()
        return h


async def day_with(anomaly: str, emulator, ring, sessions, household):
    """28 normal days, then a day ``D`` (yesterday, so everything is in the past) with the
    anomaly, written up to noon. Returns D and the learned first_activity anchor for D."""
    _, world = emulator
    d = datetime.now(tz=UTC).astimezone(TZ).date() - timedelta(days=1)
    noon = datetime.combine(d, time(12, 0), TZ).astimezone(UTC)
    events = ringsynth.plan(
        PERSONA, 29, seed=2, end=d + timedelta(days=1), now=noon, anomalies=[ringsynth.parse_anomaly(anomaly, d)]
    )
    with TestClient(create_app(world)) as http:
        ringsynth.apply(events, client=http)
    async with sessions() as session:
        devices = await ring.list_devices()
        since = datetime.now(tz=UTC) - timedelta(days=35)
        await backfill(session, ring, household.id, [x.id for x in devices], since=since, source="emulator")
        h = await session.merge(household)
        await rebuild(session, h, now=datetime.combine(d, time(3, 0), TZ).astimezone(UTC))
        await session.commit()
        group = "weekend" if d.weekday() >= 5 else "weekday"
        first = (
            await session.execute(select(Anchor).where(Anchor.name == "first_activity", Anchor.day_group == group))
        ).scalar_one()
    return d, first


def local(d, t: time, plus_min: int = 0) -> datetime:
    return datetime.combine(d, t, TZ).astimezone(UTC) + timedelta(minutes=plus_min)


async def run_tick(sessions, household, ring, now):
    async with sessions() as session:
        h = await session.merge(household)
        result = await tick(session, h, now=now, ring=ring)
        await session.commit()
        return result


async def cases(sessions):
    async with sessions() as session:
        return list((await session.execute(select(Case).order_by(Case.id))).scalars())


async def test_silent_morning_opens_a_concern_that_climbs_from_l1_to_l2(emulator, ring, sessions, household):
    d, first = await day_with("silent_morning@0", emulator, ring, sessions, household)
    assert first.confidence >= 0.9

    assert (await run_tick(sessions, household, ring, local(d, first.window_end, -1)))["opened"] is None  # window open
    opened = await run_tick(sessions, household, ring, local(d, first.window_end, 1))
    [case] = await cases(sessions)
    assert opened["opened"] == case.id and case.kind == "concern"
    assert case.level == 1 and 36 <= case.score <= 40  # missed first activity x confidence
    assert case.agent_decision["status"] == "pending"

    await run_tick(sessions, household, ring, local(d, first.window_end, 15))  # 14 min: not yet
    assert (await cases(sessions))[0].level == 1
    await run_tick(sessions, household, ring, local(d, first.window_end, 16))  # 15 min unanswered
    [case] = await cases(sessions)
    assert case.level == 2 and case.state == "open"
    assert [h["level"] for h in case.score_history] == [1, 2]


async def test_camera_offline_opens_a_device_issue_not_a_concern(emulator, ring, sessions, household):
    d, first = await day_with("offline@0:06:00-12:00", emulator, ring, sessions, household)
    result = await run_tick(sessions, household, ring, local(d, first.window_end, 1))
    [case] = await cases(sessions)
    assert result["opened"] == case.id
    assert case.kind == "device_issue" and case.score == 0
    assert {"kind": "device", "reason": "offline"}.items() <= next(s for s in case.signals if s["kind"] == "device").items()

    # Later ticks while it stays down never turn it into a concern.
    await run_tick(sessions, household, ring, local(d, first.window_end, 30))
    assert [c.kind for c in await cases(sessions)] == ["device_issue"]

    _, world = emulator
    front = next(x for x in world.devices.values() if x.name == "Front Door")
    world.record_event(front.id, "device_online", at_ms=int(local(d, time(11, 0)).timestamp() * 1000))
    await run_tick(sessions, household, ring, local(d, time(11, 1)))
    [case] = await cases(sessions)
    assert (case.state, case.resolution) == ("resolved", "device_back")


async def test_activity_at_the_door_resolves_the_concern(emulator, ring, sessions, household):
    d, first = await day_with("silent_morning@0", emulator, ring, sessions, household)
    await run_tick(sessions, household, ring, local(d, first.window_end, 1))
    _, world = emulator
    front = next(x for x in world.devices.values() if x.name == "Front Door")
    seen = local(d, first.window_end, 20)
    payload = sandbox_webhooks.build_event(event_type="motion_detected", device_id=front.id, sub_type="human", occurred_at=seen)
    async with sessions() as session:
        await store_webhook(session, household.id, payload, source="emulator")
        await session.commit()

    result = await run_tick(sessions, household, ring, local(d, first.window_end, 21))
    [case] = await cases(sessions)
    assert result["resolved"] == [case.id] and (case.state, case.resolution) == ("resolved", "activity")
    # The handled miss doesn't reopen a case later that morning.
    await run_tick(sessions, household, ring, local(d, first.window_end, 40))
    assert len(await cases(sessions)) == 1


async def test_an_ack_reason_lowers_the_score_and_stops_escalation(emulator, ring, sessions, household):
    d, first = await day_with("silent_morning@0", emulator, ring, sessions, household)
    await run_tick(sessions, household, ring, local(d, first.window_end, 1))
    async with sessions() as session:
        case = (await session.execute(select(Case))).scalar_one()
        before = case.score
        await acknowledge(session, case, "calling", reason="at the temple this morning", now=local(d, first.window_end, 3))
        await session.commit()

    await run_tick(sessions, household, ring, local(d, first.window_end, 4))
    await run_tick(sessions, household, ring, local(d, first.window_end, 40))  # past the 15-minute timeout
    [case] = await cases(sessions)
    assert case.state == "acked" and case.level == 1  # an answered case doesn't climb
    assert any(s["kind"] == "explained" and s["points"] == -40 for s in case.signals)
    # The reason cancels the missed first activity; only the growing silence still counts.
    gap = sum(s["points"] for s in case.signals if s["kind"] == "activity_gap")
    assert case.score == round(max(0, before - 40 + gap)) and case.score < before - 30


async def test_a_device_removed_from_the_account_is_not_an_outage(emulator, ring, sessions, household):
    d, first = await day_with("silent_morning@0", emulator, ring, sessions, household)
    _, world = emulator
    front = next(x for x in world.devices.values() if x.name == "Front Door")
    world.remove(front.id)  # status now 404
    await run_tick(sessions, household, ring, local(d, first.window_end, 1))
    assert [c.kind for c in await cases(sessions)] == ["concern"]
