"""P3 tests: comment delivery wiring and the rule-candidate state machine.

The posting test uses an injected transport (a fake ``post_review``) instead of
GitHub: the contract we care about is "what payload would we send, with which
token, and do findings get marked posted".  That contract is what breaks when
someone refactors, and it does not need a live token to verify.
"""

from __future__ import annotations

from apps.worker.main import run_once
from conftest import ROOT

PY_DIFF = """diff --git a/app/demo.py b/app/demo.py
--- a/app/demo.py
+++ b/app/demo.py
@@ -1,2 +1,3 @@
 import os
+result = eval(user_input)
"""


class FakeSettings:
    ocr_mode = "fake"
    rules_path = ROOT / "rules"
    max_findings_per_file = 10
    github_dry_run = False
    github_token = "fake-token"
    github_api_base = "https://api.example.test"


async def _make_run(client, repo: str, pr: int, sha: str) -> int:
    resp = await client.post(
        "/api/replay",
        json={"repo_full_name": repo, "pr_number": pr, "diff": PY_DIFF, "head_sha": sha},
    )
    assert resp.status_code == 202
    return resp.json()["run_id"]


async def test_dry_run_records_but_does_not_post(client):
    run_id = await _make_run(client, "acme/dry", 71, "7" * 40)
    await run_once()

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["stats"]["comment_status"] == "skipped"
    assert detail["stats"]["dry_run"] is True
    assert all(finding["posted"] is False for finding in detail["findings"])


async def test_posting_sends_one_review_and_marks_findings(client, monkeypatch):
    captured: dict = {}

    async def fake_post(repo, pr, payload, token, api_base):
        captured.update(repo=repo, pr=pr, payload=payload, token=token, api_base=api_base)
        return {"id": 123}

    monkeypatch.setattr("core.service.post_review", fake_post)
    monkeypatch.setattr("core.service.get_settings", lambda: FakeSettings())

    run_id = await _make_run(client, "acme/post", 72, "8" * 40)
    await run_once()

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] == "completed"
    assert detail["stats"]["comment_status"] == "posted"
    assert all(finding["posted"] is True for finding in detail["findings"])

    assert captured["repo"] == "acme/post"
    assert captured["pr"] == 72
    assert captured["token"] == "fake-token"
    assert captured["payload"]["commit_id"] == "8" * 40
    assert len(captured["payload"]["comments"]) == len(detail["findings"])


async def test_posting_failure_keeps_findings_and_records_status(client, monkeypatch):
    from core.adapters.github import GitHubError

    async def failing_post(*args, **kwargs):
        raise GitHubError("HTTP 502 from GitHub")

    monkeypatch.setattr("core.service.post_review", failing_post)
    monkeypatch.setattr("core.service.get_settings", lambda: FakeSettings())

    run_id = await _make_run(client, "acme/postfail", 73, "9" * 40)
    await run_once()

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    # A failed notification must not lose the review.
    assert detail["status"] == "completed"
    assert detail["findings"]
    assert detail["stats"]["comment_status"].startswith("failed")
    assert all(finding["posted"] is False for finding in detail["findings"])


async def test_post_run_delivers_findings_that_were_never_posted(client, monkeypatch):
    """A dry-run (or failed delivery) must be replayable without re-reviewing."""
    run_id = await _make_run(client, "acme/replay-post", 75, "b" * 40)
    await run_once()
    before = (await client.get(f"/api/runs/{run_id}")).json()
    assert before["stats"]["comment_status"] == "skipped"
    assert all(finding["posted"] is False for finding in before["findings"])

    captured: dict = {}

    async def fake_post(repo, pr, payload, token, api_base):
        captured.update(payload=payload, repo=repo)
        return {"id": 1}

    monkeypatch.setattr("core.service.post_review", fake_post)
    monkeypatch.setattr("core.service.get_settings", lambda: FakeSettings())

    resp = await client.post(f"/api/runs/{run_id}/post")
    assert resp.status_code == 200
    assert resp.json()["posted"] == len(before["findings"])
    assert len(captured["payload"]["comments"]) == len(before["findings"])

    after = (await client.get(f"/api/runs/{run_id}")).json()
    assert after["stats"]["comment_status"] == "posted"
    assert all(finding["posted"] is True for finding in after["findings"])

    again = await client.post(f"/api/runs/{run_id}/post")
    assert again.json()["posted"] == 0


async def test_rule_candidate_lifecycle(client):
    run_id = await _make_run(client, "acme/candidate", 74, "a" * 40)
    await run_once()
    findings = (await client.get(f"/api/runs/{run_id}/findings")).json()["items"]
    rule_key = findings[0]["rule_key"]

    for finding in findings:
        await client.post(
            f"/api/findings/{finding['id']}/feedback",
            json={"verdict": "false_positive", "actor": "dave", "note": "noise"},
        )

    clusters = (await client.get("/api/feedback/clusters")).json()["items"]
    assert any(c["rule_key"] == rule_key for c in clusters)

    created = await client.post(
        "/api/rules/candidates", json={"rule_key": rule_key, "note": "tighten path filter"}
    )
    assert created.status_code == 201
    candidate_id = created.json()["id"]
    assert created.json()["status"] == "draft"

    duplicate = await client.post("/api/rules/candidates", json={"rule_key": rule_key})
    assert duplicate.json()["created"] is False
    assert duplicate.json()["id"] == candidate_id

    listed = (await client.get("/api/rules/candidates?status=draft")).json()["items"]
    assert len(listed) == 1

    approved = await client.post(
        f"/api/rules/candidates/{candidate_id}/decision",
        json={"status": "approved", "owner": "platform"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    again = await client.post(
        f"/api/rules/candidates/{candidate_id}/decision", json={"status": "rejected"}
    )
    assert again.status_code == 409

    approved_list = (await client.get("/api/rules/candidates?status=approved")).json()["items"]
    assert any(item["id"] == candidate_id and item["owner"] == "platform" for item in approved_list)
