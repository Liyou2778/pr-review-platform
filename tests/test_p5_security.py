"""P0 security hardening tests.

Covers the four items from the cross-check review:
1. mutating API requires X-API-Token, and never silently opens in prod;
2. request body size cap;
3. clone_url scheme/host allow-list (SSRF);
4. repo-scoped token beats the process token;
plus the prod boot validation itself.
"""

from __future__ import annotations

from conftest import ROOT
from cryptography.fernet import Fernet

from apps.worker.main import run_once
from core.config import Settings

DIFF = """diff --git a/app/x.py b/app/x.py
--- a/app/x.py
+++ b/app/x.py
@@ -1,1 +1,2 @@
 import os
+result = eval(user_input)
"""


class DeliverySettings:
    ocr_mode = "fake"
    rules_path = ROOT / "rules"
    max_findings_per_file = 10
    max_diff_bytes = 2_000_000
    github_dry_run = False
    github_token = "global-token"
    github_api_base = "https://api.example.test"
    post_retries = 3
    post_retry_base_s = 0.01
    repo_hourly_run_limit = 0


async def test_mutating_api_requires_token(client, monkeypatch):
    monkeypatch.setattr(
        "apps.api.main.get_settings", lambda: Settings(api_token="s3cret", env="test")
    )

    denied = await client.post("/api/replay", json={"repo_full_name": "acme/sec", "pr_number": 1})
    assert denied.status_code == 401

    allowed = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/sec", "pr_number": 1, "diff": DIFF, "head_sha": "1" * 40},
        headers={"X-API-Token": "s3cret"},
    )
    assert allowed.status_code == 202

    # Reads stay open: the dashboard is not a mutation surface.
    assert (await client.get("/healthz")).status_code == 200


async def test_body_limit_rejects_oversized_payload(client, monkeypatch):
    monkeypatch.setattr(
        "apps.api.main.get_settings", lambda: Settings(max_request_bytes=80, env="test")
    )
    resp = await client.post("/api/replay", json={"repo_full_name": "acme/big", "pr_number": 2, "diff": "x" * 500})
    assert resp.status_code == 413


async def test_clone_url_rejects_file_scheme_and_foreign_host(client):
    local_file = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/ssrf", "pr_number": 3, "clone_url": "file:///C:/Windows"},
    )
    assert local_file.status_code == 400
    assert "clone_url" in local_file.json()["detail"] or "file://" in local_file.json()["detail"]

    foreign = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/ssrf", "pr_number": 4, "clone_url": "https://evil.example.com/a/b"},
    )
    assert foreign.status_code == 400


async def test_repo_scoped_token_is_preferred_for_delivery(client, monkeypatch):
    capture: dict = {}

    async def fake_post(repo, pr, payload, token, api_base):
        capture["token"] = token
        return {"id": 1}

    monkeypatch.setattr("core.service.post_review", fake_post)
    monkeypatch.setattr("core.service.get_settings", lambda: DeliverySettings())

    # repo gets created by the first replay
    first = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/scoped", "pr_number": 5, "diff": DIFF, "head_sha": "2" * 40},
    )
    assert first.status_code == 202

    stored = await client.put(
        "/api/repos/acme/scoped/token", json={"token": "repo-token-123456"}
    )
    assert stored.status_code == 200
    assert stored.json()["token_hint"].endswith("*")  # masked, never echoed

    second = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/scoped", "pr_number": 5, "diff": DIFF, "head_sha": "3" * 40},
    )
    run_id = second.json()["run_id"]
    first_id = first.json()["run_id"]
    # The first replay left a queued run too; drain until the queue is empty.
    for _ in range(5):
        if await run_once() is None:
            break

    detail = (await client.get(f"/api/runs/{first_id}")).json()
    assert detail["stats"]["comment_status"] == "posted"
    assert capture["token"] == "repo-token-123456"

    # The second run sees the same findings already delivered by the first run.
    second_detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert second_detail["stats"]["comment_status"] == "skipped"


async def test_production_configuration_fails_fast():
    insecure = Settings(
        env="prod", api_token="", fernet_key="", github_webhook_secret="dev-secret"
    )
    problems = insecure.security_problems()
    assert any("API_TOKEN" in p for p in problems)
    assert any("WEBHOOK_SECRET" in p for p in problems)
    assert any("FERNET_KEY" in p for p in problems)

    import pytest

    with pytest.raises(RuntimeError, match="insecure configuration"):
        insecure.enforce_security()

    secure = Settings(
        env="prod",
        api_token="token",
        fernet_key=Fernet.generate_key().decode(),
        github_webhook_secret="a" * 32,
    )
    assert secure.security_problems() == []
