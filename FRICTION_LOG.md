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
