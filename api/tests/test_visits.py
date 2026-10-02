from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from visits import AideSlot, DoorEvent, group_visits

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 9, 29, 5, 0, tzinfo=UTC)  # Tue 10:30 IST
_ids = iter(range(1, 10_000))


def ev(minutes: float, type_="motion_detected", sub_type="human", device="front"):
    return DoorEvent(next(_ids), device, type_, sub_type, T0 + timedelta(minutes=minutes))


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
