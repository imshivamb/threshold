# Threshold

Routine awareness for an older adult living alone, built on the Ring Partner API.
Threshold learns a home's daily rhythm from Ring events and tells a faraway family when
*nothing* happened, while telling "no activity" apart from "camera offline".

> Work in progress for the Amazon Developer Hackathon (Ring track).

## Run it

```bash
cp .env.example .env      # optional; every value has a default
docker compose up --build
```

| Service | URL | What it is |
| --- | --- | --- |
| web | http://localhost:3000 | Next.js app (status page for now) |
| api | http://localhost:8000/docs | FastAPI backend; owns every Ring call |
| ring-sandbox | http://localhost:8787 | Ring API emulator: signed webhooks, scenarios |
| db | localhost:5432 | Postgres 16 (`threshold` / `threshold`) |

### Emulator mode (default)

The api registers its webhook with the emulator on startup, the same as
`ring-sandbox webhook http://api:8000/webhooks/ring --url http://localhost:8787 --key <RING_WEBHOOK_SIGNING_KEY>`.
Then play a scenario:

```bash
docker compose exec ring-sandbox ring-sandbox play delivery --speed 5
curl localhost:8000/events      # or open http://localhost:3000
```

(Or `pip install "ring-sandbox[server]"` and run `ring-sandbox play delivery --speed 5` from your machine.)

### Three weeks of routine in one command

`ringsynth` writes weeks of a persona's daily routine into the emulator's event history,
and Threshold backfills it through the Ring history API, then learns the rhythm:

```bash
docker compose exec ring-sandbox sh -c 'ringsynth --persona $PERSONAS/aging_in_place.yml --days 28'
curl -X POST localhost:8000/ring/backfill
curl localhost:8000/households/1/rhythm
curl localhost:8000/households/1/today
```

Use `--days 28`: anchors alert only after 7 observed days in their day group, and four
weeks are needed for 7 weekend days.

Add `--anomaly silent_morning@-1` (nothing before noon yesterday),
`--anomaly offline@-2:07:00-10:00` or `--anomaly delay@-1:aide_visit+90` to test absence detection. Use `--dry-run` to print the
plan. Personas live in [`tools/synth/personas`](tools/synth/personas).

### Playground mode (real Ring API)

Set `RING_MODE=playground` in `.env`, `docker compose up`, then paste a 30-minute token from
the [Ring Developer Playground](https://developer.amazon.com/docs/ring/get-started.html):

```bash
curl -X POST localhost:8000/settings/playground-token \
  -H 'content-type: application/json' -d '{"token":"<playground token>"}'
```

The Playground has no webhooks, so the api polls each device's event history every 15 s.
Open a live view in the Playground and an `on_demand` event appears within 15 s. When the
token expires, the api returns 401 with a prompt to paste a new one.

## How Ring data flows in

- `api/ring/client.py` — httpx client for `list_devices`, `get_status`, `get_history`
  (paginated, epoch ms → UTC) and `start_whep`. Retries 429 using `Retry-After`; a 401 marks
  the token expired.
- `api/webhooks.py` — `POST /webhooks/ring` checks the HMAC-SHA256 `X-Signature` over the raw
  bytes, drops duplicate `meta.request_id`s and returns 200 quickly.
- `api/poller.py` — the Playground history poller, which also uses the status call as a heartbeat.
- `events.ring_request_id` is unique, so redeliveries and overlapping polls are dropped. The
  same moment arriving from both sources (a webhook, then a history backfill) is also
  stored once: the dedupe checks device, type and timestamp.
- `POST /ring/backfill` reads up to 28 days of Ring history on connect, so a new install has
  a rhythm to learn from on day one.

## How the rhythm is learned

- `api/visits.py` groups door events into bursts, then visits: deliveries (a parcel, or a
  vehicle plus a ring), and rung-in visits that last until the person is seen leaving.
  Other human motion is the resident stepping in or out.
- `api/rhythm.py` learns five anchors per weekday / weekend group: `first_activity`,
  `aide_visit`, `delivery`, `out_and_back` and `last_activity` (a first activity after 12:00
  is treated as an outlier day). Each window is P10 − 15 min to
  P90 + 15 min; confidence is days seen ÷ days observed. An anchor may alert only with
  confidence ≥ 0.6 and at least 7 observed days.
- It relearns every night at 03:00 household time, and after every backfill.

## How silence is noticed

`api/watch.py` runs every minute (the Silence Watch):

1. Each of today's anchors is **met**, **pending** (window still open) or **missed**.
2. Signals: missed anchors (only those allowed to alert), a gap in human activity beyond
   the household's P95 gap for weekdays or weekends, the aide not arriving 30 min after their slot, and activity
   between 01:00 and 04:00 that the history never shows.
3. **Device health first.** Before scoring, each camera's status is refreshed. If one is
   offline, or its heartbeat is older than `DEVICE_STALE_HOURS` (2), the signals open a
   `device_issue` case, not a concern. Nothing that happened while a camera was down is
   judged afterwards.
4. Otherwise the **concern score** (0–100): missed first activity, 50 × confidence (up to 40),
   silence beyond the P95 gap (10 a hour, up to 30), a late aide (20), unusual night
   activity (15), and −40 when an ack reason on file explains it.
5. Score sets the **level**: 20–39 → 0 watch, 40–59 → 1 gentle check, 60–79 → 2 primary
   caregiver, 80+ → 3 whole circle. A concern always passes through level 1 (the
   homeowner's own check) first unless the score is 80+. An unanswered level 1 climbs after
   15 min, level 2 after 30 min. Any human activity at the door resolves the concern.

`GET /households/{id}/today` shows today's anchors and open cases; `GET /cases/{id}` shows
signals, score history and acks; `POST /cases/{id}/ack` takes `fine`, `calling` or
`escalate`, with an optional reason (`recurring: true` applies it to that weekday).

## Tests

```bash
cd api
uv venv && uv pip install -e ".[dev]" -e ../tools/synth
.venv/bin/pytest                 # api
.venv/bin/pytest ../tools/synth  # synthesizer
```

The main tests are persona-day scenarios (`api/tests/test_watch_e2e.py`): four weeks of
synthesized history plus one scenario day (normal, silent morning, camera offline, late aide,
…) run through an in-process ring-sandbox emulator, history backfill and the Silence Watch on
a fake clock, asserting the case a person would see. They run on SQLite by default. To run them on Postgres instead, point `TEST_DATABASE_URL`
at a throwaway database, e.g.
`TEST_DATABASE_URL=postgresql+asyncpg://threshold:threshold@localhost:5432/threshold_test`.

## License

MIT
