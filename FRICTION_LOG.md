# Friction log

Every snag hit while building Threshold on the Ring Partner API, Developer Playground,
ring-sandbox, Bedrock, Strands and AgentCore. Only things that actually happened.

Format: task · steps · expected vs actual · severity · workaround · suggestion.

---

## 1. Playground simulated events only show up as `on_demand` history

- **Date:** 2026-10-02
- **Task:** Read Package / Vehicle / Motion events simulated in the Developer Playground through the API.
- **Steps:** Generate a Playground token → trigger simulated Package, Vehicle and Motion events → `GET /v1/history/devices/{id}/events`.
- **Expected:** History entries typed as motion (with `cv_detections` for package / vehicle / human).
- **Actual:** Only `on_demand` (live-view) entries appear, with empty `cv_detections`.
  Re-checked by polling history every 3 s: a simulated Motion triggered at 07:58:14 UTC appeared
  at 07:58:56 as `on_demand` (`start` 07:58:14, `end` 07:58:53, no `sub_type`, `cv_detections: []`).
  The simulation plays inside a live-view session and only that session is logged.
- **Severity:** High — a Playground-only app cannot see a single motion event.
- **Workaround:** Motion events come from the ring-sandbox emulator; Playground `on_demand` entries feed the "who looked" access log instead.
- **Suggestion:** Have Playground simulations write real motion / ding history entries with `cv_detections`, or document that they don't.

## 2. Snapshot download fails with "type is required"

- **Date:** 2026-10-02
- **Task:** Download a snapshot with `POST /v1/devices/{id}/media/image/download`.
- **Steps:** <!-- TODO(shivam): paste the exact request you sent -->
- **Expected:** A 303 to a pre-signed image URL.
- **Actual:** 400 with "type is required".
- **Severity:** Medium — blocks snapshot captions until the body shape is known.
- **Workaround:** Copy the request body from the Playground API explorer. <!-- TODO(shivam): paste it here on Oct 3 -->
- **Suggestion:** Show a full example request body in the API reference, including `type` (`at_timestamp` / `latest_in_range`).

## 3. Live views only reach event history after they end

- **Date:** 2026-10-02
- **Task:** Detect a Playground live view by polling `GET /v1/history/devices/{id}/events` every 15 s.
- **Steps:** Opened live views on "Playground Device" several times, polling history every 15 s (Threshold) and every 3 s (a separate tracker), and compared each entry's `start` / `end` with the poll times.
- **Expected:** The `on_demand` entry is listed on the first poll after the live view starts.
- **Actual:** The entry is missing from every poll while the view is open, and appears on the first poll after `end`. Example: `start` 07:46:43.9, `end` 07:47:15.0 UTC; missing at the 07:46:57 and 07:47:12 polls, listed at 07:47:27. With 3 s polling: started 07:56:27, closed by the user about 07:56:50, `end` recorded as 07:57:07 (~15 s after closing), listed at 07:57:10. So an entry appears ~15–20 s after the viewer closes the stream, and never while it is open.
- **Severity:** Low — fine for an access log, but history polling can't show "someone is looking right now".
- **Workaround:** Present live views in the "who looked" log as recent, not live; the stored `start` is still exact.
- **Suggestion:** List the entry (without `end`) when the session opens, or send a live-view-started event the Playground can deliver.

## 4. ring-sandbox: device status `reported_at` is the last event's time, not a heartbeat

- **Date:** 2026-10-02
- **Task:** Tell "camera offline" apart from "nobody moved" using the device status heartbeat.
- **Steps:** Write a quiet night into the emulator, then call `GET /v1/devices/{id}/status` the next morning.
- **Expected:** As on the real Playground, `reported_at` is recent while the device is online (Playground: `reported_at` 07:42:22 UTC when its last event was 06:24:46).
- **Actual:** The emulator sets `reported_at` to the timestamp of the device's most recent event, so after any quiet stretch an online camera looks hours stale.
- **Severity:** Medium — a heartbeat check built against the emulator flags every silent morning as a device problem.
- **Workaround:** Treat a successful status call with `online: true` as a heartbeat at call time; judge staleness only when Ring can't be reached.
- **Suggestion:** Advance `reported_at` on each status read (or on a timer) while the device is online. Candidate for our ring-sandbox PR.

## 5. ring-sandbox: history keeps only motion and dings; device ids change on restart

- **Date:** 2026-10-02
- **Task:** Backfill past days from the emulator's event history, including a past camera outage.
- **Steps:** Inject `device_offline` / `device_online` and contact-sensor events with past timestamps, then read `GET /v1/history/devices/{id}/events`; restart the emulator container.
- **Expected:** Some record of past outages; stable device ids across restarts.
- **Actual:** Only `motion` and `ding` reach history (device state and sensor events change state but leave no trace), and every restart creates new random device ids, so the old ids return 404.
- **Severity:** Low — outages are only knowable live (via webhooks), and stale ids look like dead cameras unless 404 is handled.
- **Workaround:** Treat a 404 from the status call as "no longer on the account" and stop watching that device.
- **Suggestion:** Allow fixed device ids (e.g. `--seed`) and document which event types land in history.
