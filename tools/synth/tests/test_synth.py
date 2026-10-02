from collections import Counter
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from ring_sandbox.emulator import create_app
from ring_sandbox.world import default_world

from ringsynth import apply, load_persona, parse_anomaly, parse_persona, plan

PERSONA = Path(__file__).resolve().parents[1] / "personas" / "aging_in_place.yml"
IST = ZoneInfo("Asia/Kolkata")
END = date(2026, 10, 3)  # a Saturday
NOW = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)


def local(ev):
    return ev.at.astimezone(IST)


def test_same_seed_same_history_different_seed_different_history():
    persona = load_persona(PERSONA)
    a = plan(persona, 21, seed=1, end=END, now=NOW)
    assert a == plan(persona, 21, seed=1, end=END, now=NOW)
    assert a != plan(persona, 21, seed=2, end=END, now=NOW)


def test_days_span_and_everything_is_in_the_past():
    events = plan(load_persona(PERSONA), 21, seed=1, end=END, now=NOW)
    days = {local(e).date() for e in events}
    assert min(days) == date(2026, 9, 12) and max(days) == date(2026, 10, 2)
    assert all(e.at < NOW for e in events)


def test_aide_only_on_weekdays_and_jitter_stays_near_the_set_time():
    events = plan(load_persona(PERSONA), 28, seed=3, end=END, now=NOW)
    arrivals = [e for e in events if e.routine == "aide_visit" and e.type == "button_press"]
    assert arrivals and all(local(e).weekday() < 5 for e in arrivals)
    for e in arrivals:  # 10:30 ± 3 x 8 min
        assert time(10, 6) <= local(e).time() <= time(10, 55)


def test_weekend_profile_moves_the_morning_routine():
    events = plan(load_persona(PERSONA), 28, seed=4, end=END, now=NOW)
    firsts = {}
    for e in events:
        if e.routine == "morning_step_out":
            firsts.setdefault(local(e).date(), local(e).time())
    weekend = [t for d, t in firsts.items() if d.weekday() >= 5]
    weekday = [t for d, t in firsts.items() if d.weekday() < 5]
    assert min(weekend) > max(weekday)  # 08:10 ± 45 min vs 07:25 ± 30 min


def test_skip_probability_is_respected_roughly():
    events = plan(load_persona(PERSONA), 200, seed=5, end=END, now=NOW)
    deliveries = {local(e).date() for e in events if e.routine == "delivery"}
    assert 0.25 < len(deliveries) / 200 < 0.45  # skip 0.65


def test_scenario_routines_reuse_ring_sandbox_steps():
    events = plan(load_persona(PERSONA), 60, seed=6, end=END, now=NOW)
    first_delivery_day = min(local(e).date() for e in events if e.routine == "delivery")
    steps = [(e.type, e.sub_type) for e in events if e.routine == "delivery" and local(e).date() == first_delivery_day]
    assert steps[:4] == [
        ("motion_detected", "vehicle"),
        ("motion_detected", "human"),
        ("button_press", None),
        ("motion_detected", "package"),
    ]


def test_silent_morning_removes_everything_before_noon():
    persona = load_persona(PERSONA)
    anomaly = parse_anomaly("silent_morning@-1", END)
    events = plan(persona, 7, seed=1, end=END, now=NOW, anomalies=[anomaly])
    yesterday = [local(e) for e in events if local(e).date() == date(2026, 10, 2)]
    assert yesterday and all(t.time() >= time(12, 0) for t in yesterday)
    normal = plan(persona, 7, seed=1, end=END, now=NOW)
    assert any(local(e).date() == date(2026, 10, 2) and local(e).hour < 12 for e in normal)


def test_offline_window_drops_events_and_marks_the_device_offline():
    anomaly = parse_anomaly("offline@-1:07:00-12:00", END)
    events = plan(load_persona(PERSONA), 7, seed=1, end=END, now=NOW, anomalies=[anomaly])
    day = [e for e in events if local(e).date() == date(2026, 10, 2)]
    kinds = [(e.type, local(e).strftime("%H:%M")) for e in day if e.type.startswith("device_")]
    assert kinds == [("device_offline", "07:00"), ("device_online", "12:00")]
    assert not [e for e in day if e.type == "motion_detected" and time(7) <= local(e).time() < time(12)]


def test_bad_persona_is_rejected():
    with pytest.raises(ValueError, match="no routines"):
        parse_persona({"name": "empty"})
    with pytest.raises(ValueError, match="unknown ring-sandbox scenario"):
        parse_persona({"routines": [{"name": "x", "at": "08:00", "scenario": "nope"}]})


def test_apply_writes_history_into_the_emulator_without_webhooks():
    world = default_world()
    events = plan(load_persona(PERSONA), 14, seed=1)
    with TestClient(create_app(world)) as http:
        assert apply(events, client=http) == len(events)
    front = next(d for d in world.devices.values() if d.name == "Front Door")
    kinds = Counter(r.event_type for r in front.history)
    assert kinds["motion"] == sum(1 for e in events if e.type == "motion_detected")
    assert kinds["ding"] == sum(1 for e in events if e.type == "button_press")
    assert not [d for d in world.delivered if d.get("kind") == "webhook"]
