from datetime import UTC, datetime

from sqlalchemy import select

from ingest import ensure_household, store_history
from models import AccessLog, Event
from ring import HistoryEvent


def history(event_id: str, event_type: str, start_ms: int) -> HistoryEvent:
    raw = {"id": event_id, "attributes": {"event_type": event_type, "start": start_ms}}
    return HistoryEvent(
        id=event_id,
        device_id="ava1.ring.device.PLAYGROUND",
        event_type=event_type,
        sub_type=None,
        started_at=datetime.fromtimestamp(start_ms / 1000, tz=UTC),
        ended_at=None,
        raw=raw,
    )


async def test_polled_history_is_stored_once_and_on_demand_is_access_logged(settings, sessions):
    events = [history("h1", "on_demand", 1790925588961), history("h2", "motion", 1790925500000)]
    async with sessions() as session:
        household = await ensure_household(session, settings)
        assert await store_history(session, household.id, events, source="playground") == 2
        # The next poll overlaps: nothing new is written.
        assert await store_history(session, household.id, events, source="playground") == 0
        await session.commit()

        rows = (await session.execute(select(Event).order_by(Event.started_at))).scalars().all()
        logs = (await session.execute(select(AccessLog))).scalars().all()

    assert [(e.type, e.source) for e in rows] == [("motion_detected", "playground"), ("on_demand", "playground")]
    assert rows[1].ring_request_id == "history:h1"
    assert [(a.action, a.target) for a in logs] == [("live_view", "ava1.ring.device.PLAYGROUND")]
