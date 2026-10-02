"""Settings, devices, events and live-view endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Anchor, Device, Event, Visit
from rhythm import can_alert
from ring import RingError, RingTokenExpired
from routine import LOOKBACK_DAYS

router = APIRouter()


def ctx(request: Request):
    return request.app.state.ctx


class TokenIn(BaseModel):
    token: str = Field(min_length=10)


@router.get("/health")
async def health(c=Depends(ctx)) -> dict:
    return {
        "ok": True,
        "ring_mode": c.settings.ring_mode,
        "ring_url": c.settings.ring_url,
        "token": c.tokens.status(),
        "poller": c.poller.last_run if c.poller else None,
    }


@router.get("/settings/playground-token")
async def token_status(c=Depends(ctx)) -> dict:
    return c.tokens.status()


@router.post("/settings/playground-token")
async def set_playground_token(body: TokenIn, c=Depends(ctx)) -> dict:
    """Paste a fresh 30-minute Playground token. Validates it by listing devices."""
    if c.settings.ring_mode != "playground":
        raise HTTPException(409, "RING_MODE is emulator; a Playground token is not used")
    c.tokens.set(body.token)
    try:
        devices = await c.sync_devices()
    except RingTokenExpired as exc:
        raise HTTPException(401, str(exc)) from exc
    except RingError as exc:
        raise HTTPException(502, f"{exc} {exc.body[:200]}") from exc
    backfilled = await c.backfill()
    return {"token": c.tokens.status(), "devices": [d.name for d in devices], "backfill": backfilled}


@router.get("/devices")
async def list_devices(session: AsyncSession = Depends(get_session), c=Depends(ctx)) -> list[dict]:
    rows = (await session.execute(select(Device).where(Device.household_id == c.household_id))).scalars()
    return [
        {"id": d.id, "name": d.name, "kind": d.kind, "online": d.online, "last_heartbeat_at": d.last_heartbeat_at}
        for d in rows
    ]


@router.post("/devices/sync")
async def sync_devices(c=Depends(ctx)) -> list[str]:
    try:
        return [d.name for d in await c.sync_devices()]
    except RingTokenExpired as exc:
        raise HTTPException(401, str(exc)) from exc


@router.post("/poll")
async def poll_now(c=Depends(ctx)) -> dict:
    if not c.poller:
        raise HTTPException(409, "poller is off in emulator mode (set POLL_IN_EMULATOR_MODE=true)")
    return await c.poller.poll_once()


@router.get("/events")
async def recent_events(limit: int = 50, session: AsyncSession = Depends(get_session), c=Depends(ctx)) -> list[dict]:
    rows = (
        await session.execute(
            select(Event)
            .where(Event.household_id == c.household_id)
            .order_by(Event.started_at.desc(), Event.id.desc())
            .limit(min(limit, 500))
        )
    ).scalars()
    return [
        {
            "id": e.id,
            "ring_request_id": e.ring_request_id,
            "device_id": e.device_id,
            "type": e.type,
            "sub_type": e.sub_type,
            "started_at": e.started_at,
            "source": e.source,
        }
        for e in rows
    ]


@router.post("/devices/{device_id}/whep")
async def start_live_view(device_id: str, request: Request, c=Depends(ctx)) -> Response:
    """Browser posts its SDP offer here; we forward it to Ring server-side (CORS blocks browsers)."""
    offer = (await request.body()).decode()
    try:
        session = await c.ring.start_whep(device_id, offer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RingTokenExpired as exc:
        raise HTTPException(401, str(exc)) from exc
    except RingError as exc:
        raise HTTPException(502, f"{exc} {exc.body[:200]}") from exc
    return Response(
        session.sdp_answer,
        status_code=201,
        media_type="application/sdp",
        headers={"X-Ring-Session-Url": session.session_url},
    )


@router.post("/ring/backfill")
async def backfill_history(days: int = LOOKBACK_DAYS, c=Depends(ctx)) -> dict:
    """Read up to ``days`` of past Ring history, then relearn visits and the rhythm."""
    try:
        return await c.backfill(min(max(days, 1), 90))
    except RingTokenExpired as exc:
        raise HTTPException(401, str(exc)) from exc
    except RingError as exc:
        raise HTTPException(502, f"{exc} {exc.body[:200]}") from exc


def _household(household_id: int, c) -> None:
    if household_id != c.household_id:
        raise HTTPException(404, "household not found")


@router.get("/households/{household_id}/rhythm")
async def rhythm(household_id: int, session: AsyncSession = Depends(get_session), c=Depends(ctx)) -> list[dict]:
    """Learned anchors: usual window, how reliable they are, and whether they may alert."""
    _household(household_id, c)
    rows = (
        await session.execute(
            select(Anchor).where(Anchor.household_id == household_id).order_by(Anchor.window_start)
        )
    ).scalars()
    return [
        {
            "name": a.name,
            "day_group": a.day_group,
            "window": [a.window_start.isoformat("minutes"), a.window_end.isoformat("minutes")],
            "confidence": a.confidence,
            "days_seen": a.days_seen,
            "days_observed": a.days_observed,
            "can_alert": can_alert(a.confidence, a.days_observed),
            "stats": a.stats,
            "computed_at": a.computed_at,
        }
        for a in rows
    ]


@router.post("/households/{household_id}/rhythm/recompute")
async def recompute_rhythm(household_id: int, c=Depends(ctx)) -> dict:
    _household(household_id, c)
    return await c.relearn()


@router.get("/households/{household_id}/visits")
async def recent_visits(
    household_id: int, limit: int = 50, session: AsyncSession = Depends(get_session), c=Depends(ctx)
) -> list[dict]:
    _household(household_id, c)
    rows = (
        await session.execute(
            select(Visit)
            .where(Visit.household_id == household_id)
            .order_by(Visit.started_at.desc())
            .limit(min(limit, 500))
        )
    ).scalars()
    return [
        {
            "id": v.id,
            "started_at": v.started_at,
            "ended_at": v.ended_at,
            "minutes": round((v.ended_at - v.started_at).total_seconds() / 60),
            "role_guess": v.role_guess,
            "event_ids": v.event_ids,
        }
        for v in rows
    ]
