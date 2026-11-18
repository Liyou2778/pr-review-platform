"""Unified-diff parsing, finding normalisation and cross-run dedupe.

Line numbers are the part everyone gets wrong: a hunk header
``@@ -10,3 +12,5 @@`` means the first line *after* it is new-file line 12, and
only ``+`` lines advance that counter.  Getting this right is the difference
between a comment on the right line and one that drifts to line 1.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SEVERITY_ORDER = {"blocker": 4, "high": 3, "medium": 2, "low": 1}


@dataclass(slots=True)
class AddedLine:
    path: str
    line_no: int
    text: str


@dataclass(slots=True)
class FindingDraft:
    file_path: str
    line_start: int
    severity: str
    rule_key: str
    message: str
    line_end: int | None = None
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end or self.line_start,
            "severity": self.severity,
            "rule_key": self.rule_key,
            "message": self.message,
            "evidence": self.evidence,
        }


def iter_added_lines(diff: str) -> list[AddedLine]:
    """Yield added lines with their new-file line numbers."""
    out: list[AddedLine] = []
    current_path = ""
    new_line = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            current_path, in_hunk = "", False
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current_path, in_hunk = "", False
            else:
                current_path = target[2:] if target.startswith("b/") else target
                in_hunk = False
            continue
        if raw.startswith("--- "):
            continue
        match = HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            continue
        if not in_hunk or not current_path:
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            out.append(AddedLine(current_path, new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line += 1
    return out


def make_dedup_key(repo: str, pr_number: int, path: str, line: int, rule_key: str) -> str:
    raw = f"{repo}|{pr_number}|{path}|{line}|{rule_key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:40]


def normalize(drafts: list[FindingDraft], max_per_file: int = 10) -> list[FindingDraft]:
    """Sort by severity/line, drop duplicates, cap per file."""
    seen: set[tuple[str, int, str]] = set()
    kept: list[FindingDraft] = []
    per_file: dict[str, int] = {}
    for item in sorted(
        drafts,
        key=lambda d: (-SEVERITY_ORDER.get(d.severity, 0), d.file_path, d.line_start),
    ):
        key = (item.file_path, item.line_start, item.rule_key)
        if key in seen:
            continue
        if per_file.get(item.file_path, 0) >= max_per_file:
            continue
        seen.add(key)
        per_file[item.file_path] = per_file.get(item.file_path, 0) + 1
        kept.append(item)
    return kept
