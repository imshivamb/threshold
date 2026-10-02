from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ring import RingClient, RingRateLimited, RingTokenExpired, TokenStore, ms_to_utc


async def front_door(ring) -> str:
    return next(d.id for d in await ring.list_devices() if d.name == "Front Door")


async def test_list_devices_reads_names_kinds_and_status(ring):
    devices = {d.name: d for d in await ring.list_devices()}
    assert {"Front Door", "Backyard"} <= devices.keys()
    assert devices["Front Door"].kind == "doorbell"
    assert devices["Front Door"].online is True


async def test_get_status(ring):
    status = await ring.get_status(await front_door(ring))
    assert status.online is True


async def test_history_paginates_and_converts_epoch_ms_to_utc(ring, sandbox):
    device_id = await front_door(ring)
    base = datetime.now(tz=UTC) - timedelta(hours=3)
    for i in range(120):  # emulator pages are 50 events
        r = await sandbox.post(
            "/_sandbox/events",
            json={
                "device_id": device_id,
                "type": "motion_detected",
                "sub_type": "human",
                "at": (base + timedelta(minutes=i)).isoformat(),
                "deliver": False,
            },
        )
        r.raise_for_status()

    events = await ring.get_history(device_id)
    assert len(events) == 120
    assert all(e.started_at.tzinfo is UTC for e in events)
    assert events[0].started_at > events[-1].started_at  # newest first
    assert events[0].event_type == "motion"

    recent = await ring.get_history(device_id, since=base + timedelta(minutes=100))
    assert len(recent) == 20


async def test_live_view_starts_whep_and_logs_on_demand(ring):
    device_id = await front_door(ring)
    session = await ring.start_whep(device_id, "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n")
    assert session.sdp_answer.startswith("v=")
    assert session.session_url.startswith(f"/v1/devices/{device_id}/")
    history = await ring.get_history(device_id)
    assert any(e.event_type == "on_demand" for e in history)
    await ring.stop_whep(session.session_url)


async def test_429_waits_for_retry_after_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": []})

    tokens = TokenStore(mode="playground")
    tokens.set("playground-token")
    async with RingClient("https://ring.test", tokens, transport=httpx.MockTransport(handler)) as ring:
        assert await ring.list_devices() == []
    assert len(calls) == 2


async def test_429_gives_up_when_retry_after_is_too_long():
    tokens = TokenStore(mode="playground")
    tokens.set("playground-token")
    transport = httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    async with RingClient("https://ring.test", tokens, transport=transport) as ring:
        with pytest.raises(RingRateLimited) as exc:
            await ring.list_devices()
    assert exc.value.retry_after == 120


async def test_401_marks_token_expired_and_asks_for_a_new_one():
    tokens = TokenStore(mode="playground")
    tokens.set("stale-playground-token")
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"errors": []}))
    async with RingClient("https://ring.test", tokens, transport=transport) as ring:
        with pytest.raises(RingTokenExpired, match="paste a new Playground token"):
            await ring.list_devices()
        assert tokens.current() is None
        with pytest.raises(RingTokenExpired):  # no further calls until a new token is pasted
            await ring.list_devices()
    tokens.set("fresh-playground-token")
    assert tokens.current() == "fresh-playground-token"


async def test_pagination_refuses_off_origin_links():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "e1", "attributes": {"event_type": "on_demand", "start": 1790925588961}}
                ],
                "links": {"next": "https://evil.test/v1/history"},
            },
        )

    tokens = TokenStore(mode="playground")
    tokens.set("playground-token")
    async with RingClient("https://ring.test", tokens, transport=httpx.MockTransport(handler)) as ring:
        with pytest.raises(Exception, match="off-origin"):
            await ring.get_history("dev")


def test_ms_to_utc():
    assert ms_to_utc(1790925588961) == datetime(2026, 10, 2, 7, 19, 48, 961000, tzinfo=UTC)
