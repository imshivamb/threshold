from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from rhythm import daily_observations, learn_anchors
from visits import DoorEvent, group_visits

IST = ZoneInfo("Asia/Kolkata")
_ids = iter(range(1, 100_000))


def at(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), IST).astimezone(UTC)


def human(when: datetime) -> DoorEvent:
    return DoorEvent(next(_ids), "front", "motion_detected", "human", when)


def test_window_is_p10_minus_15_to_p90_plus_15_and_confidence_counts_missing_days():
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(28)]  # Sep 1 2026 is a Tuesday
    weekdays = [d for d in days if d.weekday() < 5]
    events = []
    for i, d in enumerate(weekdays):
        if i == 0:
            continue  # one silent weekday
        events.append(human(at(d, 7, 0) + timedelta(minutes=i % 10)))  # 07:00-07:09; P10 07:01, P90 07:08.2
        events.append(human(at(d, 21, 30)))
    visits, resident = group_visits(events, IST)
    anchors = {(a.name, a.day_group): a for a in learn_anchors(daily_observations(events, visits, resident, IST), days)}

    first = anchors[("first_activity", "weekday")]
    assert first.days_observed == len(weekdays) == 20
    assert first.days_seen == 19 and first.confidence == 0.95
    assert first.window_start == time(6, 46) and first.window_end == time(7, 23)
    assert first.can_alert
    assert ("first_activity", "weekend") not in anchors  # weekends were silent: nothing to learn


def test_first_activity_ignores_the_small_hours_and_afternoon_outliers():
    d = date(2026, 9, 7)
    events = [human(at(d, 2, 30)), human(at(d, 7, 15))]
    obs = daily_observations(events, [], [], IST)
    assert obs[d]["first_activity"]["minute"] == 7 * 60 + 15

    # Eight weekend days, one with a skipped morning: the 13:30 outlier must not stretch the window.
    sats = [date(2026, 9, 5) + timedelta(weeks=i) for i in range(8)]
    events = [human(at(s, 13, 30) if i == 3 else at(s, 8, 0) + timedelta(minutes=i)) for i, s in enumerate(sats)]
    [first] = [a for a in learn_anchors(daily_observations(events, [], [], IST), sats) if a.name == "first_activity"]
    assert first.window_end <= time(8, 30) and first.days_seen == 7 and first.confidence == 0.875
