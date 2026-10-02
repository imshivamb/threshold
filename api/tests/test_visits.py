from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from visits import AideSlot, DoorEvent, bursts, group_visits

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 9, 29, 5, 0, tzinfo=UTC)  # Tue 10:30 IST
_ids = iter(range(1, 10_000))


def ev(minutes: float, type_="motion_detected", sub_type="human", device="front"):
    return DoorEvent(next(_ids), device, type_, sub_type, T0 + timedelta(minutes=minutes))


def test_events_within_five_minutes_form_one_burst_per_device():
    events = [ev(0), ev(3), ev(7.5), ev(20), ev(1, device="backyard")]
    assert [len(b.events) for b in bursts(events)] == [3, 1, 1]


def test_delivery_is_one_visit():
    events = [ev(0, sub_type="vehicle"), ev(0.2), ev(0.3, "button_press", None), ev(0.5, sub_type="package")]
    visits, resident = group_visits(events, IST)
    assert [(v.role_guess, len(v.event_ids)) for v in visits] == [("delivery", 4)]
    assert resident == []


def test_ring_then_leave_is_one_visit_and_long_ones_are_guessed_aide():
    events = [ev(0), ev(0.1, "button_press", None), ev(90)]  # aide stays 90 min
    events += [ev(300), ev(300.1, "button_press", None), ev(320)]  # neighbour, 20 min
    visits, resident = group_visits(events, IST)
    assert [(v.role_guess, round((v.ended_at - v.started_at).total_seconds() / 60)) for v in visits] == [
        ("aide", 90),
        ("unknown", 20),
    ]
    assert resident == []


def test_aide_schedule_decides_the_role_when_present():
    slot = AideSlot.from_schedule({"days": ["mon", "tue"], "start": "10:00", "end": "11:00"})
    events = [ev(0), ev(0.1, "button_press", None), ev(15)]  # short, but inside the slot
    visits, _ = group_visits(events, IST, [slot])
    assert visits[0].role_guess == "aide"
    events = [ev(500), ev(500.1, "button_press", None), ev(600)]  # long, but outside the slot
    visits, _ = group_visits(events, IST, [slot])
    assert visits[0].role_guess == "unknown"


def test_unpaired_ring_ends_at_its_own_burst_and_resident_motion_stays_resident():
    events = [ev(0), ev(0.1, "button_press", None), ev(400), ev(460)]  # nobody seen leaving within 4 h
    visits, resident = group_visits(events, IST)
    assert len(visits) == 1 and visits[0].ended_at == visits[0].started_at + timedelta(seconds=6)
    assert [b.start for b in resident] == [T0 + timedelta(minutes=400), T0 + timedelta(minutes=460)]


def test_vehicle_alone_is_neither_visit_nor_resident():
    visits, resident = group_visits([ev(0, sub_type="vehicle")], IST)
    assert visits == [] and resident == []
