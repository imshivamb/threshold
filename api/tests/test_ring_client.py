
import httpx
import pytest

from ring import RingClient, RingTokenExpired, TokenStore


async def front_door(ring) -> str:
    return next(d.id for d in await ring.list_devices() if d.name == "Front Door")


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
