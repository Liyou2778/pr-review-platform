"""Test bootstrap.

Environment is configured *before* any project import so that
``core.config.get_settings()`` (cached) sees the test values.  Tests run on
SQLite + in-memory queue: the platform's contracts (idempotency, dedupe,
line numbers) are storage-agnostic, so this is a valid fast lane.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.tmp import workdir

ROOT = Path(__file__).resolve().parents[1]
TMP = workdir("tests-")

os.environ["APP_ENV"] = "test"
# Default fast lane is SQLite; set TEST_DATABASE_URL to run the same contract
# tests against Postgres. Use `or` rather than a default argument: GitHub
# Actions sets an *empty* string for the SQLite matrix entry, and
# `os.environ.get(key, default)` would happily return "". Strip whitespace too.
_test_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
os.environ["DATABASE_URL"] = _test_url or (
    f"sqlite+aiosqlite:///{(TMP / 'test.db').as_posix()}"
)
os.environ["QUEUE_BACKEND"] = "memory"
os.environ["RULES_DIR"] = str(ROOT / "rules")
os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
os.environ["GITHUB_DRY_RUN"] = "true"
os.environ["OCR_MODE"] = "fake"
os.environ["FERNET_KEY"] = ""

import httpx  # noqa: E402
import pytest  # noqa: E402

from core.queue import reset_queue_for_tests  # noqa: E402
from core.security import compute_signature  # noqa: E402
from core.tmp import workdir  # noqa: E402

_initialized = False


async def _truncate_all() -> None:
    """Isolate tests: contract tests assume each starts from an empty DB."""
    from sqlalchemy import delete

    from core.db import SessionLocal
    from core.models import CostRecord, Delivery, Feedback, Finding, Repo, Run

    async with SessionLocal() as session:
        for model in (Feedback, Finding, CostRecord, Run, Delivery, Repo):
            await session.execute(delete(model))
        await session.commit()


@pytest.fixture
async def client():
    global _initialized
    from apps.api.main import app
    from core.db import init_db

    if not _initialized:
        await init_db()
        _initialized = True
    await _truncate_all()
    reset_queue_for_tests()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def sign():
    def _sign(body: bytes) -> str:
        return compute_signature(os.environ["GITHUB_WEBHOOK_SECRET"], body)

    return _sign


def pr_payload(
    repo: str = "acme/demo",
    pr: int = 1,
    head_sha: str = "a" * 40,
    base_sha: str = "b" * 40,
    action: str = "opened",
) -> dict:
    return {
        "action": action,
        "number": pr,
        "repository": {"full_name": repo, "default_branch": "main", "clone_url": f"https://x/{repo}.git"},
        "pull_request": {
            "number": pr,
            "title": "feat: demo",
            "base": {"sha": base_sha, "ref": "main"},
            "head": {"sha": head_sha, "ref": "feature"},
        },
        "sender": {"login": "alice"},
    }


def delivery_headers(sign, payload: dict, delivery_id: str) -> dict:
    body = json.dumps(payload).encode("utf-8")
    return {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sign(body),
        "Content-Type": "application/json",
    }, body
