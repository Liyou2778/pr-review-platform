"""Bench: measure review quality against labelled PR snapshots.

Why a separate module instead of "just eyeball the comments":

* precision/recall need a *matching rule* between a finding and a ground-truth
  defect. Two findings on the same line are not two hits; one finding that
  spans the right line but the wrong rule is a miss, not a hit.
* the same numbers must be comparable across engines (rules vs ocr), which is
  only true if the matching and the denominator are identical.

Matching rule (documented because every F1 number depends on it):
    a finding matches a truth item when ``file`` and ``rule_key`` are equal and
    the finding's line range overlaps the truth's line range.
    Each truth item can be matched once; extra findings count as false
    positives; unmatched truth items count as false negatives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class TruthItem:
    file: str
    line_start: int
    line_end: int
    rule_key: str
    note: str = ""


@dataclass(slots=True)
class BenchCase:
    id: str
    name: str
    diff: str
    truth: list[TruthItem] = field(default_factory=list)


@dataclass(slots=True)
class MatchResult:
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    missed: list[dict] = field(default_factory=list)
    false_positives: list[dict] = field(default_factory=list)


def load_cases(path: Path | str) -> list[BenchCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases: list[BenchCase] = []
    for item in raw.get("cases", []):
        cases.append(
            BenchCase(
                id=str(item["id"]),
                name=str(item.get("name", item["id"])),
                diff=str(item["diff"]),
                truth=[
                    TruthItem(
                        file=str(t["file"]),
                        line_start=int(t.get("line_start", 1)),
                        line_end=int(t.get("line_end", t.get("line_start", 1))),
                        rule_key=str(t.get("rule_key", "any")),
                        note=str(t.get("note", "")),
                    )
                    for t in item.get("truth", [])
                ],
            )
        )
    return cases


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def match(findings: list[dict], truth: list[TruthItem]) -> MatchResult:
    used: set[int] = set()
    tp = 0
    false_positives: list[dict] = []

    for finding in findings:
        hit_index = None
        for index, item in enumerate(truth):
            if index in used:
                continue
            if item.file != finding["file_path"]:
                continue
            if item.rule_key not in ("any", finding["rule_key"]):
                continue
            if _overlaps(
                int(finding["line_start"]),
                int(finding.get("line_end") or finding["line_start"]),
                item.line_start,
                item.line_end,
            ):
                hit_index = index
                break
        if hit_index is None:
            false_positives.append(finding)
        else:
            used.add(hit_index)
            tp += 1

    missed = [
        {"file": item.file, "line_start": item.line_start, "rule_key": item.rule_key, "note": item.note}
        for index, item in enumerate(truth)
        if index not in used
    ]
    fp = len(false_positives)
    fn = len(missed)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return MatchResult(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        missed=missed,
        false_positives=false_positives,
    )
