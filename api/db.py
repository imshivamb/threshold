from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    _engine = create_async_engine(url or get_settings().database_url, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        return init_engine()
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session
