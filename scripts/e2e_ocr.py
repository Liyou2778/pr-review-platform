"""P1 end-to-end: git workspace -> upstream OCR (real model) -> findings in DB.

Runs on a temporary SQLite database so it is independent from the compose
stack.  It exercises ``process_run`` exactly as the worker does, including
claiming the run and dedupe, so a green run here means P1 is wired.

Usage:  python scripts/e2e_ocr.py [--repo tools/ocrdemo] [--from HEAD~1] [--to HEAD]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from core.tmp import workdir  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

_tmp_db = workdir("e2e-ocr-") / "e2e.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp_db.as_posix()}"
os.environ["QUEUE_BACKEND"] = "db"
os.environ["OCR_MODE"] = "cli"
os.environ["APP_ENV"] = "test"  # NullPool, no dev warnings noise

from core.db import SessionLocal, engine, init_db  # noqa: E402
from core.models import Repo, Run  # noqa: E402
from core.service import claim_run, process_run  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="tools/ocrdemo")
    parser.add_argument("--from", dest="base", default="HEAD~1")
    parser.add_argument("--to", dest="head", default="HEAD")
    args = parser.parse_args()

    demo = (ROOT / args.repo).resolve()
    if not (demo / ".git").exists():
        print(f"not a git repo: {demo}", file=sys.stderr)
        return 2

    base = git(demo, "rev-parse", args.base)
    head = git(demo, "rev-parse", args.head)
    print(f"repo={demo}\nbase={base[:8]} head={head[:8]}")

    await init_db()
    async with SessionLocal() as session:
        repo = Repo(full_name="acme/ocr-demo", clone_url=str(demo))
        session.add(repo)
        await session.flush()
        run = Run(
            repo_id=repo.id,
            pr_number=9,
            base_sha=base,
            head_sha=head,
            trigger="replay",
            status="queued",
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    print(f"run_id={run_id} claimed={await claim_run(run_id)}")
    stats = await process_run(run_id)
    print(json.dumps(stats, ensure_ascii=False, indent=2)[:1200])

    async with SessionLocal() as session:
        from sqlalchemy import select

        from core.models import Finding

        rows = (
            await session.execute(select(Finding).where(Finding.run_id == run_id))
        ).scalars().all()
        print(f"persisted findings={len(rows)}")
        for row in rows:
            print(
                f"  [{row.severity:<7}] {row.rule_key:<10} {row.file_path}:{row.line_start} "
                f"{row.message[:60]}"
            )

    await engine.dispose()
    ok = stats.get("engine") == "ocr" and stats.get("findings", 0) >= 1
    print("E2E:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
