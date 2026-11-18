"""Data retention.

Findings and cost rows grow without bound; a review platform that never
deletes anything becomes a slow, expensive database. Retention is deliberately
explicit and dry-run by default: deleting history is an operational decision,
not a background surprise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import CostRecord, Delivery, Feedback, Finding, Run


def cutoff_iso(days: int) -> str:
    if days <= 0:
        return ""
    return (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()


async def prune(session: AsyncSession, cutoff: str, dry_run: bool = True) -> dict:
    """Delete runs created before ``cutoff`` and everything hanging off them."""
    if not cutoff:
        return {"runs": 0, "findings": 0, "feedback": 0, "costs": 0, "deliveries": 0, "dry_run": dry_run}

    run_ids = list(
        (await session.scalars(select(Run.id).where(Run.created_at < cutoff))).all()
    )
    if not run_ids:
        return {"runs": 0, "findings": 0, "feedback": 0, "costs": 0, "deliveries": 0, "dry_run": dry_run}

    finding_ids = list(
        (await session.scalars(select(Finding.id).where(Finding.run_id.in_(run_ids)))).all()
    )
    counts = {
        "runs": len(run_ids),
        "findings": len(finding_ids),
        "feedback": 0,
        "costs": 0,
        "deliveries": 0,
    }
    if finding_ids:
        rows = await session.execute(select(Feedback).where(Feedback.finding_id.in_(finding_ids)))
        counts["feedback"] = len(rows.all())
    counts["costs"] = int(
        (await session.execute(select(CostRecord).where(CostRecord.run_id.in_(run_ids)))).all().__len__()
    )
    counts["deliveries"] = int(
        (
            await session.execute(
                select(Delivery).where(Delivery.received_at < cutoff)
            )
        ).all().__len__()
    )

    if not dry_run:
        if finding_ids:
            await session.execute(delete(Feedback).where(Feedback.finding_id.in_(finding_ids)))
            await session.execute(delete(Finding).where(Finding.id.in_(finding_ids)))
        await session.execute(delete(CostRecord).where(CostRecord.run_id.in_(run_ids)))
        await session.execute(delete(Run).where(Run.id.in_(run_ids)))
        await session.execute(delete(Delivery).where(Delivery.received_at < cutoff))
        await session.commit()

    counts["dry_run"] = dry_run
    return counts
