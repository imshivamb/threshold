"""ringsynth --persona personas/aging_in_place.yml --days 21 [--url http://127.0.0.1:8787]"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime

from ringsynth.synth import apply, load_persona, parse_anomaly, plan


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ringsynth", description="Write weeks of persona history into ring-sandbox.")
    p.add_argument("--persona", required=True, help="persona YAML file")
    p.add_argument("--days", type=int, default=21, help="number of past days to generate (ends yesterday)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--url", default="http://127.0.0.1:8787", help="ring-sandbox emulator URL")
    p.add_argument(
        "--anomaly",
        action="append",
        default=[],
        metavar="SPEC",
        help="silent_morning@-2, offline@-5:07:00-10:00 or delay@-1:aide_visit+90 "
        "(day offset from today; repeatable)",
    )
    p.add_argument("--deliver", action="store_true", help="also send webhooks (default: history only)")
    p.add_argument("--dry-run", action="store_true", help="print the plan without writing it")
    args = p.parse_args(argv)

    persona = load_persona(args.persona)
    today = datetime.now(tz=UTC).astimezone(persona.timezone).date()
    anomalies = [parse_anomaly(a, today) for a in args.anomaly]
    events = plan(persona, args.days, seed=args.seed, anomalies=anomalies)

    if args.dry_run:
        for ev in events:
            local = ev.at.astimezone(persona.timezone)
            print(f"{local:%a %Y-%m-%d %H:%M}  {ev.routine:<18} {ev.type:<16} {ev.sub_type or ''}")
    else:
        apply(events, args.url, deliver=args.deliver)
    per_routine = Counter(ev.routine for ev in events)
    print(
        f"{'planned' if args.dry_run else 'wrote'} {len(events)} events over {args.days} days for "
        f"{persona.name}: " + ", ".join(f"{k} {v}" for k, v in sorted(per_routine.items()))
    )


if __name__ == "__main__":
    main()
