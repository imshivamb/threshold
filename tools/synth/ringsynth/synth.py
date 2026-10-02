"""Routine synthesizer: turn a persona into weeks of back-dated Ring event history.

A persona is a set of daily routines. Each routine has a local time, a jitter, a skip
probability, optional weekday/weekend overrides and a list of steps (or a built-in
ring-sandbox scenario). ``plan`` is pure and seeded, so the same persona, seed and end
date always give the same history; ``apply`` writes the plan into a running emulator.

Anomalies mark a day as unusual so absence detection can be tested:
  - ``silent_morning``: nothing happens before noon
  - ``offline``: the camera is offline between two local times, so nothing is recorded
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml
from ring_sandbox.scenarios import BUILTIN

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_SETS = {
    "daily": set(range(7)),
    "weekdays": set(range(5)),
    "weekends": {5, 6},
}


@dataclass(frozen=True)
class Step:
    type: str
    sub_type: str | None = None
    device: str | None = None
    # Offset from the routine's start: fixed seconds, or a uniform [lo, hi] range in minutes.
    after_s: float = 0.0
    after_min: tuple[float, float] | None = None


@dataclass(frozen=True)
class Profile:
    at: time
    jitter_min: float
    skip: float


@dataclass
class Routine:
    name: str
    days: set[int]
    default: Profile
    weekend: Profile | None
    steps: list[Step]
    description: str = ""

    def profile(self, day: date) -> Profile:
        return self.weekend if self.weekend and day.weekday() >= 5 else self.default


@dataclass(frozen=True)
class Anomaly:
    day: date
    kind: str  # silent_morning / offline
    start: time = time(0, 0)
    end: time = time(12, 0)

    def suppresses(self, local: datetime) -> bool:
        return local.date() == self.day and self.start <= local.time() < self.end


@dataclass
class Persona:
    name: str
    timezone: ZoneInfo
    device: str
    routines: list[Routine]
    description: str = ""


@dataclass(frozen=True, order=True)
class PlannedEvent:
    at: datetime  # UTC
    type: str
    sub_type: str | None = field(default=None, compare=False)
    device: str | None = field(default=None, compare=False)
    routine: str = field(default="", compare=False)


# ------------------------------------------------------------------ loading


def _parse_time(value: str) -> time:
    hh, mm = str(value).split(":")
    return time(int(hh), int(mm))


def _parse_days(value: Any) -> set[int]:
    if isinstance(value, str):
        if value in DAY_SETS:
            return set(DAY_SETS[value])
        value = [v.strip() for v in value.split(",")]
    try:
        return {WEEKDAYS.index(str(v).lower()[:3]) for v in value}
    except ValueError as exc:
        raise ValueError(f"days must be daily/weekdays/weekends or a list of {WEEKDAYS}") from exc


def _parse_profile(raw: dict, base: Profile | None = None) -> Profile:
    return Profile(
        at=_parse_time(raw["at"]) if "at" in raw else base.at,  # type: ignore[union-attr]
        jitter_min=float(raw.get("jitter_min", base.jitter_min if base else 0)),
        skip=float(raw.get("skip", base.skip if base else 0)),
    )


def _parse_steps(raw: dict, persona_device: str) -> list[Step]:
    if "scenario" in raw:
        scenario = BUILTIN.get(raw["scenario"])
        if scenario is None:
            raise ValueError(f"unknown ring-sandbox scenario {raw['scenario']!r}; built-ins: {sorted(BUILTIN)}")
        return [Step(s.type, s.sub_type, s.device or persona_device, after_s=s.offset_s) for s in scenario.steps]
    steps = []
    for s in raw.get("steps") or []:
        rng = s.get("after_min")
        if rng is not None and not isinstance(rng, (list, tuple)):
            rng = [rng, rng]
        steps.append(
            Step(
                type=s.get("type", "motion_detected"),
                sub_type=s.get("sub_type"),
                device=s.get("device") or persona_device,
                after_s=float(s.get("after_s", 0)),
                after_min=(float(rng[0]), float(rng[1])) if rng else None,
            )
        )
    if not steps:
        raise ValueError(f"routine {raw.get('name')!r} needs 'steps' or 'scenario'")
    return steps


def parse_persona(raw: dict) -> Persona:
    device = raw.get("device", "Front Door")
    routines = []
    for r in raw.get("routines") or []:
        default = _parse_profile(r)
        routines.append(
            Routine(
                name=r["name"],
                description=r.get("description", ""),
                days=_parse_days(r.get("days", "daily")),
                default=default,
                weekend=_parse_profile(r["weekend"], default) if r.get("weekend") else None,
                steps=_parse_steps(r, device),
            )
        )
    if not routines:
        raise ValueError("persona has no routines")
    return Persona(
        name=raw.get("name", "persona"),
        description=raw.get("description", ""),
        timezone=ZoneInfo(raw.get("timezone", "UTC")),
        device=device,
        routines=routines,
    )


def load_persona(path: str | Path) -> Persona:
    return parse_persona(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def parse_anomaly(spec: str, today: date) -> Anomaly:
    """``silent_morning@-2`` or ``offline@-5:07:00-10:00`` (day offset from today)."""
    kind, _, rest = spec.partition("@")
    offset, _, window = rest.partition(":")
    day = today + timedelta(days=int(offset or -1))
    if kind == "silent_morning":
        return Anomaly(day, kind)
    if kind == "offline":
        start, _, end = window.partition("-")
        if not start or not end:
            raise ValueError("offline anomaly needs a window, e.g. offline@-5:07:00-10:00")
        return Anomaly(day, kind, _parse_time(start), _parse_time(end))
    raise ValueError(f"unknown anomaly {kind!r} (silent_morning, offline)")


# ------------------------------------------------------------------ planning


def plan(
    persona: Persona,
    days: int,
    *,
    seed: int = 0,
    end: date | None = None,
    anomalies: list[Anomaly] = (),  # type: ignore[assignment]
    now: datetime | None = None,
) -> list[PlannedEvent]:
    """Events for the ``days`` local days ending the day before ``end`` (default: today)."""
    tz = persona.timezone
    now = now or datetime.now(tz=UTC)
    end = end or now.astimezone(tz).date()
    rng = random.Random(seed)
    out: list[PlannedEvent] = []
    for i in range(days, 0, -1):
        day = end - timedelta(days=i)
        for routine in persona.routines:
            # Draw every random number even for skipped days, so one routine's
            # settings never shift another routine's randomness.
            prof = routine.profile(day)
            skip_roll = rng.random()
            jitter = max(-3, min(3, rng.gauss(0, 1))) * prof.jitter_min
            offsets = [s.after_s + (rng.uniform(*s.after_min) * 60 if s.after_min else 0) for s in routine.steps]
            if day.weekday() not in routine.days or skip_roll < prof.skip:
                continue
            start = datetime.combine(day, prof.at, tzinfo=tz) + timedelta(minutes=jitter)
            for step, off in zip(routine.steps, offsets, strict=True):
                local = start + timedelta(seconds=off)
                if any(a.suppresses(local) for a in anomalies):
                    continue
                at = local.astimezone(UTC)
                if at >= now:
                    continue
                out.append(PlannedEvent(at, step.type, step.sub_type, step.device, routine.name))
    for a in anomalies:
        if a.kind == "offline":
            for t, kind in ((a.start, "device_offline"), (a.end, "device_online")):
                at = datetime.combine(a.day, t, tzinfo=tz).astimezone(UTC)
                if at < now:
                    out.append(PlannedEvent(at, kind, None, persona.device, "anomaly"))
    return sorted(out)


# ------------------------------------------------------------------ applying


def apply(
    events: list[PlannedEvent],
    base_url: str = "http://127.0.0.1:8787",
    *,
    deliver: bool = False,
    client: httpx.Client | None = None,
) -> int:
    """Write planned events into a running emulator's history.

    ``deliver=False`` (default) only writes history: Ring does not send webhooks for
    events weeks in the past, so a client is expected to backfill from history.
    """
    http = client or httpx.Client(base_url=base_url, timeout=10.0)
    try:
        state = http.get("/_sandbox/state").json()
        ids = {d["name"]: d["id"] for d in state["devices"]} | {d["id"]: d["id"] for d in state["devices"]}
        for ev in events:
            if ev.device not in ids:
                raise KeyError(f"no device {ev.device!r} in the emulator; have {sorted(n for n in ids if ' ' in n)}")
            resp = http.post(
                "/_sandbox/events",
                json={
                    "device_id": ids[ev.device],
                    "type": ev.type,
                    "sub_type": ev.sub_type,
                    "at": ev.at.isoformat(),
                    "deliver": deliver,
                },
            )
            resp.raise_for_status()
    finally:
        if client is None:
            http.close()
    return len(events)
