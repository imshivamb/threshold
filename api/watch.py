"""Silence Watch: every minute, compare today with the learned rhythm.

1. Anchor status: each of today's anchors is met, pending (window still open) or missed.
2. Signals: missed anchors, a gap in human activity beyond the household's P95 gap for
   that day group, a late
   aide, unusual night activity. Only anchors that may alert (confidence >= 0.6 and
   7+ observed days in today's day group) count.
3. Device health first: if a watched camera is offline or its heartbeat is stale, the
   signals open a device_issue case, not a concern.
4. Otherwise the concern score (0-100) sets the level; an unanswered level climbs.
5. Any new human activity at the door resolves an open concern.

Detection is deterministic; the agent (Tuesday) only explains and drafts messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Anchor, Case, Device, Event, Household, Member
from rhythm import NIGHT, can_alert, day_group
from ring import RingClient, RingError
from visits import AideSlot, DoorEvent, group_visits

log = logging.getLogger("threshold.watch")

DOOR_TYPES = ("motion_detected", "button_press")
LOOKBACK = timedelta(days=28)

# Concern score points (build spec, "Detection logic", with the owner's adjustments)
MISSED_FIRST_ACTIVITY_MAX = 40
MISSED_FIRST_ACTIVITY_RATE = 50  # x anchor confidence: confidence >= 0.8 alone reaches level 1
GAP_MAX, GAP_POINTS_PER_HOUR = 30, 10
AIDE_LATE, AIDE_GRACE = 20, timedelta(minutes=30)
NIGHT_ACTIVITY = 15
EXPLAINED = -40

ACK_TIMEOUT = {1: timedelta(minutes=15), 2: timedelta(minutes=30)}
MAX_LEVEL = 3
PLACEHOLDER_DECISION = {"status": "pending", "note": "the escalation agent fills this in"}


def level_for(score: int) -> int | None:
    """None below 20: no case. 0 watch, 1 gentle check, 2 primary, 3 whole circle."""
    if score < 20:
        return None
    return 0 if score < 40 else 1 if score < 60 else 2 if score < 80 else 3


def next_level(current: int | None, score: int) -> int | None:
    """The level a score allows from ``current`` (None: no case yet). A concern passes
    through level 1, the homeowner's own check, before anyone else is told: the score alone
    never jumps past level 1 unless it is 80+. Levels never go down while a case is open."""
    target = level_for(score)
    if target is None:
        return current
    if target == 2 and (current is None or current < 2):
        target = 1  # level 2 comes from level 1 going unanswered
    return target if current is None else max(current, target)


def clamp_score(points: float) -> int:
    return int(round(max(0, min(100, points))))


# ---------------------------------------------------------------------- pure parts


@dataclass(frozen=True)
class AnchorView:
    name: str
    window_start: time
    window_end: time
    confidence: float
    days_observed: int
    stats: dict
    overrides: dict = field(default_factory=dict)

    @property
    def can_alert(self) -> bool:
        return can_alert(self.confidence, self.days_observed)

    def explanation(self, day: date) -> str | None:
        """An ack reason on file for this anchor on this day (or every such weekday)."""
        for e in self.overrides.get("explanations", []):
            if e.get("date") == day.isoformat() or e.get("weekday") == day.weekday():
                return e.get("reason")
        return None

    @classmethod
    def of(cls, a: Anchor) -> AnchorView:
        return cls(a.name, a.window_start, a.window_end, a.confidence, a.days_observed, a.stats, a.overrides or {})


@dataclass(frozen=True)
class AnchorStatus:
    name: str
    window: tuple[str, str]
    confidence: float
    can_alert: bool
    status: str  # met / pending / missed
    met_at: str | None = None


@dataclass(frozen=True)
class Signal:
    key: str  # stable per day, so a handled signal never reopens a case
    kind: str  # missed_anchor / activity_gap / aide_late / night_activity / explained
    points: float
    detail: dict

    def as_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "points": round(self.points, 1), **self.detail}


@dataclass
class Today:
    """Everything the watch knows about the household's current local day."""

    tz: ZoneInfo
    now: datetime
    events: list[DoorEvent]  # door events since local midnight
    last_human: DoorEvent | None  # most recent human activity, any day
    aide_slot_start: time | None  # from the aide's schedule, if on file for today
    # A camera was down until this moment today: nothing before it can be judged.
    blind_until: datetime | None = None

    @property
    def local_now(self) -> datetime:
        return self.now.astimezone(self.tz)

    @property
    def day(self) -> date:
        return self.local_now.date()


def _local(e: DoorEvent, tz: ZoneInfo) -> datetime:
    return e.at.astimezone(tz)


def anchor_statuses(anchors: list[AnchorView], today: Today) -> list[AnchorStatus]:
    tz, now_t = today.tz, today.local_now.time()
    human = [e for e in today.events if e.is_human]
    visits, _ = group_visits(today.events, tz)
    out = []
    for a in sorted(anchors, key=lambda a: a.window_start):
        start, end = a.window_start, a.window_end
        met: datetime | None = None
        if a.name == "first_activity":
            first = next((e for e in human if _local(e, tz).time() >= time(4, 0)), None)
            if first and _local(first, tz).time() <= end:
                met = _local(first, tz)
        elif a.name in ("out_and_back", "last_activity"):
            met = next((_local(e, tz) for e in human if start <= _local(e, tz).time() <= end), None)
        elif a.name == "aide_visit":
            # Arrival is enough; the visit's length (and so its role) isn't known yet.
            early = (datetime.combine(today.day, start) - AIDE_GRACE).time()
            met = next(
                (v.started_at.astimezone(tz) for v in visits
                 if v.role_guess != "delivery" and early <= v.started_at.astimezone(tz).time() <= end),
                None,
            )
        elif a.name == "delivery":
            met = next(
                (v.started_at.astimezone(tz) for v in visits
                 if v.role_guess == "delivery" and v.started_at.astimezone(tz).time() <= end),
                None,
            )
        status = "met" if met else ("pending" if now_t < end else "missed")
        out.append(
            AnchorStatus(
                a.name,
                (start.isoformat("minutes"), end.isoformat("minutes")),
                a.confidence,
                a.can_alert,
                status,
                met.time().isoformat("minutes") if met else None,
            )
        )
    return out


def concern_signals(anchors: list[AnchorView], today: Today, stats: dict | None) -> list[Signal]:
    """Every reason for concern right now, with points. Empty until today's day group
    has 7+ observed days."""
    if not any(a.days_observed >= 7 for a in anchors):
        return []
    stats = stats or {}
    day = today.day.isoformat()
    by_name = {a.name: a for a in anchors}
    signals: list[Signal] = []

    explained_reason = None
    for s in anchor_statuses(anchors, today):
        a = by_name[s.name]
        if s.status != "missed" or not a.can_alert:
            continue
        if today.blind_until and datetime.combine(today.day, a.window_end, today.tz) <= today.blind_until:
            continue  # the window closed while the camera was down
        points = (
            min(MISSED_FIRST_ACTIVITY_MAX, MISSED_FIRST_ACTIVITY_RATE * a.confidence) if a.name == "first_activity" else 0
        )
        signals.append(
            Signal(f"missed:{a.name}:{day}", "missed_anchor", points,
                   {"anchor": a.name, "window": list(s.window), "confidence": a.confidence})
        )
        explained_reason = explained_reason or a.explanation(today.day)
    if explained_reason:
        signals.append(Signal(f"explained:{day}", "explained", EXPLAINED, {"reason": explained_reason}))

    p95 = stats.get("p95_gap_min")
    if isinstance(p95, dict):  # older rhythm_stats held one number; relearning upgrades it
        p95 = p95.get(day_group(today.day))
    if today.last_human and p95:
        since = max(today.last_human.at, today.blind_until or today.last_human.at)
        since_h = (today.now - since).total_seconds() / 3600
        excess = since_h - p95 / 60
        if excess > 0:
            signals.append(
                Signal(f"gap:{today.last_human.id}", "activity_gap", min(GAP_MAX, excess * GAP_POINTS_PER_HOUR),
                       {"hours_since_activity": round(since_h, 1), "p95_gap_hours": round(p95 / 60, 1)})
            )

    slot = today.aide_slot_start
    aide = by_name.get("aide_visit")
    if slot is None and aide and aide.can_alert:
        slot = time.fromisoformat(aide.stats["median"])
    if slot:
        slot_at = datetime.combine(today.day, slot, today.tz)
        deadline = max(slot_at, today.blind_until or slot_at) + AIDE_GRACE
        if today.now >= deadline:
            visits, _ = group_visits(today.events, today.tz)
            if not any(v.role_guess != "delivery" and v.started_at >= slot_at - AIDE_GRACE for v in visits):
                signals.append(Signal(f"aide_late:{day}", "aide_late", AIDE_LATE, {"slot": slot.isoformat("minutes")}))

    night = [e for e in today.events if e.is_human and NIGHT[0] <= _local(e, today.tz).time() < NIGHT[1]]
    if night and stats.get("night_activity_days") == 0:
        signals.append(
            Signal(f"night:{day}", "night_activity", NIGHT_ACTIVITY,
                   {"first_seen": _local(night[0], today.tz).time().isoformat("minutes")})
        )
    return signals


def score_of(signals: list[dict]) -> int:
    return clamp_score(sum(s["points"] for s in signals))


# ---------------------------------------------------------------------- database


async def load_today(session: AsyncSession, household: Household, now: datetime) -> Today:
    tz = ZoneInfo(household.timezone)
    local_now = now.astimezone(tz)
    midnight = datetime.combine(local_now.date(), time(0), tz).astimezone(UTC)
    rows = (
        await session.execute(
            select(Event.id, Event.device_id, Event.type, Event.sub_type, Event.started_at)
            .where(
                Event.household_id == household.id,
                Event.type.in_(DOOR_TYPES),
                Event.started_at >= midnight,
                Event.started_at <= now,
            )
            .order_by(Event.started_at)
        )
    ).all()
    last = (
        await session.execute(
            select(Event.id, Event.device_id, Event.type, Event.sub_type, Event.started_at)
            .where(
                Event.household_id == household.id,
                Event.type == "motion_detected",
                Event.sub_type == "human",
                Event.started_at <= now,
            )
            .order_by(Event.started_at.desc())
            .limit(1)
        )
    ).first()
    slot = None
    for sched in (
        await session.execute(
            select(Member.schedule).where(
                Member.household_id == household.id, Member.role == "aide", Member.schedule.is_not(None)
            )
        )
    ).scalars():
        aide = AideSlot.from_schedule(sched)
        if local_now.weekday() in aide.days:
            slot = min(slot, aide.start) if slot else aide.start
    return Today(
        tz=tz,
        now=now,
        events=[_door(r) for r in rows],
        last_human=_door(last) if last else None,
        aide_slot_start=slot,
    )


def _door(r: Any) -> DoorEvent:
    at = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=UTC)
    return DoorEvent(r.id, r.device_id, r.type, r.sub_type, at)


async def load_anchors(session: AsyncSession, household_id: int, day: date) -> list[AnchorView]:
    rows = (
        await session.execute(
            select(Anchor).where(Anchor.household_id == household_id, Anchor.day_group == day_group(day))
        )
    ).scalars()
    return [AnchorView.of(a) for a in rows]


async def watched_devices(session: AsyncSession, household_id: int, now: datetime) -> list[Device]:
    """Cameras that normally see the door: any device with human motion in the lookback."""
    ids = (
        await session.execute(
            select(Event.device_id)
            .where(
                Event.household_id == household_id,
                Event.type == "motion_detected",
                Event.started_at >= now - LOOKBACK,
                Event.device_id.is_not(None),
            )
            .distinct()
        )
    ).scalars().all()
    if not ids:
        return []
    return list((await session.execute(select(Device).where(Device.id.in_(ids)))).scalars())


async def device_problems(
    session: AsyncSession, household_id: int, ring: RingClient | None, now: datetime, stale_after: timedelta
) -> list[dict]:
    """Refresh each watched camera's status, then list the ones that look unhealthy.

    A successful status call that says online counts as a heartbeat now. (Ring's
    reported_at is a fresh heartbeat on the real API, but ring-sandbox sets it to the last
    event's time, so a quiet night would otherwise look like a dead camera.)
    """
    problems = []
    for device in await watched_devices(session, household_id, now):
        if ring is not None and ring.tokens.current():
            try:
                status = await ring.get_status(device.id)
                if status.online is not None:
                    device.online = status.online
                if status.online:
                    device.last_heartbeat_at = max(_aware(device.last_heartbeat_at) or now, now)
            except RingError as exc:
                if exc.status_code == 404:
                    continue  # no longer on the Ring account: nothing to watch
                log.warning("status check for %s failed: %s", device.id, exc)
        heartbeat = _aware(device.last_heartbeat_at)
        if device.online is False:
            problems.append({"device_id": device.id, "name": device.name, "reason": "offline"})
        elif heartbeat is None or now - heartbeat > stale_after:
            age = None if heartbeat is None else round((now - heartbeat).total_seconds() / 3600, 1)
            problems.append({"device_id": device.id, "name": device.name, "reason": "stale_heartbeat", "hours": age})
    return problems


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


async def _cases_today(session: AsyncSession, household_id: int, day: date) -> list[Case]:
    return list(
        (
            await session.execute(
                select(Case).where(Case.household_id == household_id, Case.local_date == day.isoformat())
            )
        ).scalars()
    )


async def _open_case(session: AsyncSession, household_id: int, kind: str) -> Case | None:
    return (
        await session.execute(
            select(Case)
            .where(Case.household_id == household_id, Case.kind == kind, Case.state.in_(("open", "acked")))
            .order_by(Case.opened_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _record(case: Case, now: datetime) -> None:
    last = case.score_history[-1] if case.score_history else None
    if not last or last["score"] != case.score or last["level"] != case.level:
        case.score_history = [*case.score_history, {"at": now.isoformat(), "score": case.score, "level": case.level}]


def _resolve(case: Case, now: datetime, resolution: str) -> None:
    case.state, case.resolved_at, case.resolution = "resolved", now, resolution
    _record(case, now)
    log.info("case %s (%s) resolved: %s", case.id, case.kind, resolution)


async def tick(
    session: AsyncSession,
    household: Household,
    *,
    now: datetime | None = None,
    ring: RingClient | None = None,
    stale_after: timedelta = timedelta(hours=2),
) -> dict:
    """One Silence Watch pass for a household. Returns what changed (for logs and tests)."""
    now = now or datetime.now(tz=UTC)
    today = await load_today(session, household, now)
    result: dict[str, Any] = {"opened": None, "resolved": [], "case": None}

    # Activity at the door ends a concern; a healthy camera ends a device issue.
    concern = await _open_case(session, household.id, "concern")
    if concern and any(e.is_human or e.type == "button_press" for e in today.events if e.at > _aware(concern.opened_at)):
        _resolve(concern, now, "activity")
        result["resolved"].append(concern.id)
        concern = None
    problems: list[dict] | None = None

    async def current_problems() -> list[dict]:
        nonlocal problems
        if problems is None:
            problems = await device_problems(session, household.id, ring, now, stale_after)
        return problems

    device_case = await _open_case(session, household.id, "device_issue")
    if device_case and not await current_problems():
        _resolve(device_case, now, "device_back")
        result["resolved"].append(device_case.id)
        device_case = None

    anchors = await load_anchors(session, household.id, today.day)
    todays = await _cases_today(session, household.id, today.day)
    back = [_aware(c.resolved_at) for c in todays if c.kind == "device_issue" and c.resolution == "device_back"]
    today.blind_until = max(back) if back else None
    handled = {s["key"] for c in todays if c.state == "resolved" for s in c.signals}
    signals = [s for s in concern_signals(anchors, today, household.rhythm_stats) if s.key not in handled]

    if signals and not concern and await current_problems():
        # Silence while a camera is down says nothing about the person. Signals that turn
        # up during the outage join the device case, so they don't alert once it's back.
        if device_case is not None:
            known = {x.get("key") for x in device_case.signals}
            device_case.signals = [*device_case.signals, *(x.as_dict() for x in signals if x.key not in known)]
        else:
            device_case = _new_case(household.id, "device_issue", today.day, now, [s.as_dict() for s in signals])
            device_case.signals = [
                *device_case.signals,
                *({"key": f"device:{p['device_id']}:{today.day}", "kind": "device", "points": 0, **p} for p in problems),
            ]
            session.add(device_case)
            await session.flush()
            result["opened"] = device_case.id
            log.info("device issue case %s opened: %s", device_case.id, problems)
        result["case"] = device_case
        return result

    if not concern and not signals:
        return result

    merged = {s["key"]: s for s in (concern.signals if concern else [])}
    merged.update({s.key: s.as_dict() for s in signals})
    score = score_of(list(merged.values()))

    if concern is None:
        level = next_level(None, score)
        if level is None:
            return result
        concern = _new_case(household.id, "concern", today.day, now, list(merged.values()))
        concern.score, concern.level = score, level
        session.add(concern)
        await session.flush()
        result["opened"] = concern.id
        log.info("concern case %s opened: score %s, level %s", concern.id, score, level)
    else:
        concern.signals, concern.score = list(merged.values()), score
        level = next_level(concern.level, score)
        timeout = ACK_TIMEOUT.get(concern.level)
        if concern.state == "open" and timeout and now - _aware(concern.level_since) >= timeout:
            level = max(level, min(MAX_LEVEL, concern.level + 1))  # unanswered: move up one
        if level != concern.level:
            log.info("case %s level %s -> %s", concern.id, concern.level, level)
            concern.level, concern.level_since = level, now
    _record(concern, now)
    result["case"] = concern
    return result


def _new_case(household_id: int, kind: str, day: date, now: datetime, signals: list[dict]) -> Case:
    return Case(
        household_id=household_id,
        kind=kind,
        state="open",
        score=0,
        level=0,
        local_date=day.isoformat(),
        signals=signals,
        score_history=[{"at": now.isoformat(), "score": 0, "level": 0}] if kind == "device_issue" else [],
        acks=[],
        agent_decision=dict(PLACEHOLDER_DECISION),
        opened_at=now,
        level_since=now,
    )


# ---------------------------------------------------------------------- acks

ACK_RESPONSES = ("fine", "calling", "escalate")


async def acknowledge(
    session: AsyncSession,
    case: Case,
    response: str,
    *,
    reason: str | None = None,
    recurring: bool = False,
    member_id: int | None = None,
    now: datetime | None = None,
) -> Case:
    """A person answers a case: "fine" resolves it, "calling" stops the escalation timer,
    "escalate" moves it up a level. A reason is kept on the missed anchors, for that day
    or (``recurring``) that weekday, and takes 40 points off when it explains a miss."""
    if response not in ACK_RESPONSES:
        raise ValueError(f"response must be one of {ACK_RESPONSES}")
    if case.state == "resolved":
        raise ValueError("case is already resolved")
    now = now or datetime.now(tz=UTC)
    case.acks = [*case.acks, {"at": now.isoformat(), "response": response, "reason": reason, "member_id": member_id}]
    if response == "fine":
        _resolve(case, now, "confirmed_fine")
    elif response == "calling":
        case.state = "acked"
    elif response == "escalate":
        case.level, case.level_since = min(MAX_LEVEL, case.level + 1), now
        _record(case, now)

    if reason:
        day = date.fromisoformat(case.local_date)
        names = {s["anchor"] for s in case.signals if s.get("kind") == "missed_anchor"}
        household = await session.get(Household, case.household_id)
        for a in (
            await session.execute(
                select(Anchor).where(Anchor.household_id == household.id, Anchor.name.in_(names))
            )
        ).scalars():
            entry = {"reason": reason, "case_id": case.id, "date": day.isoformat()}
            if recurring:
                entry["weekday"] = day.weekday()
            overrides = dict(a.overrides or {})
            overrides["explanations"] = [*overrides.get("explanations", []), entry]
            a.overrides = overrides
    return case

