"""P0 contract tests.

Each test pins one promise the platform makes to its users:

* a forged webhook is rejected (security)
* a retried delivery does not produce a second review (idempotency)
* a finding lands on the *right* line (position correctness)
* re-reviewing the same PR does not spam the same comment (dedupe)
* feedback is stored as data, not discarded (improvement loop)
"""

from __future__ import annotations

import json

from apps.worker.main import run_once
from core.rules import load_rules
from core.review import iter_added_lines

from conftest import ROOT, delivery_headers, pr_payload
PY_DIFF = """diff --git a/app/demo.py b/app/demo.py
--- a/app/demo.py
+++ b/app/demo.py
@@ -1,3 +1,5 @@
 import os
+api_key = "demo-key-1234567890abcdef"
+result = eval(user_input)
+subprocess.run(cmd, shell=True)
 print("ok")
"""

TS_DIFF = """diff --git a/web/widget.tsx b/web/widget.tsx
--- a/web/widget.tsx
+++ b/web/widget.tsx
@@ -10,2 +10,4 @@ export function Widget() {
   return (
+    <div dangerouslySetInnerHTML={{ __html: html }} />
+    <span>{eval(code)}</span>
   );
"""


async def test_forged_signature_is_rejected(client):
    payload = pr_payload()
    resp = await client.post(
        "/webhooks/github",
        content=json.dumps(payload),
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-forged",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


async def test_webhook_creates_run_and_is_idempotent(client, sign):
    payload = pr_payload(repo="acme/idem", pr=7)
    headers, body = delivery_headers(sign, payload, "d-idem-1")

    first = await client.post("/webhooks/github", content=body, headers=headers)
    assert first.status_code == 202
    run_id = first.json()["run_id"]

    second = await client.post("/webhooks/github", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    detail = await client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "queued"
    assert detail.json()["repo"] == "acme/idem"


async def test_replay_reviews_python_diff_and_maps_lines(client):
    resp = await client.post(
        "/api/replay",
        json={
            "repo_full_name": "acme/py-demo",
            "pr_number": 11,
            "diff": PY_DIFF,
            "head_sha": "c" * 40,
        },
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    processed = await run_once()
    assert processed == run_id

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] == "completed"
    assert detail["stats"]["engine"] == "rules"
    assert detail["stats"]["cost_usd"] == 0.0

    findings = {f["rule_key"]: f for f in detail["findings"]}
    assert set(findings) == {"py-hardcoded-secret", "py-eval-usage", "py-shell-true"}
    assert findings["py-hardcoded-secret"]["line_start"] == 2
    assert findings["py-eval-usage"]["line_start"] == 3
    assert findings["py-shell-true"]["line_start"] == 4
    assert findings["py-eval-usage"]["file_path"] == "app/demo.py"


async def test_second_review_of_same_pr_suppresses_duplicate_comments(client):
    payload = {"repo_full_name": "acme/dedupe", "pr_number": 21, "diff": PY_DIFF}
    first = (await client.post("/api/replay", json={**payload, "head_sha": "1" * 40})).json()
    await run_once()

    second = (await client.post("/api/replay", json={**payload, "head_sha": "2" * 40})).json()
    await run_once()

    detail = (await client.get(f"/api/runs/{second['run_id']}")).json()
    assert detail["status"] == "completed"
    assert detail["stats"]["findings"] == 3
    assert detail["stats"]["new_findings"] == 0
    assert detail["stats"]["suppressed_duplicates"] == 3

    first_detail = (await client.get(f"/api/runs/{first['run_id']}")).json()
    assert first_detail["stats"]["new_findings"] == 3


async def test_feedback_is_recorded_on_finding(client):
    resp = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/fb", "pr_number": 31, "diff": PY_DIFF, "head_sha": "3" * 40},
    )
    run_id = resp.json()["run_id"]
    await run_once()

    findings = (await client.get(f"/api/runs/{run_id}/findings")).json()["items"]
    target = findings[0]["id"]
    fb = await client.post(
        f"/api/findings/{target}/feedback",
        json={"verdict": "false_positive", "actor": "bob", "note": "这个 key 是测试用的"},
    )
    assert fb.status_code == 200

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    updated = next(f for f in detail["findings"] if f["id"] == target)
    assert updated["feedback"][0]["verdict"] == "false_positive"


async def test_stats_endpoint_aggregates(client):
    # Self-contained: the fixture truncates tables, so create the data this
    # assertion is about instead of relying on other tests having run.
    resp = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/stats", "pr_number": 41, "diff": PY_DIFF, "head_sha": "4" * 40},
    )
    assert resp.status_code == 202
    await run_once()

    stats = await client.get("/api/stats")
    assert stats.status_code == 200
    data = stats.json()
    assert data["runs"]["total"] >= 1
    assert data["findings"]["total"] >= 1
    assert "p95" in data["latency_ms"]
    assert "cost_usd" in data["cost"]


async def test_diff_line_numbers_are_new_file_coordinates():
    lines = iter_added_lines(PY_DIFF)
    assert [(line.path, line.line_no) for line in lines] == [
        ("app/demo.py", 2),
        ("app/demo.py", 3),
        ("app/demo.py", 4),
    ]


async def test_typescript_rules_apply():
    from core.adapters.reviewer import ReviewContext, RuleReviewer

    rules = load_rules(ROOT / "rules")
    reviewer = RuleReviewer(rules)
    result = await reviewer.review(
        ReviewContext(
            repo_full_name="acme/ts",
            pr_number=1,
            base_sha="b",
            head_sha="h",
            diff_text=TS_DIFF,
        )
    )
    keys = {f.rule_key for f in result.findings}
    assert keys == {"ts-dangerously-set-inner-html", "ts-eval-usage"}


async def test_rule_fixtures_guard_against_drift():
    """Every rule ships positive/negative samples; they must keep passing."""
    rules = {rule.key: rule for rule in load_rules(ROOT / "rules") if rule.pattern}
    for key, rule in rules.items():
        positive = (rule.fixtures or {}).get("positive")
        negative = (rule.fixtures or {}).get("negative")
        regex = rule.compiled()
        assert regex is not None, key
        if positive:
            assert regex.search(positive), f"{key}: positive fixture no longer matches"
        if negative:
            assert not regex.search(negative), f"{key}: negative fixture now matches"
