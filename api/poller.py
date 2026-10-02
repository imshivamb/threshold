"""Playground has no webhooks, so poll each device's history (and status as a heartbeat)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from db import sessionmaker
from ingest import store_history, touch_heartbeat
from models import Device
from ring import RingClient, RingError, RingRateLimited, RingTokenExpired

log = logging.getLogger("threshold.poller")

# Re-read a little history each cycle; the unique ring_request_id drops what we already have.
OVERLAP = timedelta(minutes=10)
FIRST_POLL_LOOKBACK = timedelta(days=1)


class HistoryPoller:
    def __init__(self, ring: RingClient, household_id: int, source: str):
        self.ring = ring
        self.household_id = household_id
        self.source = source
        # Set by the last device sync, so devices from an old token or emulator run are skipped.
        self.device_ids: list[str] = []
        self.last_polled: dict[str, datetime] = {}
        self.last_run: dict = {}
        self._warned_token = False

    async def poll_once(self) -> dict:
        started = datetime.now(tz=UTC)
        written = 0
        errors: list[str] = []
        async with sessionmaker()() as session:
            devices = (
                await session.execute(
                    select(Device).where(Device.household_id == self.household_id, Device.id.in_(self.device_ids))
                )
            ).scalars().all()
            for device in devices:
                try:
                    status = await self.ring.get_status(device.id)
                    if status.online is not None:
                        device.online = status.online
                    if status.online:
                        touch_heartbeat(device, status.reported_at or started)
                    since = self.last_polled.get(device.id, started - FIRST_POLL_LOOKBACK + OVERLAP) - OVERLAP
                    events = await self.ring.get_history(device.id, since=since)
                    written += await store_history(session, self.household_id, events, source=self.source)
                    self.last_polled[device.id] = started
                except RingTokenExpired as exc:
                    if not self._warned_token:
                        log.warning("%s", exc)
                        self._warned_token = True
                    errors.append(str(exc))
                    break
                except RingRateLimited as exc:
                    log.warning("%s", exc)
                    errors.append(str(exc))
                    break
                except RingError as exc:
                    log.warning("poll %s failed: %s %s", device.id, exc, exc.body[:200])
                    errors.append(f"{device.id}: {exc}")
            await session.commit()
        if not errors:
            self._warned_token = False
        if written:
            log.info("poller stored %d new event(s)", written)
        self.last_run = {"at": started, "written": written, "errors": errors}
        return self.last_run
