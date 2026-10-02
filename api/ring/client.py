"""Async client for the Ring Partner API.

One interface over both sources:
  - RING_MODE=playground -> https://api.amazonvision.com with a 30-minute Playground token
  - RING_MODE=emulator   -> a ring-sandbox emulator (any non-empty token)

All Ring calls are server-to-server; browsers are blocked by CORS.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from ring.tokens import TokenStore

TOKEN_EXPIRED_MESSAGE = (
    "Ring token expired or invalid — paste a new Playground token "
    "(POST /settings/playground-token)."
)


class RingError(Exception):
    def __init__(self, status_code: int, message: str, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message)


class RingTokenExpired(RingError):
    def __init__(self, body: str = ""):
        super().__init__(401, TOKEN_EXPIRED_MESSAGE, body)


class RingRateLimited(RingError):
    def __init__(self, retry_after: float, body: str = ""):
        self.retry_after = retry_after
        super().__init__(429, f"Ring rate limit hit; retry after {retry_after:.0f}s", body)


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


@dataclass(frozen=True)
class RingDevice:
    id: str
    name: str
    kind: str
    online: bool | None
    status_reported_at: datetime | None
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class RingStatus:
    device_id: str
    online: bool | None
    reported_at: datetime | None
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class HistoryEvent:
    id: str
    device_id: str
    event_type: str  # motion / ding / on_demand
    sub_type: str | None
    started_at: datetime
    ended_at: datetime | None
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class WhepSession:
    device_id: str
    session_url: str
    sdp_answer: str


class RingClient:
    def __init__(
        self,
        base_url: str,
        tokens: TokenStore,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        max_retry_delay: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.max_retries = max_retries
        self.max_retry_delay = max_retry_delay
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> RingClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ plumbing

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        token = self.tokens.current()
        if not token:
            raise RingTokenExpired("no token set")
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        for attempt in range(self.max_retries + 1):
            resp = await self._http.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401:
                self.tokens.mark_expired()
                raise RingTokenExpired(resp.text)
            if resp.status_code == 429:
                delay = _retry_after(resp.headers.get("Retry-After"), attempt)
                if attempt < self.max_retries and delay <= self.max_retry_delay:
                    await asyncio.sleep(delay)
                    continue
                raise RingRateLimited(delay, resp.text)
            if resp.status_code >= 400:
                raise RingError(resp.status_code, f"Ring API returned HTTP {resp.status_code}", resp.text)
            return resp
        raise AssertionError("unreachable")

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return (await self._request("GET", url, params=params)).json()

    def _same_origin(self, link: str) -> str:
        """Pagination links may be relative or absolute; never follow them off-origin."""
        url = httpx.URL(self.base_url).join(link)
        base = httpx.URL(self.base_url)
        if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port):
            raise RingError(0, f"refusing off-origin pagination link {link!r}")
        return str(url)

    # ------------------------------------------------------------------ calls

    async def list_devices(self) -> list[RingDevice]:
        doc = await self._get_json("/v1/devices", {"include": "status,capabilities"})
        included = {(r["type"], r["id"]): r for r in doc.get("included", [])}
        devices = []
        for raw in doc.get("data", []):
            rels = raw.get("relationships") or {}
            status = _included(included, rels, "status")
            caps = _included(included, rels, "capabilities")
            status_attrs = (status or {}).get("attributes") or {}
            devices.append(
                RingDevice(
                    id=raw["id"],
                    name=(raw.get("attributes") or {}).get("name") or raw["id"],
                    kind=_device_kind(raw, caps),
                    online=status_attrs.get("online"),
                    status_reported_at=_parse_dt(status_attrs.get("reported_at")),
                    raw=raw,
                )
            )
        return devices

    async def get_status(self, device_id: str) -> RingStatus:
        doc = await self._get_json(f"/v1/devices/{_seg(device_id)}/status")
        attrs = (doc.get("data") or {}).get("attributes") or {}
        return RingStatus(
            device_id=device_id,
            online=attrs.get("online"),
            reported_at=_parse_dt(attrs.get("reported_at")),
            raw=doc,
        )

    async def get_history(
        self,
        device_id: str,
        *,
        since: datetime | None = None,
        event_types: list[str] | None = None,
        max_pages: int = 10,
    ) -> list[HistoryEvent]:
        """Newest-first event history, following links.next until ``since`` or ``max_pages``."""
        since_ms = int(since.timestamp() * 1000) if since else None
        url: str | None = f"/v1/history/devices/{_seg(device_id)}/events"
        params: dict[str, Any] | None = {"event_types": ",".join(event_types)} if event_types else None
        out: list[HistoryEvent] = []
        for _ in range(max_pages):
            if url is None:
                break
            doc = await self._get_json(url, params)
            params = None  # the next link already carries the query
            page = doc.get("data") or []
            for raw in page:
                ev = _history_event(raw, device_id)
                if since_ms is not None and raw["attributes"]["start"] < since_ms:
                    return out
                out.append(ev)
            nxt = (doc.get("links") or {}).get("next")
            url = self._same_origin(nxt) if nxt and page else None
        return out

    async def start_whep(
        self, device_id: str, sdp_offer: str, component_id: str | None = None
    ) -> WhepSession:
        """Open a WebRTC/WHEP live-view session; the browser supplies the SDP offer."""
        if not sdp_offer.lstrip().startswith("v="):
            raise ValueError("sdp_offer must be an SDP offer (starts with v=)")
        resp = await self._request(
            "POST",
            f"/v1/devices/{_seg(device_id)}/media/streaming/whep/sessions",
            params={"component_id": component_id} if component_id else None,
            content=sdp_offer.encode(),
            headers={"Content-Type": "application/sdp", "Accept": "application/sdp"},
        )
        location = resp.headers.get("Location")
        if resp.status_code != 201 or not location:
            raise RingError(resp.status_code, "WHEP response missing 201/Location", resp.text)
        return WhepSession(device_id=device_id, session_url=location, sdp_answer=resp.text)

    async def stop_whep(self, session_url: str) -> None:
        await self._request("DELETE", self._same_origin(session_url))


# ---------------------------------------------------------------------- helpers


def _seg(value: str) -> str:
    if not value:
        raise ValueError("id must be non-empty")
    return quote(value, safe="")


def _retry_after(header: str | None, attempt: int) -> float:
    delay: float | None = None
    if header:
        try:
            delay = float(header)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(header) - datetime.now(tz=UTC)).total_seconds()
            except (TypeError, ValueError):
                delay = None
    if delay is None or not math.isfinite(delay) or delay < 0:
        delay = float(2**attempt)
    return delay


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return ms_to_utc(int(value))
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _included(included: dict, rels: dict, name: str) -> dict | None:
    data = (rels.get(name) or {}).get("data")
    if isinstance(data, dict):
        return included.get((data.get("type"), data.get("id")))
    return None


def _device_kind(raw: dict, caps: dict | None) -> str:
    image = ((raw.get("attributes") or {}).get("image_url") or "").rsplit("/", 1)[-1]
    stem = image.split(".", 1)[0].lower()
    if stem:
        return stem
    if caps and (caps.get("attributes") or {}).get("video"):
        return "camera"
    return "unknown"


def _history_event(raw: dict, device_id: str) -> HistoryEvent:
    attrs = raw["attributes"]
    source = ((raw.get("relationships") or {}).get("source") or {}).get("data") or {}
    return HistoryEvent(
        id=raw["id"],
        device_id=source.get("id") or device_id,
        event_type=attrs["event_type"],
        sub_type=attrs.get("sub_type"),
        started_at=ms_to_utc(attrs["start"]),
        ended_at=ms_to_utc(attrs["end"]) if attrs.get("end") else None,
        raw=raw,
    )
