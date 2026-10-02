"""Rhythm Engine: learn a household's daily anchors from past days. Pure functions.

For each anchor template and day group (weekday / weekend), collect the anchor's local
time-of-day on every day it happened, then:
  window     = P10 - 15 min  to  P90 + 15 min
  confidence = days it happened / days observed
An anchor may alert only with confidence >= 0.6 and at least 7 days observed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from visits import Burst, DoorEvent, VisitGroup

ANCHORS = ("first_activity", "aide_visit", "delivery", "out_and_back", "last_activity")
DAY_GROUPS = ("weekday", "weekend")
WINDOW_PAD_MIN = 15
MIN_CONFIDENCE = 0.6
MIN_DAYS_OBSERVED = 7
DAY_START = time(4, 0)  # first activity counts from 04:00
OUT_AND_BACK = (timedelta(minutes=20), timedelta(minutes=180))


@dataclass(frozen=True)
class AnchorResult:
    name: str
    day_group: str
    window_start: time
    window_end: time
    confidence: float
    days_observed: int
    days_seen: int
    stats: dict

    @property
    def can_alert(self) -> bool:
        return can_alert(self.confidence, self.days_observed)


def can_alert(confidence: float, days_observed: int) -> bool:
    return confidence >= MIN_CONFIDENCE and days_observed >= MIN_DAYS_OBSERVED


def day_group(d: date) -> str:
    return "weekend" if d.weekday() >= 5 else "weekday"


def percentile(values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default)."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _minutes(t: datetime) -> float:
    return t.hour * 60 + t.minute + t.second / 60


def _clock(minutes: float) -> time:
    m = int(round(max(0, min(23 * 60 + 59, minutes))))
    return time(m // 60, m % 60)


def daily_observations(
    events: list[DoorEvent], visits: list[VisitGroup], resident: list[Burst], tz: ZoneInfo
) -> dict[date, dict[str, dict]]:
    """For each local day, when each anchor happened: {day: {anchor: {"minute": m, ...}}}."""
    out: dict[date, dict[str, dict]] = defaultdict(dict)

    for ev in sorted((e for e in events if e.is_human), key=lambda e: e.at):
        local = ev.at.astimezone(tz)
        day = out[local.date()]
        if local.time() >= DAY_START and "first_activity" not in day:
            day["first_activity"] = {"minute": _minutes(local)}
        day["last_activity"] = {"minute": _minutes(local)}

    for v in sorted(visits, key=lambda v: v.started_at):
        if v.role_guess not in ("aide", "delivery"):
            continue
        name = "aide_visit" if v.role_guess == "aide" else "delivery"
        local = v.started_at.astimezone(tz)
        out[local.date()].setdefault(
            name, {"minute": _minutes(local), "duration_min": (v.ended_at - v.started_at).total_seconds() / 60}
        )

    for a, b in zip(resident, resident[1:]):
        gap = b.start - a.end
        local = a.start.astimezone(tz)
        if OUT_AND_BACK[0] <= gap <= OUT_AND_BACK[1] and b.start.astimezone(tz).date() == local.date():
            out[local.date()].setdefault("out_and_back", {"minute": _minutes(local), "away_min": gap.total_seconds() / 60})
    return out


def learn_anchors(observations: dict[date, dict[str, dict]], observed_days: list[date]) -> list[AnchorResult]:
    """Anchors per (template, day group) over ``observed_days``. Days not observed are ignored."""
    days_by_group: dict[str, list[date]] = defaultdict(list)
    for d in observed_days:
        days_by_group[day_group(d)].append(d)

    results = []
    for name in ANCHORS:
        for group in DAY_GROUPS:
            days = days_by_group.get(group, [])
            seen = [observations[d][name] for d in days if name in observations.get(d, {})]
            if not days or not seen:
                continue
            minutes = [o["minute"] for o in seen]
            p10, p50, p90 = percentile(minutes, 0.1), median(minutes), percentile(minutes, 0.9)
            stats = {"n": len(seen), "p10": _clock(p10).isoformat("minutes"), "median": _clock(p50).isoformat("minutes"),
                     "p90": _clock(p90).isoformat("minutes")}
            for extra in ("duration_min", "away_min"):
                vals = [o[extra] for o in seen if extra in o]
                if vals:
                    stats[f"median_{extra}"] = round(median(vals))
            results.append(
                AnchorResult(
                    name=name,
                    day_group=group,
                    window_start=_clock(p10 - WINDOW_PAD_MIN),
                    window_end=_clock(p90 + WINDOW_PAD_MIN),
                    confidence=round(len(seen) / len(days), 3),
                    days_observed=len(days),
                    days_seen=len(seen),
                    stats=stats,
                )
            )
    return results
