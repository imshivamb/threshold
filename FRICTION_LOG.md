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
- **Steps:** Opened a live view on "Playground Device" for about 30 s, twice. Polled history every 15 s and compared each entry's `start` / `end` with the poll times.
- **Expected:** The `on_demand` entry is listed on the first poll after the live view starts.
- **Actual:** The entry is missing from every poll while the view is open, and appears on the first poll after `end`. Example: `start` 07:46:43.9, `end` 07:47:15.0 UTC; missing at the 07:46:57 and 07:47:12 polls, listed at 07:47:27. A 31 s live view therefore shows up 31–46 s after it began.
- **Severity:** Low — fine for an access log, but history polling can't show "someone is looking right now".
- **Workaround:** Present live views in the "who looked" log as recent, not live; the stored `start` is still exact.
- **Suggestion:** List the entry (without `end`) when the session opens, or send a live-view-started event the Playground can deliver.
