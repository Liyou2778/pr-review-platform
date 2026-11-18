"""Ops hardening tests: rate limits, severity policy, resolved state, retries,
metrics, retention and request tracing.

These cover the "can someone else run this" layer, not the review logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import ROOT

from apps.worker.main import run_once
from core.config import Settings
from core.db import SessionLocal
from core.models import CostRecord, Finding, Repo, Run
from core.retention import cutoff_iso, prune

FIX = """diff --git a/app/x.py b/app/x.py
--- a/app/x.py
+++ b/app/x.py
@@ -1,1 +1,2 @@
 import os
+result = eval(user_input)
"""

TWO_LEVELS = """diff --git a/app/m.py b/app/m.py
--- a/app/m.py
+++ b/app/m.py
@@ -1,1 +1,2 @@
 def calc(x):
+    return eval(x)
@@ -10,1 +10,2 @@
 def load(p):
+        except:
"""

CLEAN = """diff --git a/app/x.py b/app/x.py
--- a/app/x.py
+++ b/app/x.py
@@ -1,1 +1,2 @@
 import os
+x = 1
"""


class OpsSettings:
    ocr_mode = "fake"
    rules_path = ROOT / "rules"
    max_findings_per_file = 10
    max_diff_bytes = 2_000_000
    github_dry_run = False
    github_token = "tok"
    github_api_base = "https://api.example.test"
    post_retries = 3
    post_retry_base_s = 0.01
    repo_hourly_run_limit = 0


async def _replay(client, repo: str, pr: int, sha: str, diff: str):
    return await client.post(
        "/api/replay",
        json={"repo_full_name": repo, "pr_number": pr, "diff": diff, "head_sha": sha},
    )


async def _drain(limit: int = 6) -> None:
    for _ in range(limit):
        if await run_once() is None:
            break


async def test_repo_hourly_limit_returns_429(client):
    first = await _replay(client, "acme/rate", 1, "1" * 40, FIX)
    assert first.status_code == 202

    policy = await client.put("/api/repos/acme/rate/policy", json={"max_runs_per_hour": 1})
    assert policy.status_code == 200
    assert policy.json()["max_runs_per_hour"] == 1

    second = await _replay(client, "acme/rate", 2, "2" * 40, FIX)
    assert second.status_code == 429


async def test_min_severity_filters_delivery_but_keeps_findings(client, monkeypatch):
    captured: dict = {}

    async def fake_post(repo, pr, payload, token, api_base):
        captured["payload"] = payload
        return {"id": 1}

    monkeypatch.setattr("core.service.post_review", fake_post)
    monkeypatch.setattr("core.service.get_settings", lambda: OpsSettings())

    created = await _replay(client, "acme/sev", 3, "3" * 40, TWO_LEVELS)
    assert created.status_code == 202
    run_id = created.json()["run_id"]

    await client.put("/api/repos/acme/sev/policy", json={"min_severity": "high"})
    # First run already got queued before the policy; set it before running:
    # re-queue a fresh run so the policy applies to delivery.
    policy = await client.put("/api/repos/acme/sev/policy", json={"min_severity": "high"})
    assert policy.json()["min_severity"] == "high"
    await _drain()

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    # Both findings are stored…
    assert len(detail["findings"]) == 2
    # …but only the high one is delivered.
    delivered = captured["payload"]["comments"]
    assert len(delivered) == 1
    assert "py-eval-usage" in delivered[0]["body"]


async def test_resolved_state_marks_findings_fixed_in_later_run(client):
    first = await _replay(client, "acme/resolved", 4, "4" * 40, FIX)
    await _drain()
    run1 = (await client.get(f"/api/runs/{first.json()['run_id']}")).json()
    assert run1["findings"] and run1["findings"][0]["resolved"] is False

    # Same PR, new head, code fixed: the previous finding must flip to resolved.
    await _replay(client, "acme/resolved", 4, "5" * 40, CLEAN)
    await _drain()

    run1_after = (await client.get(f"/api/runs/{first.json()['run_id']}")).json()
    assert run1_after["findings"][0]["resolved"] is True
    assert run1_after["findings"][0]["resolved_at"]


async def test_delivery_retries_with_backoff(client, monkeypatch):
    from core.adapters.github import GitHubError

    calls = {"n": 0}

    async def flaky_post(repo, pr, payload, token, api_base):
        calls["n"] += 1
        if calls["n"] < 3:
            raise GitHubError("HTTP 503 from GitHub")
        return {"id": 1}

    monkeypatch.setattr("core.service.post_review", flaky_post)
    monkeypatch.setattr("core.service.get_settings", lambda: OpsSettings())

    created = await _replay(client, "acme/retry", 5, "6" * 40, FIX)
    run_id = created.json()["run_id"]
    await _drain()

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["stats"]["comment_status"] == "posted"
    assert detail["stats"]["delivery_attempts"] == 3
    assert calls["n"] == 3


async def test_metrics_endpoint_exposes_prometheus_text(client):
    await _replay(client, "acme/metrics", 6, "7" * 40, FIX)
    await _drain()

    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "pr_runs_total" in resp.text
    assert "pr_findings_total" in resp.text
    assert "pr_run_latency_p95_ms" in resp.text


async def test_request_id_header_is_echoed(client):
    resp = await client.get("/healthz")
    assert resp.headers.get("X-Request-Id")

    sent = await client.get("/healthz", headers={"X-Request-Id": "abc123"})
    assert sent.headers["X-Request-Id"] == "abc123"


async def test_dashboard_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(
        "apps.api.main.get_settings", lambda: Settings(api_token="s3cret", env="test")
    )
    assert (await client.get("/dashboard")).status_code == 401
    assert (await client.get("/dashboard", headers={"X-API-Token": "s3cret"})).status_code == 200


async def test_retention_prunes_old_runs_only(client):
    async with SessionLocal() as session:
        repo = Repo(full_name="acme/retention")
        session.add(repo)
        await session.flush()
        old = Run(
            repo_id=repo.id,
            pr_number=1,
            head_sha="a" * 40,
            status="completed",
            created_at=(datetime.now(UTC) - timedelta(days=90)).replace(microsecond=0).isoformat(),
        )
        fresh = Run(repo_id=repo.id, pr_number=2, head_sha="b" * 40, status="completed")
        session.add_all([old, fresh])
        await session.flush()
        session.add(Finding(run_id=old.id, file_path="a.py", line_start=1, severity="low", rule_key="x", message="m", dedup_key="k1"))
        session.add(CostRecord(run_id=old.id, repo_full_name="acme/retention", cost_usd=0.01))
        await session.commit()
        old_id, fresh_id = old.id, fresh.id

    async with SessionLocal() as session:
        counts = await prune(session, cutoff_iso(30), dry_run=False)
    assert counts["runs"] >= 1
    assert counts["findings"] >= 1

    async with SessionLocal() as session:
        assert await session.get(Run, old_id) is None
        assert await session.get(Run, fresh_id) is not None
