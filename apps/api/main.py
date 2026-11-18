"""FastAPI entrypoint: webhook intake, run/finding queries, feedback, metrics."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import analytics, service
from core.adapters.github import GitHubError, fetch_diff, fetch_pull_request_meta, parse_webhook
from core.adapters.reviewer import ReviewError
from core.adapters.workspace import WorkspaceError, ensure_allowed_clone_url
from core.config import get_settings
from core.db import get_session, init_db
from core.logging import configure_logging
from core.models import Feedback, Finding, Repo, Run
from core.queue import get_queue
from core.security import mask, seal_token, verify_api_token, verify_signature

log = logging.getLogger(__name__)
settings = get_settings()

FEEDBACK_VERDICTS = {"false_positive", "fixed", "ignored", "agreed"}


async def _enqueue(run_id: int) -> None:
    """Best-effort enqueue. The run is already committed, so a broker outage
    degrades latency (worker's DB fallback picks it up) instead of losing work."""
    try:
        await get_queue().push(run_id)
    except Exception as exc:  # noqa: BLE001 - broker must not 500 the request
        log.warning("enqueue failed for run %s: %s", run_id, exc)


class ReplayRequest(BaseModel):
    repo_full_name: str = Field(min_length=3)
    pr_number: int = Field(ge=1)
    diff: str | None = None
    clone_url: str = ""
    base_sha: str = "base"
    head_sha: str | None = None
    trigger: str = "replay"


class FeedbackRequest(BaseModel):
    verdict: str
    actor: str = ""
    note: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_json)
    await init_db()
    settings.enforce_security()  # fail fast in prod, no-op in dev/test
    for warning in settings.boot_warnings():
        log.warning("boot: %s", warning)
    log.info("%s v%s started (env=%s)", settings.app_name, settings.version, settings.env)
    yield


app = FastAPI(title="PR Review Platform", version=settings.version, lifespan=lifespan)


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    """Request id, read/write auth, body cap and access logging in one place.

    * ``/api/*`` and ``/dashboard`` require ``X-API-Token`` (or ``?token=``)
      whenever ``API_TOKEN`` is configured.
    * Mutating calls are additionally refused (503) in non-dev when no token
      is configured at all — an open mutation surface is worse than downtime.
    * ``X-Request-Id`` is echoed on every response so a user report can be
      traced to log lines.
    """
    cfg = get_settings()
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    path = request.url.path
    started = time.perf_counter()

    protected = path.startswith("/api/") or path.startswith("/dashboard")
    if cfg.api_token and protected:
        provided = request.headers.get("x-api-token") or request.query_params.get("token")
        if not verify_api_token(cfg.api_token, provided):
            return JSONResponse(
                {"detail": "invalid or missing X-API-Token"},
                status_code=401,
                headers={"X-Request-Id": request_id},
            )
    elif (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and path.startswith("/api/")
        and cfg.env not in ("dev", "test")
    ):
        return JSONResponse(
            {"detail": "API_TOKEN is not configured; refusing mutating calls"},
            status_code=503,
            headers={"X-Request-Id": request_id},
        )

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cfg.max_request_bytes:
        return JSONResponse(
            {"detail": "request body too large"},
            status_code=413,
            headers={"X-Request-Id": request_id},
        )

    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    log.info(
        "%s %s -> %s",
        request.method,
        path,
        response.status_code,
        extra={
            "request_id": request_id,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return response


@app.get("/")
async def index() -> dict:
    return {
        "name": settings.app_name,
        "version": settings.version,
        "endpoints": [
            "POST /webhooks/github",
            "POST /api/replay",
            "GET /api/runs",
            "GET /api/runs/{id}",
            "GET /api/runs/{id}/findings",
            "POST /api/runs/{id}/retry",
            "POST /api/findings/{id}/feedback",
            "GET /api/stats",
            "GET /healthz",
        ],
    }


@app.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(select(1))
    return {
        "status": "ok",
        "version": settings.version,
        "env": settings.env,
        "queue": settings.queue_backend,
        "storage": "sqlite" if settings.is_sqlite else "postgres",
        "reviewer": settings.ocr_mode,
        "dry_run": settings.github_dry_run,
    }


@app.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    if not verify_signature(settings.github_webhook_secret, body, signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON payload: {exc}") from exc

    event = request.headers.get("x-github-event", "")
    delivery_id = request.headers.get("x-github-delivery") or hashlib.sha256(body).hexdigest()
    pr_event = parse_webhook(event, payload)
    repo_full_name = (payload.get("repository") or {}).get("full_name", "")

    if pr_event is None:
        return JSONResponse({"ignored": True, "reason": f"event={event!r} action={payload.get('action')!r}"})

    is_new = await service.record_delivery(
        session,
        delivery_id=delivery_id,
        event=event,
        action=pr_event.action,
        repo_full_name=repo_full_name,
        payload_hash=hashlib.sha256(body).hexdigest(),
    )
    if not is_new:
        return JSONResponse({"duplicate": True, "delivery_id": delivery_id}, status_code=200)

    repo = await service.ensure_repo(
        session,
        pr_event.repo_full_name,
        (payload.get("repository") or {}).get("default_branch", "main"),
        clone_url=pr_event.clone_url,
    )
    if not await service.check_rate_limit(session, repo, settings):
        raise HTTPException(status_code=429, detail="repo hourly run limit reached")
    run, created = await service.create_run(
        session,
        repo=repo,
        pr_number=pr_event.pr_number,
        base_sha=pr_event.base_sha,
        head_sha=pr_event.head_sha,
        trigger="webhook",
    )
    await session.commit()

    if created:
        await _enqueue(run.id)
    return JSONResponse(
        {"run_id": run.id, "status": run.status, "created": created, "pr": pr_event.pr_number},
        status_code=202 if created else 200,
    )


@app.post("/api/replay", status_code=202)
async def replay(
    body: ReplayRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Re-run a review from a stored diff (demos, bench, CI replay)."""
    diff = body.diff
    if body.clone_url:
        try:
            ensure_allowed_clone_url(body.clone_url, settings)
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    cli_mode = (settings.ocr_mode or "fake").lower() == "cli"
    if diff is None and not cli_mode:
        try:
            diff = await fetch_diff(body.repo_full_name, body.pr_number)
        except Exception as exc:  # noqa: BLE001 - surface as a 400, not a 500
            raise HTTPException(status_code=400, detail=f"cannot fetch diff: {exc}") from exc

    seed = diff or f"{body.repo_full_name}:{body.pr_number}:{body.head_sha or ''}"
    head_sha = body.head_sha
    base_sha = body.base_sha
    try:
        meta = await fetch_pull_request_meta(
            body.repo_full_name, body.pr_number, settings.github_token
        )
        head_sha = head_sha or meta.get("head_sha")
        if base_sha == "base":
            base_sha = meta.get("base_sha") or base_sha
    except GitHubError:
        meta = {}
    head_sha = head_sha or hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    repo = await service.ensure_repo(session, body.repo_full_name, clone_url=body.clone_url)
    if not await service.check_rate_limit(session, repo, settings):
        raise HTTPException(status_code=429, detail="repo hourly run limit reached")
    run, created = await service.create_run(
        session,
        repo=repo,
        pr_number=body.pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        trigger=body.trigger,
        diff_text=diff,
    )
    await session.commit()
    if created:
        await _enqueue(run.id)
    return {"run_id": run.id, "status": run.status, "created": created}


@app.get("/api/runs")
async def list_runs(
    limit: int = Query(20, ge=1, le=200),
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(Run).order_by(Run.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(Run.status == status)
    runs = (await session.scalars(stmt)).all()
    return {"items": [_run_dict(run) for run in runs], "count": len(runs)}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    data = _run_dict(run)
    data["findings"] = [_finding_dict(f) for f in run.findings]
    return data


@app.get("/api/runs/{run_id}/findings")
async def run_findings(run_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "items": [_finding_dict(f) for f in run.findings]}


@app.post("/api/runs/{run_id}/retry", status_code=202)
async def retry_run(run_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status in service.ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail=f"run is {run.status}")
    run.status = "queued"
    run.error = None
    run.error_type = None
    run.ended_at = None
    await session.commit()
    await _enqueue(run.id)
    return {"run_id": run.id, "status": "queued"}


@app.post("/api/runs/{run_id}/post")
async def post_run_endpoint(run_id: int) -> dict:
    """Replay comment delivery for an existing run (no re-review, no re-cost)."""
    try:
        return await service.post_run(run_id)
    except ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/findings/{finding_id}/feedback")
async def post_feedback(
    finding_id: int,
    body: FeedbackRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if body.verdict not in FEEDBACK_VERDICTS:
        raise HTTPException(status_code=422, detail=f"verdict must be one of {sorted(FEEDBACK_VERDICTS)}")
    try:
        feedback = await service.add_feedback(
            session, finding_id, body.verdict, body.actor, body.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": feedback.id, "finding_id": finding_id, "verdict": feedback.verdict}


class RepoTokenRequest(BaseModel):
    token: str = Field(min_length=8)


class RepoPolicyRequest(BaseModel):
    min_severity: str | None = None
    max_runs_per_hour: int | None = None
    enabled: bool | None = None


@app.put("/api/repos/{full_name:path}/policy")
async def set_repo_policy(
    full_name: str,
    body: RepoPolicyRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Per-repo review policy: severity floor, hourly budget, kill switch."""
    repo = await session.scalar(select(Repo).where(Repo.full_name == full_name))
    if repo is None:
        raise HTTPException(status_code=404, detail="repo not found")
    if body.min_severity is not None:
        if body.min_severity not in ("low", "medium", "high", "blocker"):
            raise HTTPException(status_code=422, detail="min_severity must be low|medium|high|blocker")
        repo.min_severity = body.min_severity
    if body.max_runs_per_hour is not None:
        if body.max_runs_per_hour < 0:
            raise HTTPException(status_code=422, detail="max_runs_per_hour must be >= 0")
        repo.max_runs_per_hour = body.max_runs_per_hour
    if body.enabled is not None:
        repo.enabled = body.enabled
    await session.commit()
    return {
        "repo": repo.full_name,
        "min_severity": repo.min_severity,
        "max_runs_per_hour": repo.max_runs_per_hour,
        "enabled": repo.enabled,
    }


@app.put("/api/repos/{full_name:path}/token")
async def set_repo_token(
    full_name: str,
    body: RepoTokenRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Store a repo-scoped credential (encrypted at rest).

    Prefer this over one global token: a leaked process token must not grant
    write access to every connected repository.
    """
    repo = await session.scalar(select(Repo).where(Repo.full_name == full_name))
    if repo is None:
        raise HTTPException(status_code=404, detail="repo not found")
    repo.token_enc = seal_token(body.token)
    await session.commit()
    return {"repo": repo.full_name, "token_set": True, "token_hint": mask(body.token)}


@app.get("/metrics")
async def metrics_endpoint(session: AsyncSession = Depends(get_session)) -> Response:
    """Prometheus text exposition, derived from the same numbers as the dashboard."""
    data = await analytics.dashboard(session)
    lines: list[str] = []

    def add(name: str, help_text: str, samples: list[tuple[str, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            lines.append(f"{name}{labels} {value}")

    add(
        "pr_runs_total",
        "Runs by status",
        [(f'{{status="{status}"}}', count) for status, count in data["runs"]["by_status"].items()],
    )
    add(
        "pr_findings_total",
        "Findings by severity",
        [
            (f'{{severity="{severity}"}}', count)
            for severity, count in data["findings_by_severity"].items()
        ],
    )
    add(
        "pr_run_latency_p95_ms",
        "P95 run latency by engine",
        [
            (f'{{engine="{engine}"}}', stats["p95"])
            for engine, stats in data["runs"]["latency_ms"]["by_engine"].items()
        ],
    )
    add("pr_cost_usd_total", "Estimated model cost", [("", round(sum(r["cost_usd"] for r in data["cost_by_repo"]), 6))])
    add(
        "pr_false_positive_rate",
        "Share of feedback marked false_positive",
        [("", data["feedback"]["false_positive_rate"])],
    )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    return await service.overview(session)


@app.get("/api/dashboard")
async def dashboard_data(session: AsyncSession = Depends(get_session)) -> dict:
    return await analytics.dashboard(session)


@app.get("/api/feedback/clusters")
async def feedback_clusters(session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await analytics.feedback_clusters(session)}


class CandidateRequest(BaseModel):
    rule_key: str = Field(min_length=2)
    note: str = ""
    owner: str = ""
    action_hint: str = "review-rule"


class DecisionRequest(BaseModel):
    status: str
    owner: str = ""


@app.post("/api/rules/candidates", status_code=201)
async def create_candidate(
    body: CandidateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    candidate, created = await service.propose_rule_candidate(
        session, body.rule_key, body.note, body.owner, body.action_hint
    )
    return {
        "id": candidate.id,
        "rule_key": candidate.rule_key,
        "status": candidate.status,
        "created": created,
    }


@app.get("/api/rules/candidates")
async def list_candidates(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    items = await service.list_rule_candidates(session, status)
    return {
        "items": [
            {
                "id": c.id,
                "rule_key": c.rule_key,
                "status": c.status,
                "action_hint": c.action_hint,
                "owner": c.owner,
                "note": c.note,
                "created_at": c.created_at,
                "decided_at": c.decided_at,
            }
            for c in items
        ]
    }


@app.post("/api/rules/candidates/{candidate_id}/decision")
async def decide_candidate(
    candidate_id: int,
    body: DecisionRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        candidate = await service.decide_rule_candidate(
            session, candidate_id, body.status, body.owner
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": candidate.id, "status": candidate.status, "decided_at": candidate.decided_at}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(session: AsyncSession = Depends(get_session)) -> str:
    data = await analytics.dashboard(session)
    return _render_dashboard(data)


def _render_dashboard(data: dict) -> str:
    """Server-rendered dashboard: no npm, no build, no client state to trust."""
    import html as html_mod

    def esc(value) -> str:
        return html_mod.escape(str(value))

    severity_rows = "".join(
        f"<tr><td>{esc(k)}</td><td class='num'>{v}</td></tr>"
        for k, v in sorted(data["findings_by_severity"].items())
    ) or "<tr><td colspan=2 class='muted'>no findings yet</td></tr>"

    cost_rows = "".join(
        f"<tr><td>{esc(r['repo'])}</td><td class='num'>{r['runs']}</td>"
        f"<td class='num'>{r['tokens_in']}/{r['tokens_out']}</td>"
        f"<td class='num'>${r['cost_usd']:.4f}</td></tr>"
        for r in data["cost_by_repo"]
    ) or "<tr><td colspan=4 class='muted'>no runs yet</td></tr>"

    feedback = data["feedback"]
    verdict_bits = ", ".join(f"{esc(k)}={v}" for k, v in feedback["by_verdict"].items()) or "none"
    cluster_rows = "".join(
        f"<tr><td>{esc(c['rule_key'])}</td><td class='num'>{c['feedback_count']}</td>"
        f"<td>{esc(c['action_hint'])}</td></tr>"
        for c in data["clusters"]
    ) or "<tr><td colspan=3 class='muted'>no false positives recorded</td></tr>"

    latency = data["runs"]["latency_ms"]
    latency_rows = "".join(
        f"<tr><td>{esc(engine)}</td><td class='num'>{v['p50']:.0f}</td>"
        f"<td class='num'>{v['p95']:.0f}</td><td class='num'>{v['count']}</td></tr>"
        for engine, v in latency["by_engine"].items()
    ) or "<tr><td colspan=4 class='muted'>no completed runs</td></tr>"
    engines = ", ".join(f"{esc(k)}={v}" for k, v in data["runs"]["engines"].items()) or "none"

    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>PR Review Platform · Dashboard</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:32px}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#8b8f98;margin-bottom:24px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}
 .card{{background:#171a21;border:1px solid #232838;border-radius:10px;padding:16px}}
 .card h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:#8b8f98;margin:0 0 12px}}
 table{{width:100%;border-collapse:collapse}} td{{padding:6px 10px 6px 0;border-bottom:1px solid #232838}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums}}
 .muted{{color:#6b7280}} .big{{font-size:26px;font-weight:600}}
</style></head><body>
<h1>PR Review Platform</h1>
<div class="sub">repo={data['repos']} · runs={data['runs']['total']} · engines={engines}</div>
<div class="grid">
  <div class="card"><h2>Latency by engine (ms)</h2><table>
    <tr><td class="muted">engine</td><td class="muted num">p50</td><td class="muted num">p95</td><td class="muted num">n</td></tr>
    {latency_rows}</table></div>
  <div class="card"><h2>Cost</h2><div class="big">${sum(r['cost_usd'] for r in data['cost_by_repo']):.4f}</div>
    <div class="muted">tokens in/out 见下表</div></div>
  <div class="card"><h2>False-positive rate</h2><div class="big">{feedback['false_positive_rate']:.1%}</div>
    <div class="muted">{verdict_bits}</div></div>
</div>
<div class="grid" style="margin-top:16px">
  <div class="card"><h2>Findings by severity</h2><table>{severity_rows}</table></div>
  <div class="card"><h2>Cost by repo</h2><table>
    <tr><td class="muted">repo</td><td class="muted num">runs</td><td class="muted num">tok in/out</td><td class="muted num">usd</td></tr>
    {cost_rows}</table></div>
  <div class="card"><h2>False-positive clusters</h2><table>
    <tr><td class="muted">rule</td><td class="muted num">hits</td><td class="muted">action</td></tr>
    {cluster_rows}</table></div>
</div>
</body></html>"""


def _run_dict(run: Run) -> dict:
    return {
        "id": run.id,
        "repo": run.repo.full_name if run.repo else None,
        "pr_number": run.pr_number,
        "head_sha": run.head_sha,
        "trigger": run.trigger,
        "status": run.status,
        "latency_ms": run.latency_ms,
        "stats": run.stats,
        "error": run.error,
        "error_type": run.error_type,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
    }


def _finding_dict(finding: Finding) -> dict:
    return {
        "id": finding.id,
        "file_path": finding.file_path,
        "line_start": finding.line_start,
        "line_end": finding.line_end,
        "severity": finding.severity,
        "rule_key": finding.rule_key,
        "message": finding.message,
        "evidence": finding.evidence,
        "posted": finding.posted,
        "resolved": finding.resolved,
        "resolved_at": finding.resolved_at,
        "feedback": [
            {"verdict": f.verdict, "actor": f.actor, "note": f.note} for f in finding.feedbacks
        ],
    }


__all__ = ["app", "Repo", "Feedback"]
