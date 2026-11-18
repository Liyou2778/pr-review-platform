"""Read-only analytics: cost, quality, feedback clusters, dashboard payload.

Kept separate from ``service`` because these are pure queries — no writes, no
queue, no model. That makes them safe to expose on a dashboard endpoint and
trivial to test.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics as metrics_mod
from core.models import CostRecord, Feedback, Finding, Repo, Run


async def cost_by_repo(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(
            select(
                CostRecord.repo_full_name,
                func.count(CostRecord.id),
                func.coalesce(func.sum(CostRecord.tokens_in), 0),
                func.coalesce(func.sum(CostRecord.tokens_out), 0),
                func.coalesce(func.sum(CostRecord.cost_usd), 0.0),
            )
            .group_by(CostRecord.repo_full_name)
            .order_by(func.sum(CostRecord.cost_usd).desc())
        )
    ).all()
    return [
        {
            "repo": repo or "(unknown)",
            "runs": int(count or 0),
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "cost_usd": round(float(cost or 0.0), 6),
        }
        for repo, count, tokens_in, tokens_out, cost in rows
    ]


async def findings_by_severity(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(select(Finding.severity, func.count()).group_by(Finding.severity))
    ).all()
    return {str(severity): int(count) for severity, count in rows}


async def feedback_summary(session: AsyncSession) -> dict:
    total = await session.scalar(select(func.count()).select_from(Feedback)) or 0
    by_verdict: dict[str, int] = {}
    for verdict, count in (
        await session.execute(select(Feedback.verdict, func.count()).group_by(Feedback.verdict))
    ).all():
        by_verdict[str(verdict)] = int(count)
    false_positive = by_verdict.get("false_positive", 0)
    return {
        "total": int(total),
        "by_verdict": by_verdict,
        "false_positive_rate": round(false_positive / total, 4) if total else 0.0,
    }


async def feedback_clusters(session: AsyncSession, limit: int = 20) -> list[dict]:
    """Group false positives by rule so they can become rule candidates.

    ``action_hint`` is deliberately blunt: it suggests the kind of change a
    platform owner would consider, not an automatic edit.
    """
    rows = (
        await session.execute(
            select(
                Finding.rule_key,
                func.count(Feedback.id),
                func.count(func.distinct(Finding.id)),
            )
            .join(Feedback, Feedback.finding_id == Finding.id)
            .where(Feedback.verdict == "false_positive")
            .group_by(Finding.rule_key)
            .order_by(func.count(Feedback.id).desc())
            .limit(limit)
        )
    ).all()

    clusters: list[dict] = []
    for rule_key, feedback_count, finding_count in rows:
        examples = (
            await session.execute(
                select(Feedback.note)
                .join(Finding, Finding.id == Feedback.finding_id)
                .where(Finding.rule_key == rule_key, Feedback.verdict == "false_positive")
                .order_by(Feedback.id.desc())
                .limit(3)
            )
        ).scalars().all()
        count = int(feedback_count or 0)
        clusters.append(
            {
                "rule_key": rule_key,
                "feedback_count": count,
                "finding_count": int(finding_count or 0),
                "action_hint": "review-rule" if count >= 3 else "collect-more-signal",
                "examples": [e for e in examples if e],
            }
        )
    return clusters


async def run_stats(session: AsyncSession) -> dict:
    total = await session.scalar(select(func.count()).select_from(Run)) or 0
    by_status: dict[str, int] = {}
    for status, count in (
        await session.execute(select(Run.status, func.count()).group_by(Run.status))
    ).all():
        by_status[str(status)] = int(count)
    latencies = [
        float(v)
        for v in (
            await session.execute(select(Run.latency_ms).where(Run.status == "completed"))
        ).scalars()
        if v
    ]
    engines: dict[str, int] = {}
    for stats in (await session.execute(select(Run.stats))).scalars():
        if isinstance(stats, dict) and stats.get("engine"):
            engines[stats["engine"]] = engines.get(stats["engine"], 0) + 1
    return {
        "total": int(total),
        "by_status": by_status,
        "engines": engines,
        "latency_ms": metrics_mod.summarize(latencies),
    }


async def dashboard(session: AsyncSession) -> dict:
    repos = await session.scalar(select(func.count()).select_from(Repo)) or 0
    return {
        "repos": int(repos),
        "runs": await run_stats(session),
        "findings_by_severity": await findings_by_severity(session),
        "cost_by_repo": await cost_by_repo(session, ),
        "feedback": await feedback_summary(session),
        "clusters": await feedback_clusters(session, limit=5),
    }
