"""Environment smoke test for the OCR engine (real model, real repo).

Creates nothing: it reviews the last commit of ``tools/ocrdemo`` and prints the
normalised findings. Use it to verify that the CLI, the provider config and the
adapter agree before wiring P1 into the pipeline.

Usage:  python scripts/ocr_smoke.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env")

from core.adapters.reviewer import OcrReviewer, ReviewContext  # noqa: E402
from core.config import get_settings  # noqa: E402


def git(demo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(demo), *args]).decode().strip()


async def main() -> int:
    settings = get_settings()
    demo = Path("tools/ocrdemo").resolve()
    if not (demo / ".git").exists():
        print(f"missing demo repo: {demo}", file=sys.stderr)
        return 2

    head = git(demo, "rev-parse", "HEAD")
    base = git(demo, "rev-parse", "HEAD~1")
    print(f"repo={demo}\nbase={base[:8]} head={head[:8]}")

    reviewer = OcrReviewer(
        command=settings.ocr_command,
        timeout_s=settings.ocr_timeout_s,
        ocr_home=settings.ocr_home,
        price_in_per_1m=settings.llm_price_in_per_1m,
        price_out_per_1m=settings.llm_price_out_per_1m,
    )
    result = await reviewer.review(
        ReviewContext(
            repo_full_name="acme/ocr-demo",
            pr_number=1,
            base_sha=base,
            head_sha=head,
            diff_text="",
            workspace_path=str(demo),
        )
    )

    print(f"engine={result.engine} tokens_in={result.tokens_in} tokens_out={result.tokens_out} cost=${result.cost_usd}")
    print(f"meta={result.meta}")
    print(f"findings={len(result.findings)}")
    for finding in result.findings:
        print(
            f"  [{finding.severity:<8}] {finding.rule_key:<12} {finding.file_path}:{finding.line_start} "
            f"{finding.message[:70]}"
        )
    return 0 if result.findings else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
