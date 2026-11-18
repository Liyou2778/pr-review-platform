"""Hardening tests found during the cross-check review.

Three issues that only show up in production-ish conditions:
* a second worker stealing in-flight jobs on boot,
* re-reading rules from disk on every run,
* an unbounded diff OOM-ing the worker.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from conftest import ROOT

from apps.worker.main import recover_stale, run_once
from core import rules as rules_mod
from core.db import SessionLocal
from core.models import Repo, Run
from core.tmp import workdir

SMALL_DIFF = """diff --git a/app/x.py b/app/x.py
--- a/app/x.py
+++ b/app/x.py
@@ -1,1 +1,2 @@
 import os
+result = eval(user_input)
"""


def _iso(delta_seconds: int = 0) -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=delta_seconds)
    ).replace(microsecond=0).isoformat()


async def test_recover_stale_skips_fresh_runs(client):  # client ensures schema + clean tables
    async with SessionLocal() as session:
        repo = Repo(full_name="acme/staleness")
        session.add(repo)
        await session.flush()
        fresh = Run(
            repo_id=repo.id, pr_number=1, head_sha="f" * 40, status="running", started_at=_iso()
        )
        old = Run(
            repo_id=repo.id,
            pr_number=2,
            head_sha="o" * 40,
            status="running",
            started_at=_iso(-7200),
        )
        session.add_all([fresh, old])
        await session.commit()
        fresh_id, old_id = fresh.id, old.id

    recovered = await recover_stale(stale_after_s=900)
    assert recovered >= 1

    async with SessionLocal() as session:
        assert (await session.get(Run, fresh_id)).status == "running"  # not stolen
        assert (await session.get(Run, old_id)).status == "queued"  # orphan recovered


async def test_rule_cache_avoids_reparse_and_invalidates_on_touch():
    root = workdir("rules-")
    (root / "r.yaml").write_text(
        "version: 1\nowner: test\nrules:\n"
        "  - key: demo\n    severity: low\n    pattern: 'eval\\('\n    message: demo\n",
        encoding="utf-8",
    )
    rules_mod.clear_rule_cache()

    first = rules_mod.load_rules(root)
    second = rules_mod.load_rules(root)
    assert first is second, "unchanged rules should come from cache"

    # Bump mtime into the future so the cache key changes on any filesystem.
    future = datetime.now().timestamp() + 120
    os.utime(root / "r.yaml", (future, future))
    third = rules_mod.load_rules(root)
    assert third is not first, "touched rules must reload"
    assert [rule.key for rule in third] == ["demo"]


async def test_oversized_diff_fails_fast_with_typed_error(client, monkeypatch):
    class TinyLimitSettings:
        ocr_mode = "fake"
        rules_path = ROOT / "rules"
        max_findings_per_file = 10
        max_diff_bytes = 10
        github_dry_run = True
        github_token = ""
        github_api_base = "https://api.github.com"

    monkeypatch.setattr("core.service.get_settings", lambda: TinyLimitSettings())

    resp = await client.post(
        "/api/replay",
        json={"repo_full_name": "acme/bigdiff", "pr_number": 81, "diff": SMALL_DIFF, "head_sha": "d" * 40},
    )
    run_id = resp.json()["run_id"]
    await run_once()

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] == "failed"
    assert detail["error_type"] == "diff_too_large"
    assert detail["stats"]["error_type"] == "diff_too_large"
