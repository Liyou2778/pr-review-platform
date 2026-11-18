"""Run the offline bench and write a machine-readable report.

Usage:
  python scripts/bench.py                       # rules engine (free, offline)
  python scripts/bench.py --engine recorded --recorded eval/bench/recorded-ocr.json
  python scripts/bench.py --out eval/bench/last-run.json

The report is the input of the CI gate (``scripts/gate.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from core.adapters.reviewer import ReviewContext, RuleReviewer
from core.bench import load_cases, match
from core.config import get_settings
from core.rules import load_rules

ROOT = Path(__file__).resolve().parents[1]


async def run_rules(cases, rules) -> dict[str, list[dict]]:
    reviewer = RuleReviewer(rules)
    out: dict[str, list[dict]] = {}
    for case in cases:
        result = await reviewer.review(
            ReviewContext(
                repo_full_name="bench/offline",
                pr_number=0,
                base_sha="base",
                head_sha="head",
                diff_text=case.diff,
            )
        )
        out[case.id] = [f.as_dict() for f in result.findings]
    return out


def load_recorded(path: Path) -> dict[str, list[dict]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): v for k, v in raw.get("findings_by_case", {}).items()}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["rules", "recorded"], default="rules")
    parser.add_argument("--recorded", default="eval/bench/recorded-ocr.json")
    parser.add_argument("--cases", default="eval/bench/cases.json")
    parser.add_argument("--out", default="eval/bench/last-run.json")
    args = parser.parse_args()

    cases = load_cases(ROOT / args.cases)
    settings = get_settings()
    rules = load_rules(settings.rules_path)

    if args.engine == "recorded":
        findings_by_case = load_recorded(ROOT / args.recorded)
    else:
        findings_by_case = await run_rules(cases, rules)

    per_case = []
    totals = {"tp": 0, "fp": 0, "fn": 0}
    for case in cases:
        findings = findings_by_case.get(case.id, [])
        result = match(findings, case.truth)
        totals["tp"] += result.tp
        totals["fp"] += result.fp
        totals["fn"] += result.fn
        per_case.append(
            {
                "id": case.id,
                "name": case.name,
                "tp": result.tp,
                "fp": result.fp,
                "fn": result.fn,
                "precision": result.precision,
                "recall": result.recall,
                "f1": result.f1,
                "false_positives": result.false_positives,
                "missed": result.missed,
            }
        )

    tp, fp, fn = totals["tp"], totals["fp"], totals["fn"]
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    report = {
        "engine": args.engine,
        "cases": len(cases),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "per_case": per_case,
    }
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"engine={report['engine']} cases={report['cases']} tp={tp} fp={fp} fn={fn}")
    print(f"precision={report['precision']:.3f} recall={report['recall']:.3f} f1={report['f1']:.3f}")
    for item in per_case:
        print(f"  {item['id']:<18} P={item['precision']:.2f} R={item['recall']:.2f} F1={item['f1']:.2f}")
    print(f"report -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
