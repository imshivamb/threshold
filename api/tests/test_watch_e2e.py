"""Persona-day scenarios, end to end: 28 synthesized days plus one scenario day ->
emulator -> history backfill -> relearn -> Silence Watch ticks on a fake clock. Each test
asserts what a person would see: whether a case opens, its level, how it resolves.

The escalation agent isn't built yet; cases carry its placeholder decision. When it lands,
these tests stub its response so they stay fast and deterministic."""

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
from watch import acknowledge, anchor_statuses, load_anchors, load_today, score_of, tick

ringsynth = pytest.importorskip("ringsynth")
PERSONA = ringsynth.load_persona(Path(__file__).resolve().parents[2] / "tools" / "synth" / "personas" / "aging_in_place.yml")
TZ = PERSONA.timezone


@pytest.fixture
async def household(settings, sessions):
    async with sessions() as session:
        h = await ensure_household(session, settings)
        await session.commit()
        return h


def last_day(kind: str = "any"):
    """The scenario day: the latest past day of ``kind`` (any / weekday / weekend), so
    everything stays in the past for the emulator."""
    d = datetime.now(tz=UTC).astimezone(TZ).date() - timedelta(days=1)
    while (kind == "weekday" and d.weekday() >= 5) or (kind == "weekend" and d.weekday() < 5):
        d -= timedelta(days=1)
    return d


def plan_for(d, anomalies=(), seed=2, cutoff=time(12, 0)):
    """28 normal days, then day ``d`` with the anomalies, written up to ``cutoff``."""
    return ringsynth.plan(
        PERSONA,
        29,
        seed=seed,
        end=d + timedelta(days=1),
        now=datetime.combine(d, cutoff, TZ).astimezone(UTC),
        anomalies=[ringsynth.parse_anomaly(a, d) for a in anomalies],
    )


def typical_seed(d, anomalies=(), cutoff=time(23, 59), quiet=(), whole_day=True):
    """A seed whose day ``d`` and the evening before run every daily routine within 15 min
    of its usual time (a skipped evening leaves a long overnight silence), with nothing from
    the optional routines during the ``quiet`` (start, end) spans on ``d``. With
    ``whole_day=False`` only the evening before is checked."""
    eve = d - timedelta(days=1)
    for seed in range(300):
        events = plan_for(d, anomalies, seed, cutoff)
        ok = True
        for day in (eve, d) if whole_day else (eve,):
            todays = [e for e in events if e.at.astimezone(TZ).date() == day]
            for r in PERSONA.routines:
                if r.default.skip > 0.5 or day.weekday() not in r.days:
                    continue
                if day == d and any(r.name in a for a in anomalies):
                    continue
                usual = datetime.combine(day, r.profile(day).at, TZ)
                first = next((e.at for e in todays if e.routine == r.name), None)
                ok &= first is not None and abs(first - usual) <= timedelta(minutes=15)
        on_d = [e for e in events if e.at.astimezone(TZ).date() == d]
        for start, end in quiet:
            ok &= not any(
                start <= e.at.astimezone(TZ).time() < end for e in on_d if e.routine in ("delivery", "neighbour_drop_in")
            )
        if ok:
            return seed, on_d
    raise AssertionError("no typical seed found")


async def day_with(anomalies, emulator, ring, sessions, household, *, d=None, seed=2, cutoff=time(12, 0)):
    """Write the scenario into the emulator, backfill, relearn at 03:00 on ``d``.
    Returns ``d`` and the learned anchors for its day group, by name."""
    _, world = emulator
    d = d or last_day()
    with TestClient(create_app(world)) as http:
        ringsynth.apply(plan_for(d, anomalies, seed, cutoff), client=http)
    async with sessions() as session:
        devices = await ring.list_devices()
        since = datetime.now(tz=UTC) - timedelta(days=40)
        await backfill(session, ring, household.id, [x.id for x in devices], since=since, source="emulator")
        h = await session.merge(household)
        await rebuild(session, h, now=datetime.combine(d, time(3, 0), TZ).astimezone(UTC))
        await session.commit()
        group = "weekend" if d.weekday() >= 5 else "weekday"
        anchors = (await session.execute(select(Anchor).where(Anchor.day_group == group))).scalars()
        return d, {a.name: a for a in anchors}


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


@pytest.mark.parametrize("kind", ["weekday", "weekend"])
async def test_silent_morning_opens_a_concern_that_climbs_from_l1_to_l2(kind, emulator, ring, sessions, household):
    d = last_day(kind)
    seed, _ = typical_seed(d, ["silent_morning@0"], cutoff=time(12, 0), whole_day=False)
    d, anchors = await day_with(["silent_morning@0"], emulator, ring, sessions, household, d=d, seed=seed)
    first = anchors["first_activity"]
    assert first.window_end < time(12, 0)
    deadline = local(d, first.window_end, 1)

    # The morning on a fake clock: a concern opens by the time the first-activity window
    # closes (sooner if the overnight silence already runs past the usual longest gap).
    now, case = local(d, time(5, 0)), None
    while case is None or case.level < 1:
        assert now < local(d, time(12, 0)), "never reached level 1 by noon"
        await run_tick(sessions, household, ring, now)
        found = await cases(sessions)
        case = found[0] if found else None
        assert case is not None or now < deadline, "no concern after the window closed"
        now += timedelta(minutes=1)
    reached = now - timedelta(minutes=1)
    assert case.kind == "concern" and case.state == "open"
    assert case.level == 1  # the homeowner's own check comes first, whatever the score
    assert case.agent_decision["status"] == "pending"
    missed = next((s for s in case.signals if s["kind"] == "missed_anchor"), None)
    if missed:
        assert missed["anchor"] == "first_activity"
        assert missed["points"] == pytest.approx(min(40, 50 * first.confidence), abs=0.1)

    await run_tick(sessions, household, ring, reached + timedelta(minutes=14))
    assert (await cases(sessions))[0].level == 1  # not yet
    await run_tick(sessions, household, ring, reached + timedelta(minutes=15))  # 15 min unanswered
    [case] = await cases(sessions)
    assert case.level == 2 and case.state == "open"
    levels = [h["level"] for h in case.score_history]
    assert levels[-1] == 2 and sorted(levels) == levels and 1 in levels


async def test_camera_offline_opens_a_device_issue_not_a_concern(emulator, ring, sessions, household):
    d, anchors = await day_with(["offline@0:06:00-12:00"], emulator, ring, sessions, household)
    first = anchors["first_activity"]
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
    d, anchors = await day_with(["silent_morning@0"], emulator, ring, sessions, household)
    first = anchors["first_activity"]
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
    d = last_day()
    seed, _ = typical_seed(d, ["silent_morning@0"], cutoff=time(12, 0), whole_day=False)
    d, anchors = await day_with(["silent_morning@0"], emulator, ring, sessions, household, d=d, seed=seed)
    first = anchors["first_activity"]
    await run_tick(sessions, household, ring, local(d, first.window_end, 1))
    async with sessions() as session:
        case = (await session.execute(select(Case))).scalar_one()
        before, level = case.score, case.level
        await acknowledge(session, case, "calling", reason="at the temple this morning", now=local(d, first.window_end, 3))
        await session.commit()

    await run_tick(sessions, household, ring, local(d, first.window_end, 4))
    await run_tick(sessions, household, ring, local(d, first.window_end, 40))  # past the 15-minute timeout
    [case] = await cases(sessions)
    assert case.state == "acked" and case.level == level  # an answered case doesn't climb
    assert any(s["kind"] == "explained" and s["points"] == -40 for s in case.signals)
    # The reason cancels the missed first activity; only the growing silence still counts.
    assert case.score == score_of(case.signals) and case.score < before


async def test_a_device_removed_from_the_account_is_not_an_outage(emulator, ring, sessions, household):
    d, anchors = await day_with(["silent_morning@0"], emulator, ring, sessions, household)
    first = anchors["first_activity"]
    _, world = emulator
    front = next(x for x in world.devices.values() if x.name == "Front Door")
    world.remove(front.id)  # status now 404
    await run_tick(sessions, household, ring, local(d, first.window_end, 1))
    assert [c.kind for c in await cases(sessions)] == ["concern"]


async def test_a_normal_day_opens_no_case_and_every_anchor_is_met(emulator, ring, sessions, household):
    d = last_day()
    seed, _ = typical_seed(d)
    d, anchors = await day_with([], emulator, ring, sessions, household, d=d, seed=seed, cutoff=time(23, 59))
    assert all(a.confidence >= 0.6 for n, a in anchors.items() if n != "delivery")

    for minute in range(5 * 60, 24 * 60, 15):  # every 15 minutes, 05:00 to 23:45
        await run_tick(sessions, household, ring, local(d, time(0), minute))
    assert await cases(sessions) == []

    async with sessions() as session:
        h = await session.merge(household)
        today = await load_today(session, h, local(d, time(23, 50)))
        statuses = {s.name: s.status for s in anchor_statuses(await load_anchors(session, h.id, d), today)}
    expected = {n: "met" for n, a in anchors.items() if a.confidence >= 0.6}
    assert {n: statuses[n] for n in expected} == expected


async def test_a_late_aide_opens_a_watch_case_that_the_arrival_resolves(emulator, ring, sessions, household):
    d = last_day("weekday")
    late = ["delay@0:aide_visit+90"]
    seed, events = typical_seed(d, late, quiet=[(time(10, 0), time(13, 0))])
    d, anchors = await day_with(late, emulator, ring, sessions, household, d=d, seed=seed, cutoff=time(23, 59))
    slot = datetime.combine(d, time.fromisoformat(anchors["aide_visit"].stats["median"]), TZ).astimezone(UTC)
    arrival = min(e.at for e in events if e.routine == "aide_visit")

    await run_tick(sessions, household, ring, slot + timedelta(minutes=29))
    assert await cases(sessions) == []
    await run_tick(sessions, household, ring, slot + timedelta(minutes=31))
    [case] = await cases(sessions)
    assert (case.kind, case.level, case.score) == ("concern", 0, 20)  # watch: shown on Today, nobody pinged
    assert "aide_late" in {s["kind"] for s in case.signals}

    await run_tick(sessions, household, ring, arrival - timedelta(minutes=1))
    assert (await cases(sessions))[0].state == "open"
    await run_tick(sessions, household, ring, arrival + timedelta(minutes=1))
    [case] = await cases(sessions)
    assert (case.state, case.resolution) == ("resolved", "activity")
