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
- `events.ring_request_id` is unique, so both sources dedupe in the database.

## Tests

```bash
cd api
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
```

The Ring client tests run against an in-process ring-sandbox emulator.

## License

MIT
