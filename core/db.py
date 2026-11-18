"""Async SQLAlchemy engine/session + portable schema init.

The schema deliberately avoids Postgres-only types so the same models run on
SQLite (laptop P0) and Postgres (compose).  ``sa.JSON`` maps to JSON on
Postgres and TEXT on SQLite; timestamps are ISO-8601 strings for the same
reason (no tz/format drift between dialects).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from core.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

_connect_args = {"check_same_thread": False} if _settings.is_sqlite else {}
# aiosqlite connections are loop-bound, and pytest gives every test its own
# event loop; a pooled asyncpg connection would be reused across dead loops.
# Tests therefore always use NullPool.
_no_pool = _settings.is_sqlite or _settings.env == "test"
engine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=not _no_pool,
    connect_args=_connect_args,
    poolclass=NullPool if _no_pool else None,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables if missing. Real deployments use Alembic; P0 keeps it one line."""
    from core import models  # noqa: F401  (register mappers)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
