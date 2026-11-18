"""Queue abstraction with three interchangeable backends.

* ``memory`` — asyncio.Queue, single process, used by tests and by a
  single-process demo (uvicorn serving API + worker thread).
* ``db``     — the runs table *is* the queue.  Works on SQLite and Postgres,
  survives restarts, and needs no extra infrastructure.  This is the laptop
  default when Redis is not available.
* ``redis``  — production path: LPUSH/BRPOP, lets N workers scale out.

The interface is intentionally tiny: a job is just a run id.  Claiming is the
worker's job (atomic UPDATE ... WHERE status='queued'), so a duplicated or
stale message can never run twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from sqlalchemy import select

from core.config import get_settings

log = logging.getLogger(__name__)

QUEUE_KEY = "prreview:runs"


class Queue(Protocol):
    backend: str

    async def push(self, run_id: int) -> None: ...

    async def pop(self, timeout_s: float = 5.0) -> int | None: ...


class MemoryQueue:
    backend = "memory"
    _queue: asyncio.Queue[int] | None = None

    @classmethod
    def _get(cls) -> asyncio.Queue[int]:
        if cls._queue is None:
            cls._queue = asyncio.Queue()
        return cls._queue

    async def push(self, run_id: int) -> None:
        await self._get().put(run_id)

    async def pop(self, timeout_s: float = 5.0) -> int | None:
        try:
            return await asyncio.wait_for(self._get().get(), timeout=timeout_s)
        except TimeoutError:
            return None


class DbQueue:
    backend = "db"

    async def push(self, run_id: int) -> None:
        # The row already exists with status='queued'; nothing else to do.
        return None

    async def pop(self, timeout_s: float = 5.0) -> int | None:
        from core.db import SessionLocal
        from core.models import Run

        async with SessionLocal() as session:
            row = await session.scalar(
                select(Run.id).where(Run.status == "queued").order_by(Run.id).limit(1)
            )
        if row is None:
            await asyncio.sleep(min(timeout_s, 1.0))
            return None
        return int(row)


class RedisQueue:
    backend = "redis"

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        # protocol=2 keeps compatibility with Redis 5 (RESP2).  redis-py 8
        # defaults to RESP3 and sends HELLO 3, which Redis 5 rejects with
        # "unknown command HELLO".
        self._client = aioredis.from_url(url, decode_responses=True, protocol=2)

    async def push(self, run_id: int) -> None:
        await self._client.lpush(QUEUE_KEY, json.dumps({"run_id": run_id}))

    async def pop(self, timeout_s: float = 5.0) -> int | None:
        item = await self._client.brpop(QUEUE_KEY, timeout=int(timeout_s))
        if not item:
            return None
        _, payload = item
        try:
            return int(json.loads(payload)["run_id"])
        except (KeyError, ValueError, TypeError):
            log.warning("dropping malformed queue payload: %r", payload)
            return None


_queue: Queue | None = None


def get_queue() -> Queue:
    global _queue
    if _queue is not None:
        return _queue
    settings = get_settings()
    backend = (settings.queue_backend or "memory").lower()
    if backend == "redis":
        _queue = RedisQueue(settings.redis_url)
    elif backend == "db":
        _queue = DbQueue()
    elif backend == "memory":
        _queue = MemoryQueue()
    else:
        raise ValueError(f"unsupported QUEUE_BACKEND={backend!r} (memory|db|redis)")
    log.info("queue backend = %s", backend)
    return _queue


def reset_queue_for_tests() -> None:
    global _queue
    _queue = None
    MemoryQueue._queue = None
