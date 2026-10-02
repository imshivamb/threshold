from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from visits import DoorEvent
from watch import AnchorView, Today, concern_signals, level_for, score_of

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 9, 29)  # a Tuesday
_ids = iter(range(1, 100_000))


def at(hh: int, mm: int, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm), IST).astimezone(UTC)


def human(when: datetime) -> DoorEvent:
    return DoorEvent(next(_ids), "front", "motion_detected", "human", when)


def ring(when: datetime) -> DoorEvent:
    return DoorEvent(next(_ids), "front", "button_press", None, when)


def anchor(name, start, end, confidence=1.0, days=15, median=None, overrides=None) -> AnchorView:
    return AnchorView(
        name, time.fromisoformat(start), time.fromisoformat(end), confidence, days,
        {"median": median or start}, overrides or {},
    )


FIRST = anchor("first_activity", "07:00", "08:00")
AIDE = anchor("aide_visit", "10:10", "10:50", median="10:30")
STATS = {"p95_gap_min": {"weekday": 11 * 60, "weekend": 11 * 60}, "night_activity_days": 0}


def today(now: datetime, events=(), last_human=None, slot=None) -> Today:
    events = sorted(events, key=lambda e: e.at)
    last = last_human or next((e for e in reversed(events) if e.is_human), None)
    return Today(IST, now, list(events), last, slot)


def kinds(signals):
    return {s.kind: s.points for s in signals}


def test_levels_follow_the_score_bands():
    assert [level_for(s) for s in (0, 19, 20, 39, 40, 59, 60, 79, 80, 100)] == [None, None, 0, 0, 1, 1, 2, 2, 3, 3]
    assert score_of([{"points": 70}, {"points": 50}]) == 100 and score_of([{"points": 30}, {"points": -40}]) == 0


def test_a_low_confidence_anchor_never_alerts():
    low = anchor("first_activity", "07:00", "08:00", confidence=0.59)
    assert concern_signals([low], today(at(9, 0)), STATS) == []


def test_nothing_alerts_before_seven_days_in_the_day_group():
    young = anchor("first_activity", "07:00", "08:00", days=6)
    assert concern_signals([young], today(at(9, 0)), STATS) == []


def test_the_aide_schedule_on_file_beats_the_learned_slot():
    signals = concern_signals([FIRST], today(at(9, 31), [human(at(7, 20))], slot=time(9, 0)), STATS)
    assert kinds(signals) == {"aide_late": 20}


def test_long_silence_beyond_the_p95_gap_adds_up_to_thirty_points():
    last = human(at(7, 20))
    assert "activity_gap" not in kinds(concern_signals([FIRST], today(at(18, 0), [last]), STATS))
    two_hours_over = concern_signals([FIRST], today(at(20, 20), [last]), STATS)
    assert kinds(two_hours_over)["activity_gap"] == 20
    assert kinds(concern_signals([FIRST], today(at(23, 59), [last]), STATS))["activity_gap"] == 30


def test_night_activity_counts_only_when_the_history_never_shows_it():
    events = [human(at(2, 15)), human(at(7, 20))]
    assert kinds(concern_signals([FIRST], today(at(8, 5), events), STATS))["night_activity"] == 15
    night_owl = {**STATS, "night_activity_days": 4}
    assert "night_activity" not in kinds(concern_signals([FIRST], today(at(8, 5), events), night_owl))


def test_an_ack_reason_on_file_takes_forty_points_off():
    temple = anchor("first_activity", "07:00", "08:00", overrides={"explanations": [{"weekday": 1, "reason": "temple on Tuesdays"}]})
    signals = concern_signals([temple], today(at(8, 5)), STATS)
    assert kinds(signals) == {"missed_anchor": 40, "explained": -40}
    assert score_of([s.as_dict() for s in signals]) == 0
    wednesday = concern_signals([temple], today(at(8, 5, DAY + timedelta(days=1))), STATS)
    assert "explained" not in kinds(wednesday)


def test_nothing_seen_while_the_camera_was_down_is_judged():
    morning_out = today(at(11, 1), last_human=human(at(21, 40, DAY - timedelta(days=1))))
    morning_out.blind_until = at(11, 0)  # camera back at 11:00
    signals = concern_signals([FIRST, AIDE], morning_out, STATS)
    assert signals == []  # first-activity window and aide slot both fell inside the outage
    later = today(at(11, 31), last_human=human(at(21, 40, DAY - timedelta(days=1))))
    later.blind_until = at(11, 0)
    assert kinds(concern_signals([FIRST, AIDE], later, STATS)) == {"aide_late": 20}  # fresh 30 min after return
