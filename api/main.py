"""Threshold API entrypoint."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import routes
import webhooks
from config import Settings, get_settings
from db import dispose_engine, init_engine, sessionmaker
from ingest import backfill, ensure_household, sync_devices
from models import Device, Household
from poller import HistoryPoller
from ring import RingClient, RingError, TokenStore
from routine import LOOKBACK_DAYS, rebuild

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("threshold")


@dataclass
class AppContext:
    settings: Settings
    tokens: TokenStore
    ring: RingClient
    household_id: int
    poller: HistoryPoller | None = None

    async def sync_devices(self) -> list[Device]:
        found = await self.ring.list_devices()
        async with sessionmaker()() as session:
            devices = await sync_devices(session, self.household_id, found)
            await session.commit()
        if self.poller:
            self.poller.device_ids = [d.id for d in devices]
        log.info("synced %d Ring device(s): %s", len(devices), ", ".join(d.name for d in devices))
        return devices

    @property
    def history_source(self) -> str:
        return "playground" if self.settings.ring_mode == "playground" else "emulator"

    async def backfill(self, days: int = LOOKBACK_DAYS) -> dict:
        """Pull past Ring history for every device, then relearn visits and anchors."""
        devices = await self.sync_devices()
        since = datetime.now(tz=UTC) - timedelta(days=days)
        async with sessionmaker()() as session:
            written = await backfill(
                session, self.ring, self.household_id, [d.id for d in devices], since=since, source=self.history_source
            )
            await session.commit()
        log.info("backfilled %d event(s) from %d day(s) of Ring history", sum(written.values()), days)
        return {"written": sum(written.values()), "rhythm": await self.relearn()}

    async def relearn(self) -> dict:
        async with sessionmaker()() as session:
            household = await session.get(Household, self.household_id)
            result = await rebuild(session, household)
            await session.commit()
        log.info("rhythm relearned: %s", result)
        return result


async def register_emulator_webhook(settings: Settings) -> None:
    """Same as `ring-sandbox webhook <url> --key <key>`, but idempotent across restarts."""
    async with httpx.AsyncClient(base_url=settings.ring_url, timeout=10) as http:
        state = (await http.get("/_sandbox/state")).json()
        if settings.public_webhook_url in (state.get("webhooks") or []):
            return
        await http.post(
            "/_sandbox/webhooks",
            json={"url": settings.public_webhook_url, "signing_key": settings.ring_webhook_signing_key},
        )
        log.info("registered emulator webhook -> %s", settings.public_webhook_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_engine(settings.database_url)
    async with sessionmaker()() as session:
        household = await ensure_household(session, settings)
        await session.commit()

    tokens = TokenStore(mode=settings.ring_mode)
    if settings.ring_mode == "emulator":
        tokens.set(settings.ring_emulator_token)
    elif settings.ring_playground_token:
        tokens.set(settings.ring_playground_token)

    ring = RingClient(settings.ring_url, tokens)
    ctx = AppContext(settings=settings, tokens=tokens, ring=ring, household_id=household.id)
    app.state.ctx = ctx
    polling = settings.ring_mode == "playground" or settings.poll_in_emulator_mode
    if polling:
        ctx.poller = HistoryPoller(ring, household.id, "playground" if settings.ring_mode == "playground" else "emulator")

    if tokens.current():
        try:
            if settings.ring_mode == "playground":
                await ctx.backfill()
            else:
                await ctx.sync_devices()
        except (RingError, httpx.HTTPError) as exc:
            log.warning("initial Ring sync failed: %s", exc)
    else:
        log.warning("no Playground token yet — POST /settings/playground-token to connect Ring")

    if settings.ring_mode == "emulator" and settings.register_emulator_webhook:
        try:
            await register_emulator_webhook(settings)
        except httpx.HTTPError as exc:
            log.warning("could not register emulator webhook: %s", exc)

    scheduler = AsyncIOScheduler()
    if polling:

        async def poll_job() -> None:
            if not tokens.current():
                return
            try:
                if not ctx.poller.device_ids:
                    await ctx.sync_devices()
                await ctx.poller.poll_once()
            except Exception:
                log.exception("poll cycle failed")

        scheduler.add_job(
            poll_job, "interval", seconds=settings.poll_interval_seconds, max_instances=1, coalesce=True
        )
    async def relearn_job() -> None:
        try:
            await ctx.relearn()
        except Exception:
            log.exception("nightly relearn failed")

    # Relearn the rhythm every night once the day is complete.
    scheduler.add_job(relearn_job, "cron", hour=3, minute=0, timezone=household.timezone)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await ring.aclose()
        await dispose_engine()


app = FastAPI(title="Threshold API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"], allow_methods=["*"], allow_headers=["*"])
app.include_router(webhooks.router)
app.include_router(routes.router)
