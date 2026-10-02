"""Group door events into bursts and visits. Pure functions; no database.

A *burst* is a run of door events with gaps of at most BURST_GAP. Bursts become:
  - a delivery visit: a parcel is seen, or a vehicle arrives and someone rings
  - a doorbell visit: someone rings; it lasts until the next burst with a person and no
    ring (they leave), if that comes within MAX_VISIT
  - otherwise a resident burst: the person who lives there stepping in or out
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

BURST_GAP = timedelta(minutes=5)
MAX_VISIT = timedelta(hours=4)
# With no aide schedule on file, a rung-in visit at least this long is labelled "likely_aide".
AIDE_MIN_DURATION = timedelta(minutes=45)
AIDE_SLOT_SLACK = timedelta(minutes=30)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class DoorEvent:
    id: int
    device_id: str | None
    type: str  # motion_detected / button_press
    sub_type: str | None
    at: datetime

    @property
    def is_human(self) -> bool:
        return self.type == "motion_detected" and self.sub_type == "human"


@dataclass
class Burst:
    events: list[DoorEvent] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.events[0].at

    @property
    def end(self) -> datetime:
        return self.events[-1].at

    def has(self, type_: str, sub_type: str | None = None) -> bool:
        return any(e.type == type_ and (sub_type is None or e.sub_type == sub_type) for e in self.events)

    @property
    def has_human(self) -> bool:
        return self.has("motion_detected", "human")

    @property
    def rang(self) -> bool:
        return self.has("button_press")

    @property
    def is_delivery(self) -> bool:
        return self.has("motion_detected", "package") or (self.has("motion_detected", "vehicle") and self.rang)


@dataclass
class VisitGroup:
    started_at: datetime
    ended_at: datetime
    event_ids: list[int]
    role_guess: str  # aide / likely_aide / delivery / unknown


AIDE_ROLES = ("aide", "likely_aide")


@dataclass(frozen=True)
class AideSlot:
    days: frozenset[int]
    start: time
    end: time

    @classmethod
    def from_schedule(cls, schedule: dict) -> AideSlot:
        days = schedule.get("days") or WEEKDAYS[:5]
        return cls(
            days=frozenset(WEEKDAYS.index(str(d).lower()[:3]) for d in days),
            start=time.fromisoformat(schedule["start"]),
            end=time.fromisoformat(schedule["end"]),
        )

    def matches(self, at_local: datetime) -> bool:
        if at_local.weekday() not in self.days:
            return False
        day = at_local.date()
        lo = datetime.combine(day, self.start, at_local.tzinfo) - AIDE_SLOT_SLACK
        hi = datetime.combine(day, self.end, at_local.tzinfo)
        return lo <= at_local <= hi


def bursts(events: list[DoorEvent]) -> list[Burst]:
    """Bursts per device (cameras don't interleave), then ordered by start time."""
    open_by_device: dict[str | None, Burst] = {}
    out: list[Burst] = []
    for ev in sorted(events, key=lambda e: (e.at, e.id)):
        current = open_by_device.get(ev.device_id)
        if current and ev.at - current.end <= BURST_GAP:
            current.events.append(ev)
        else:
            current = Burst([ev])
            open_by_device[ev.device_id] = current
            out.append(current)
    return sorted(out, key=lambda b: b.start)


def group_visits(
    events: list[DoorEvent], tz: ZoneInfo, aide_slots: list[AideSlot] = ()  # type: ignore[assignment]
) -> tuple[list[VisitGroup], list[Burst]]:
    """Return (visits, resident bursts). Resident bursts feed the out-and-back anchor."""
    all_bursts = bursts(events)
    used: set[int] = set()
    visits: list[VisitGroup] = []
    for i, b in enumerate(all_bursts):
        if i in used:
            continue
        if b.is_delivery:
            used.add(i)
            visits.append(VisitGroup(b.start, b.end, [e.id for e in b.events], "delivery"))
        elif b.rang:
            used.add(i)
            members = list(b.events)
            ended = b.end
            for j in range(i + 1, len(all_bursts)):
                nxt = all_bursts[j]
                if nxt.start - b.start > MAX_VISIT:
                    break
                if j in used or nxt.rang or nxt.is_delivery:
                    continue
                if nxt.has_human:
                    used.add(j)
                    members += nxt.events
                    ended = nxt.end
                    break
            visits.append(VisitGroup(b.start, ended, [e.id for e in members], _guess_role(b.start, ended, tz, aide_slots)))
    resident = [b for i, b in enumerate(all_bursts) if i not in used and b.has_human]
    return visits, resident


def _guess_role(start: datetime, end: datetime, tz: ZoneInfo, aide_slots: list[AideSlot]) -> str:
    if aide_slots:
        local = start.astimezone(tz)
        return "aide" if any(slot.matches(local) for slot in aide_slots) else "unknown"
    return "likely_aide" if end - start >= AIDE_MIN_DURATION else "unknown"
