"""Service layer: everything that mutates state lives here, not in routes.

Why bother: the API and the worker must apply the *same* rules (idempotency,
dedupe, budget).  If that logic lives in a route handler, the worker always
drifts from it and the two disagree in production.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics as metrics_mod
from core.adapters.github import (
    GitHubError,
    build_review_payload,
    fetch_diff,
    post_review,
)
from core.adapters.reviewer import ReviewContext, ReviewError, get_reviewer
from core.adapters.workspace import WorkspaceError, cleanup_workspace, prepare_workspace
from core.config import get_settings
from core.db import SessionLocal
from core.models import (
    CostRecord,
    Delivery,
    Feedback,
    Finding,
    Repo,
    RuleCandidate,
    Run,
    utcnow_iso,
)
from core.review import iter_added_lines, make_dedup_key, normalize
from core.rules import load_rules
from core.security import open_token

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")


async def ensure_repo(
    session: AsyncSession,
    full_name: str,
    default_branch: str = "main",
    clone_url: str = "",
) -> Repo:
    repo = await session.scalar(select(Repo).where(Repo.full_name == full_name))
    if repo is not None:
        if clone_url and not repo.clone_url:
            repo.clone_url = clone_url
        return repo
    repo = Repo(full_name=full_name, default_branch=default_branch or "main", clone_url=clone_url)
    session.add(repo)
    try:
        await session.flush()
    except IntegrityError:  # concurrent insert
        await session.rollback()
        repo = await session.scalar(select(Repo).where(Repo.full_name == full_name))
        if repo is None:
            raise
    return repo


async def record_delivery(
    session: AsyncSession,
    delivery_id: str,
    event: str,
    action: str,
    repo_full_name: str,
    payload_hash: str,
) -> bool:
    """Return True if this delivery is new, False if it was already processed."""
    session.add(
        Delivery(
            delivery_id=delivery_id,
            event=event,
            action=action,
            repo_full_name=repo_full_name,
            payload_hash=payload_hash,
        )
    )
    try:
        await session.flush()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def create_run(
    session: AsyncSession,
    repo: Repo,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    trigger: str,
    diff_text: str | None = None,
) -> tuple[Run, bool]:
    existing = await session.scalar(
        select(Run).where(
            Run.repo_id == repo.id, Run.pr_number == pr_number, Run.head_sha == head_sha
        )
    )
    if existing is not None:
        return existing, False
    run = Run(
        repo_id=repo.id,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        trigger=trigger,
        diff_text=diff_text,
        status="queued",
    )
    session.add(run)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        run = await session.scalar(
            select(Run).where(
                Run.repo_id == repo.id, Run.pr_number == pr_number, Run.head_sha == head_sha
            )
        )
        if run is None:
            raise
        return run, False
    return run, True


async def claim_run(run_id: int) -> bool:
    """Atomic queue->running transition. False means someone else owns it."""
    async with SessionLocal() as session:
        result = await session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(status="running", started_at=utcnow_iso())
        )
        await session.commit()
        return bool(result.rowcount)


async def _previous_dedup_keys(session: AsyncSession, repo_id: int, pr_number: int, run_id: int) -> set[str]:
    """Keys seen in any earlier run of this PR.

    This drives the ``new_findings`` metric only ("have we reported this before?").
    Delivery uses a different question — "has this finding been *posted*?" — so
    that a dry-run or a failed delivery can still be retried later.
    """
    rows = await session.execute(
        select(Finding.dedup_key)
        .join(Run, Finding.run_id == Run.id)
        .where(
            Run.repo_id == repo_id,
            Run.pr_number == pr_number,
            Finding.run_id != run_id,
        )
    )
    return {r[0] for r in rows.all()}


async def _posted_dedup_keys(
    session: AsyncSession, repo_id: int, pr_number: int, run_id: int
) -> set[str]:
    """Keys already delivered by *other* runs of this PR.

    Each run owns its own finding rows, so a row's ``posted`` flag is not
    enough: without this check every new run would re-post its copy of the
    same findings.
    """
    rows = await session.execute(
        select(Finding.dedup_key)
        .join(Run, Finding.run_id == Run.id)
        .where(
            Run.repo_id == repo_id,
            Run.pr_number == pr_number,
            Finding.run_id != run_id,
            Finding.posted.is_(True),
        )
    )
    return {r[0] for r in rows.all()}


def _delivery_token(repo: Repo | None, settings) -> str:
    """Prefer the repo-scoped token; fall back to the process token in dev.

    Multi-tenant deployments must not share one credential across repos: a
    leaked process token would grant write access to every connected repo.
    """
    scoped = open_token(repo.token_enc) if repo is not None else ""
    return scoped or settings.github_token


SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "blocker": 4}


def meets_severity(severity: str, minimum: str) -> bool:
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(minimum, 0)


async def check_rate_limit(session: AsyncSession, repo: Repo, settings) -> bool:
    """Per-repo hourly cap. Returns False when the repo is over its budget."""
    limit = repo.max_runs_per_hour or settings.repo_hourly_run_limit
    if limit <= 0:
        return True
    cutoff = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    used = await session.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.repo_id == repo.id, Run.created_at >= cutoff)
    )
    return int(used or 0) < limit


async def _deliver_with_retry(
    repo_full_name: str,
    pr_number: int,
    payload: dict,
    token: str,
    api_base: str,
    retries: int = 3,
    base_s: float = 0.5,
) -> tuple[str, int]:
    """Post a review with exponential backoff.

    Returns ``(status, attempts)``. Transport/5xx failures are worth retrying;
    a 4xx (bad token, bad commit) is not, and burns the whole timeout window
    for nothing.
    """
    attempts = 0
    last_error = ""
    for attempt in range(1, max(1, retries) + 1):
        attempts = attempt
        try:
            await post_review(repo_full_name, pr_number, payload, token, api_base)
            return "posted", attempts
        except GitHubError as exc:
            last_error = str(exc)
            retryable = "HTTP 4" not in last_error
            if attempt >= retries or not retryable:
                break
            await asyncio.sleep(base_s * (2 ** (attempt - 1)))
    return f"failed: {last_error}"[:180], attempts


async def process_run(run_id: int) -> dict:
    """Execute one run end to end. Returns the stats dict (also persisted)."""
    settings = get_settings()
    started = time.perf_counter()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise ReviewError(f"run {run_id} not found", "run_missing")
        repo = await session.get(Repo, run.repo_id)
        if repo is None:
            raise ReviewError(f"repo for run {run_id} not found", "repo_missing")

        workspace: str | None = None
        try:
            cli_mode = (settings.ocr_mode or "fake").lower() == "cli"
            if cli_mode:
                token = _delivery_token(repo, settings)
                workspace = await prepare_workspace(
                    repo.clone_url, run.base_sha, run.head_sha, token=token
                )
                diff = ""
                added = []
            else:
                diff = run.diff_text or await fetch_diff(repo.full_name, run.pr_number)
                diff_bytes = len(diff.encode("utf-8"))
                if diff_bytes > settings.max_diff_bytes:
                    raise ReviewError(
                        f"diff is {diff_bytes} bytes, over the {settings.max_diff_bytes} limit",
                        "diff_too_large",
                    )
                added = iter_added_lines(diff)

            rules = load_rules(settings.rules_path)
            reviewer = get_reviewer(settings, rules)
            ctx = ReviewContext(
                repo_full_name=repo.full_name,
                pr_number=run.pr_number,
                base_sha=run.base_sha,
                head_sha=run.head_sha,
                diff_text=diff,
                workspace_path=workspace,
            )
            result = await reviewer.review(ctx)
            drafts = normalize(result.findings, settings.max_findings_per_file)
            prior = await _previous_dedup_keys(session, repo.id, run.pr_number, run.id)
            posted_prior = await _posted_dedup_keys(session, repo.id, run.pr_number, run.id)

            # A finding that disappeared in this run is considered resolved.
            current_keys = {
                make_dedup_key(
                    repo.full_name, run.pr_number, draft.file_path, draft.line_start, draft.rule_key
                )
                for draft in drafts
            }
            stale_rows = (
                await session.execute(
                    select(Finding)
                    .join(Run, Finding.run_id == Run.id)
                    .where(
                        Run.repo_id == repo.id,
                        Run.pr_number == run.pr_number,
                        Finding.run_id != run.id,
                        Finding.resolved.is_(False),
                    )
                )
            ).scalars().all()
            resolved_count = 0
            for stale in stale_rows:
                if stale.dedup_key not in current_keys:
                    stale.resolved = True
                    stale.resolved_at = utcnow_iso()
                    resolved_count += 1

            new_rows: list[Finding] = []
            new_drafts = []
            for draft in drafts:
                key = make_dedup_key(
                    repo.full_name, run.pr_number, draft.file_path, draft.line_start, draft.rule_key
                )
                if any(f.dedup_key == key for f in run.findings):
                    continue
                row = Finding(
                    run_id=run.id,
                    file_path=draft.file_path,
                    line_start=draft.line_start,
                    line_end=draft.line_end or draft.line_start,
                    severity=draft.severity,
                    rule_key=draft.rule_key,
                    message=draft.message,
                    evidence=draft.evidence,
                    dedup_key=key,
                )
                session.add(row)
                new_rows.append(row)
                if key not in prior:
                    new_drafts.append(draft)

            comment_status = "skipped"
            delivery_attempts = 0
            candidates = [f for f in run.findings if not f.posted] + new_rows
            # Per-repo policy: below-threshold findings are stored, not commented.
            # Cross-run delivery dedupe: never re-comment a key already delivered
            # by another run of this PR.
            to_post = [
                row
                for row in candidates
                if not row.posted
                and row.dedup_key not in posted_prior
                and meets_severity(row.severity, repo.min_severity)
            ]
            # GitHub reviews are atomic: one unresolvable line rejects the whole
            # review with 422. Only inline-comment lines that exist in this diff.
            allowed_lines = {(line.path, line.line_no) for line in added} if added else None
            inline_rows = [
                row
                for row in to_post
                if allowed_lines is None or (row.file_path, row.line_start) in allowed_lines
            ]
            outside_diff = len(to_post) - len(inline_rows)
            delivery_token = _delivery_token(repo, settings)
            if inline_rows and not settings.github_dry_run and delivery_token:
                payload = build_review_payload(
                    run.head_sha,
                    [
                        {
                            "file_path": row.file_path,
                            "line_start": row.line_start,
                            "severity": row.severity,
                            "rule_key": row.rule_key,
                            "message": row.message,
                        }
                        for row in inline_rows
                    ],
                )
                if outside_diff:
                    payload["body"] += f"（另有 {outside_diff} 条不在本次 diff 范围内，已记录未评论）"
                comment_status, delivery_attempts = await _deliver_with_retry(
                    repo.full_name,
                    run.pr_number,
                    payload,
                    delivery_token,
                    settings.github_api_base,
                    retries=settings.post_retries,
                    base_s=settings.post_retry_base_s,
                )
                if comment_status == "posted":
                    for row in inline_rows:
                        row.posted = True
            elif to_post and not settings.github_dry_run and not delivery_token:
                # Only a *real* delivery attempt without a credential is a
                # failure; a dry run is expected to have no token at all.
                comment_status = "failed: no github token configured"

            latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
            stats = {
                "engine": result.engine,
                "files_touched": result.meta.get("files_reviewed") or len({line.path for line in added}),
                "added_lines": len(added),
                "findings": len(drafts),
                "new_findings": len(new_drafts),
                "suppressed_duplicates": len(drafts) - len(new_drafts),
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "cost_usd": round(result.cost_usd, 6),
                "latency_ms": latency_ms,
                "dry_run": settings.github_dry_run,
                "comment_status": comment_status,
                "delivery_attempts": delivery_attempts,
                "outside_diff": outside_diff,
                "min_severity": repo.min_severity,
                "resolved_findings": resolved_count,
                "engine_meta": result.meta,
            }
            run.status = "completed"
            run.latency_ms = latency_ms
            run.ended_at = utcnow_iso()
            run.stats = stats
            run.error = None
            run.error_type = None
            session.add(
                CostRecord(
                    run_id=run.id,
                    repo_full_name=repo.full_name,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    cost_usd=result.cost_usd,
                    engine=result.engine,
                )
            )
            await session.commit()
            return stats

        except ReviewError as exc:
            await _fail(session, run, str(exc), exc.error_type, started)
            raise
        except GitHubError as exc:
            await _fail(session, run, str(exc), "diff_fetch_failed", started)
            raise ReviewError(str(exc), "diff_fetch_failed") from exc
        except WorkspaceError as exc:
            await _fail(session, run, str(exc), exc.error_type, started)
            raise ReviewError(str(exc), exc.error_type) from exc
        except Exception as exc:  # noqa: BLE001 - engine-level guard
            log.exception("run %s crashed", run_id)
            await _fail(session, run, f"{type(exc).__name__}: {exc}", "engine_error", started)
            raise
        finally:
            if workspace:
                cleanup_workspace(workspace)


async def _fail(session: AsyncSession, run: Run, message: str, error_type: str, started: float) -> None:
    run.status = "failed"
    run.error = message[:2000]
    run.error_type = error_type
    run.latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
    run.ended_at = utcnow_iso()
    run.stats = {"error_type": error_type, "latency_ms": run.latency_ms}
    await session.commit()


async def add_feedback(
    session: AsyncSession, finding_id: int, verdict: str, actor: str = "", note: str = ""
) -> Feedback:
    finding = await session.get(Finding, finding_id)
    if finding is None:
        raise ValueError(f"finding {finding_id} not found")
    feedback = Feedback(finding_id=finding_id, verdict=verdict, actor=actor, note=note)
    session.add(feedback)
    await session.commit()
    return feedback


CANDIDATE_STATUSES = ("draft", "approved", "rejected")


async def propose_rule_candidate(
    session: AsyncSession,
    rule_key: str,
    note: str = "",
    owner: str = "",
    action_hint: str = "review-rule",
) -> tuple[RuleCandidate, bool]:
    """Idempotent per (rule_key, draft): one open candidate at a time."""
    existing = await session.scalar(
        select(RuleCandidate).where(
            RuleCandidate.rule_key == rule_key, RuleCandidate.status == "draft"
        )
    )
    if existing is not None:
        return existing, False
    candidate = RuleCandidate(
        rule_key=rule_key, note=note, owner=owner, action_hint=action_hint
    )
    session.add(candidate)
    await session.commit()
    return candidate, True


async def decide_rule_candidate(
    session: AsyncSession, candidate_id: int, status: str, owner: str = ""
) -> RuleCandidate:
    if status not in ("approved", "rejected"):
        raise ValueError("decision must be approved or rejected")
    candidate = await session.get(RuleCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"candidate {candidate_id} not found")
    if candidate.status != "draft":
        raise ValueError(f"candidate is already {candidate.status}")
    candidate.status = status
    candidate.decided_at = utcnow_iso()
    if owner:
        candidate.owner = owner
    await session.commit()
    return candidate


async def list_rule_candidates(
    session: AsyncSession, status: str | None = None
) -> list[RuleCandidate]:
    stmt = select(RuleCandidate).order_by(RuleCandidate.id.desc())
    if status:
        stmt = stmt.where(RuleCandidate.status == status)
    return list((await session.scalars(stmt)).all())


async def post_run(run_id: int) -> dict:
    """Deliver an existing run's unposted findings. No re-review, no re-cost.

    This is the retry path for a failed comment delivery: the review result is
    durable, the notification is replayable.
    """
    settings = get_settings()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise ReviewError(f"run {run_id} not found", "run_missing")
        repo = await session.get(Repo, run.repo_id)
        pending = [f for f in run.findings if not f.posted]
        if not pending:
            return {"posted": 0, "status": (run.stats or {}).get("comment_status", "nothing_to_post")}
        delivery_token = _delivery_token(repo, settings)
        if not delivery_token:
            raise ReviewError("no GitHub token configured for this repo", "post_no_token")
        payload = build_review_payload(
            run.head_sha,
            [
                {
                    "file_path": f.file_path,
                    "line_start": f.line_start,
                    "severity": f.severity,
                    "rule_key": f.rule_key,
                    "message": f.message,
                }
                for f in pending
            ],
        )
        await post_review(
            repo.full_name if repo else "",
            run.pr_number,
            payload,
            delivery_token,
            settings.github_api_base,
        )
        for finding in pending:
            finding.posted = True
        run.stats = {**(run.stats or {}), "comment_status": "posted"}
        await session.commit()
        return {"posted": len(pending), "status": "posted"}


async def overview(session: AsyncSession) -> dict:
    total_runs = await session.scalar(select(func.count()).select_from(Run)) or 0
    by_status = {}
    for status, count in (
        await session.execute(select(Run.status, func.count()).group_by(Run.status))
    ).all():
        by_status[status] = count
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(CostRecord.tokens_in), 0),
                func.coalesce(func.sum(CostRecord.tokens_out), 0),
                func.coalesce(func.sum(CostRecord.cost_usd), 0.0),
            )
        )
    ).one()
    latencies = [
        float(v)
        for v in (await session.execute(select(Run.latency_ms).where(Run.status == "completed"))).scalars()
        if v
    ]
    verdicts = {}
    for verdict, count in (
        await session.execute(select(Feedback.verdict, func.count()).group_by(Feedback.verdict))
    ).all():
        verdicts[verdict] = count
    findings_total = await session.scalar(select(func.count()).select_from(Finding)) or 0
    return {
        "runs": {"total": total_runs, "by_status": by_status},
        "findings": {"total": findings_total, "feedback": verdicts},
        "cost": {
            "tokens_in": int(totals[0] or 0),
            "tokens_out": int(totals[1] or 0),
            "cost_usd": round(float(totals[2] or 0.0), 6),
        },
        "latency_ms": metrics_mod.summarize(latencies),
    }
