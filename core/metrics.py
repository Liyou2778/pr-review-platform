"""Metric primitives shared by API and worker.

Nearest-rank percentile: with 20 samples, P95 must be the 19th value, not an
interpolation between 18 and 19.  Reviewers ask "why is your p95 lower than
your max" — the answer is in this function.
"""

from __future__ import annotations

import math


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(math.ceil(pct / 100.0 * len(ordered))) - 1
    index = max(0, min(len(ordered) - 1, index))
    return round(float(ordered[index]), 1)


def summarize(latencies: list[float]) -> dict:
    if not latencies:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(latencies),
        "avg": round(sum(latencies) / len(latencies), 1),
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "max": round(max(latencies), 1),
    }
