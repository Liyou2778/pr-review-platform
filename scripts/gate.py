"""CI gate: fail the build when review quality regresses.

Thresholds are intentionally blunt and visible — a gate whose rule nobody can
restate is a gate people disable.

Usage:
  python scripts/gate.py --current eval/bench/last-run.json \
      --baseline eval/bench/baseline.json \
      --max-f1-drop 0.02 --min-f1 0.60
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--baseline", default="")
    parser.add_argument("--max-f1-drop", type=float, default=0.02)
    parser.add_argument("--max-precision-drop", type=float, default=0.02)
    parser.add_argument("--min-f1", type=float, default=0.0)
    args = parser.parse_args()

    current = load(Path(args.current))
    failures: list[str] = []

    if current["f1"] < args.min_f1:
        failures.append(f"f1 {current['f1']:.3f} < floor {args.min_f1:.3f}")

    if args.baseline:
        baseline = load(Path(args.baseline))
        f1_drop = round(baseline["f1"] - current["f1"], 4)
        p_drop = round(baseline["precision"] - current["precision"], 4)
        print(f"baseline: f1={baseline['f1']:.3f} precision={baseline['precision']:.3f}")
        print(f"current : f1={current['f1']:.3f} precision={current['precision']:.3f}")
        print(f"delta   : f1 {f1_drop:+.3f} precision {p_drop:+.3f}")
        if f1_drop > args.max_f1_drop:
            failures.append(f"f1 dropped {f1_drop:.3f} > allowed {args.max_f1_drop:.3f}")
        if p_drop > args.max_precision_drop:
            failures.append(
                f"precision dropped {p_drop:.3f} > allowed {args.max_precision_drop:.3f}"
            )
    else:
        print(f"no baseline: current f1={current['f1']:.3f}")

    if failures:
        print("GATE: FAIL")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
