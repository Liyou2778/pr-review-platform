"""Record one real OCR run over all bench cases, then score it offline forever.

Why: a model call in CI is slow, flaky and needs a key.  Recording the engine's
actual output once lets the bench and the gate run on every commit for free —
and keeps the numbers honest, because the recording is the real output.

How: all 20 cases are materialised as files in ONE temporary git repo (base
commit = context lines, head commit = added lines).  A single ``ocr review``
over that commit pair is far cheaper than 20 separate runs.

Usage:  python scripts/record_ocr.py [--out eval/bench/recorded-ocr.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from core.bench import load_cases  # noqa: E402
from core.tmp import cleanup, workdir  # noqa: E402

HUNK = "@@"


def _split(diff: str) -> tuple[str, str]:
    """Return (base_content, head_content) for a single-file unified diff."""
    base: list[str] = []
    head: list[str] = []
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith(HUNK):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            head.append(line[1:])
        elif line.startswith("-"):
            base.append(line[1:])
        else:
            text = line[1:] if line.startswith(" ") else line
            base.append(text)
            head.append(text)
    return "\n".join(base) + "\n", "\n".join(head) + "\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "-c", "user.email=bench@example.com", "-c", "user.name=bench", *args],
        stderr=subprocess.PIPE,
    ).decode()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="eval/bench/cases.json")
    parser.add_argument("--out", default="eval/bench/recorded-ocr.json")
    args = parser.parse_args()

    cases = load_cases(ROOT / args.cases)
    workspace = workdir("record-ocr-")
    try:
        _git(workspace, "init", "-q")
        for case in cases:
            base, _head = _split(case.diff)
            path = guess_path(case.diff)
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(base, encoding="utf-8")
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-qm", "base")
        for case in cases:
            _base, head = _split(case.diff)
            (workspace / guess_path(case.diff)).write_text(head, encoding="utf-8")
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-qm", "head")

        from core.adapters.reviewer import OcrReviewer, ReviewContext
        from core.config import get_settings

        settings = get_settings()
        reviewer = OcrReviewer(
            command=settings.ocr_command,
            timeout_s=settings.ocr_timeout_s,
            ocr_home=settings.ocr_home,
            price_in_per_1m=settings.llm_price_in_per_1m,
            price_out_per_1m=settings.llm_price_out_per_1m,
        )
        base_sha = _git(workspace, "rev-parse", "HEAD~1").strip()
        head_sha = _git(workspace, "rev-parse", "HEAD").strip()
        print(f"reviewing {len(cases)} files: {base_sha[:8]}..{head_sha[:8]}")
        result = await reviewer.review(
            ReviewContext(
                repo_full_name="bench/ocr",
                pr_number=0,
                base_sha=base_sha,
                head_sha=head_sha,
                diff_text="",
                workspace_path=str(workspace),
            )
        )

        by_path = {guess_path(case.diff): case.id for case in cases}
        findings_by_case: dict[str, list[dict]] = {case.id: [] for case in cases}
        unmatched: list[dict] = []
        for draft in result.findings:
            case_id = by_path.get(draft.file_path)
            if case_id is None:
                unmatched.append(draft.as_dict())
                continue
            findings_by_case[case_id].append(draft.as_dict())

        payload = {
            "engine": "ocr",
            "model": result.meta.get("model"),
            "provider": result.meta.get("provider"),
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": result.cost_usd,
            "findings_by_case": findings_by_case,
            "unmatched": unmatched,
        }
        out_path = ROOT / args.out
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        total = sum(len(v) for v in findings_by_case.values())
        print(f"recorded {total} findings across {len(cases)} cases -> {out_path}")
        print(f"tokens {result.tokens_in}/{result.tokens_out} cost ${result.cost_usd}")
        return 0
    finally:
        cleanup(workspace)


def guess_path(diff: str) -> str:
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            return line[6:]
    raise ValueError("diff has no +++ b/ path")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
