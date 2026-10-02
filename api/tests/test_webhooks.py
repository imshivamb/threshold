import httpx
import pytest
from fastapi import FastAPI
from ring_sandbox import webhooks as sandbox_webhooks
from sqlalchemy import func, select

import webhooks
from ingest import ensure_household
from main import AppContext
from models import Device, Event
from ring import RingClient, TokenStore
from tests.conftest import SIGNING_KEY


@pytest.fixture
async def api(settings, sessions):
    async with sessions() as session:
        household = await ensure_household(session, settings)
        await session.commit()
    app = FastAPI()
    app.include_router(webhooks.router)
    app.state.ctx = AppContext(
        settings=settings,
        tokens=TokenStore(mode="emulator"),
        ring=RingClient("http://unused", TokenStore(mode="emulator")),
        household_id=household.id,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        yield client


def delivery(event_type="motion_detected", sub_type="human", request_id=None, device_id="ava1.ring.device.FRONT"):
    """A body + signature exactly as ring-sandbox (and Ring) send them."""
    payload = sandbox_webhooks.build_event(
        event_type=event_type, device_id=device_id, sub_type=sub_type, request_id=request_id
    )
    body = sandbox_webhooks.encode(payload)
    return body, sandbox_webhooks.sign(SIGNING_KEY, body)


async def post(api, body, signature):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Signature"] = signature
    return await api.post("/webhooks/ring", content=body, headers=headers)


async def count_events(sessions) -> int:
    async with sessions() as session:
        return (await session.execute(select(func.count(Event.id)))).scalar_one()


async def test_duplicate_delivery_is_dropped(api, sessions):
    body, sig = delivery(request_id="req-1")
    assert (await post(api, body, sig)).json()["status"] == "stored"
    r = await post(api, body, sig)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert await count_events(sessions) == 1


async def test_bad_signature_is_rejected(api, sessions):
    body, _ = delivery()
    forged = sandbox_webhooks.sign("wrong-key", body)
    assert (await post(api, body, forged)).status_code == 401
    assert (await post(api, body, None)).status_code == 401
    assert await count_events(sessions) == 0


async def test_signature_covers_raw_bytes_not_parsed_json(api, sessions):
    body, sig = delivery()
    reformatted = body.replace(b",", b", ")  # same JSON, different bytes
    assert (await post(api, reformatted, sig)).status_code == 401
    assert await count_events(sessions) == 0


async def test_bare_hex_signature_is_accepted(api):
    body, sig = delivery()
    assert (await post(api, body, sig.removeprefix("sha256="))).status_code == 200


async def test_device_offline_and_online_update_device(api, sessions):
    for kind in ("device_offline", "device_online", "device_offline"):
        body, sig = delivery(event_type=kind, sub_type=None)
        assert (await post(api, body, sig)).status_code == 200
    async with sessions() as session:
        assert (await session.get(Device, "ava1.ring.device.FRONT")).online is False
