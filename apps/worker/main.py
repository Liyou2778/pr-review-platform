"""Worker process: pop -> claim -> review -> persist.

Run it as its own process in production (``python -m apps.worker.main``).
Recovery on boot is deliberate: a ``running`` row without a live worker is a
lie left by a crash, so it is re-queued rather than failed.  Failed runs lose
work; re-queued runs are idempotent because findings are deduped by key.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, update

from core.adapters.reviewer import ReviewError
from core.config import get_settings
from core.db import SessionLocal, engine, init_db
from core.logging import configure_logging
from core.models import Run, utcnow_iso
from core.queue import get_queue
from core.service import claim_run, process_run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("worker")

_stop = asyncio.Event()


async def recover_stale(stale_after_s: int | None = None) -> int:
    """Re-queue runs abandoned by a crashed worker.

    Only runs that have been ``running`` for longer than the threshold are
    recovered.  Resetting *all* running rows would let a second worker steal
    jobs that the first worker is still executing — a bug that only shows up
    when you scale to N workers, which is exactly when it hurts.
    """
    threshold = stale_after_s if stale_after_s is not None else get_settings().run_stale_after_s
    cutoff = (datetime.now(UTC) - timedelta(seconds=threshold)).replace(
        microsecond=0
    ).isoformat()
    async with SessionLocal() as session:
        result = await session.execute(
            update(Run)
            .where(
                Run.status == "running",
                or_(Run.started_at.is_(None), Run.started_at < cutoff),
            )
            .values(status="queued", started_at=None)
        )
        await session.commit()
    count = int(result.rowcount or 0)
    if count:
        log.warning("recovered %d stale running run(s) back to queued", count)
    return count


async def _db_fallback() -> int | None:
    """The queue is an optimization; the runs table is the source of truth.

    If a broker is down, or a producer crashed between COMMIT and LPUSH, the
    run stays ``queued``.  Scanning for it here means a lost message costs
    latency, never a lost review.
    """
    from sqlalchemy import select

    async with SessionLocal() as session:
        row = await session.scalar(select(Run.id).where(Run.status == "queued").order_by(Run.id).limit(1))
    return int(row) if row is not None else None


async def run_once(timeout_s: float = 0.05) -> int | None:
    """Process at most one job. Returns the run id, or None when idle."""
    queue = get_queue()
    run_id = await queue.pop(timeout_s)
    if run_id is None:
        run_id = await _db_fallback()
    if run_id is None:
        return None
    if not await claim_run(run_id):
        return None
    try:
        stats = await process_run(run_id)
        log.info(
            "run=%s engine=%s findings=%s new=%s tokens=%s/%s latency=%sms",
            run_id,
            stats.get("engine"),
            stats.get("findings"),
            stats.get("new_findings"),
            stats.get("tokens_in"),
            stats.get("tokens_out"),
            stats.get("latency_ms"),
        )
    except ReviewError as exc:
        log.warning("run=%s failed (%s): %s", run_id, exc.error_type, exc)
    except Exception:  # noqa: BLE001 - a worker must never die on one bad job
        log.exception("run=%s crashed", run_id)
    return run_id


async def _worker_loop(slot: int) -> None:
    """One pull loop. Errors are contained here so a broker hiccup cannot kill
    the process: log, back off, keep pulling."""
    while not _stop.is_set():
        try:
            processed = await run_once(timeout_s=2.0)
        except Exception:  # noqa: BLE001 - worker must survive broker/DB errors
            log.exception("worker slot %s: unhandled error, backing off", slot)
            await asyncio.sleep(2.0)
            continue
        if processed is None:
            await asyncio.sleep(0.2)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_json)
    settings.enforce_security()  # a worker with weak config must not start either
    await init_db()
    for warning in settings.boot_warnings():
        log.warning("boot: %s", warning)
    await recover_stale()
    concurrency = max(1, settings.worker_concurrency)
    log.info(
        "worker up: queue=%s storage=%s reviewer=%s dry_run=%s concurrency=%s",
        settings.queue_backend,
        "sqlite" if settings.is_sqlite else "postgres",
        settings.ocr_mode,
        settings.github_dry_run,
        concurrency,
    )
    await asyncio.gather(*[_worker_loop(slot) for slot in range(concurrency)])
    await engine.dispose()
    log.info("worker stopped at %s", utcnow_iso())


def _install_signal_handlers() -> None:
    if sys.platform == "win32":
        return  # Ctrl+C raises KeyboardInterrupt; handled below

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop.set)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted")
