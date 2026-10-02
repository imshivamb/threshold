import sys
from pathlib import Path

import httpx
import pytest
from ring_sandbox.emulator import create_app
from ring_sandbox.world import default_world

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings  # noqa: E402
from db import dispose_engine, init_engine  # noqa: E402
from models import Base  # noqa: E402
from ring import RingClient, TokenStore  # noqa: E402

SIGNING_KEY = "test-signing-key"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        ring_mode="emulator",
        ring_webhook_signing_key=SIGNING_KEY,
        _env_file=None,
    )


@pytest.fixture
async def sessions(settings):
    maker = init_engine(settings.database_url)
    async with maker.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield maker
    await dispose_engine()


@pytest.fixture
def emulator():
    """An in-process ring-sandbox emulator: (asgi transport, world)."""
    world = default_world()
    return httpx.ASGITransport(app=create_app(world)), world


@pytest.fixture
async def ring(emulator):
    transport, _ = emulator
    tokens = TokenStore(mode="emulator")
    tokens.set("sandbox-token")
    client = RingClient("http://emulator", tokens, transport=transport)
    yield client
    await client.aclose()


@pytest.fixture
async def sandbox(emulator):
    """Control-plane client for the emulator's /_sandbox endpoints."""
    transport, _ = emulator
    async with httpx.AsyncClient(transport=transport, base_url="http://emulator") as http:
        yield http
