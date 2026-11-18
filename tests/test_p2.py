"""P2 tests: quality matching, feedback clusters, dashboard.

The matching test is the important one: every published F1 number is only
meaningful if the TP/FP/FN definition is pinned by a test.
"""

from __future__ import annotations

from apps.worker.main import run_once
from core.bench import TruthItem, load_cases, match

from conftest import ROOT

PY_DIFF = """diff --git a/app/demo.py b/app/demo.py
--- a/app/demo.py
+++ b/app/demo.py
@@ -1,3 +1,4 @@
 import os
+result = eval(user_input)
 print("ok")
"""


def test_match_counts_tp_fp_fn():
    truth = [
        TruthItem(file="a.py", line_start=2, line_end=2, rule_key="any"),
        TruthItem(file="a.py", line_start=9, line_end=9, rule_key="any"),
    ]
    findings = [
        {"file_path": "a.py", "line_start": 2, "line_end": 2, "rule_key": "security"},
        {"file_path": "a.py", "line_start": 20, "line_end": 20, "rule_key": "security"},
    ]
    result = match(findings, truth)
    assert (result.tp, result.fp, result.fn) == (1, 1, 1)
    assert result.precision == 0.5
    assert result.recall == 0.5
    assert result.f1 == 0.5


def test_match_uses_line_range_overlap_not_exact_line():
    truth = [TruthItem(file="a.py", line_start=10, line_end=14, rule_key="any")]
    findings = [{"file_path": "a.py", "line_start": 12, "line_end": 12, "rule_key": "bug"}]
    assert match(findings, truth).tp == 1


def test_match_does_not_reuse_one_truth_item_twice():
    truth = [TruthItem(file="a.py", line_start=5, line_end=5, rule_key="any")]
    findings = [
        {"file_path": "a.py", "line_start": 5, "line_end": 5, "rule_key": "a"},
        {"file_path": "a.py", "line_start": 5, "line_end": 5, "rule_key": "b"},
    ]
    result = match(findings, truth)
    assert (result.tp, result.fp) == (1, 1)


def test_bench_cases_file_is_valid_and_covers_all_languages():
    cases = load_cases(ROOT / "eval" / "bench" / "cases.json")
    assert len(cases) >= 20
    ids = {case.id for case in cases}
    assert {"py-secret", "ts-dangerous-html", "java-exec"} <= ids
    assert all(case.truth for case in cases)
    assert sum(len(case.truth) for case in cases) >= 20


async def test_feedback_clusters_group_false_positives_by_rule(client):
    resp = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/cluster", "pr_number": 51, "diff": PY_DIFF, "head_sha": "5" * 40},
    )
    run_id = resp.json()["run_id"]
    await run_once()

    findings = (await client.get(f"/api/runs/{run_id}/findings")).json()["items"]
    assert findings
    for finding in findings:
        await client.post(
            f"/api/findings/{finding['id']}/feedback",
            json={"verdict": "false_positive", "actor": "carol", "note": "测试代码，不适用"},
        )

    clusters = (await client.get("/api/feedback/clusters")).json()["items"]
    assert clusters
    top = clusters[0]
    assert top["feedback_count"] >= 1
    assert top["action_hint"] in ("review-rule", "collect-more-signal")


async def test_dashboard_json_and_html(client):
    await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/dash", "pr_number": 61, "diff": PY_DIFF, "head_sha": "6" * 40},
    )
    await run_once()

    data = (await client.get("/api/dashboard")).json()
    assert set(data) >= {"repos", "runs", "findings_by_severity", "cost_by_repo", "feedback", "clusters"}
    assert data["runs"]["total"] >= 1

    page = await client.get("/dashboard")
    assert page.status_code == 200
    assert "PR Review Platform" in page.text
    assert "False-positive" in page.text
