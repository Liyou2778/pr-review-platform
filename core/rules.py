"""Rules-as-code.

A rule is a reviewable artifact, not a prompt string floating in code:

* it has a stable ``key`` (used for metrics, dedupe and feedback clustering),
* it declares where it applies (``paths`` / ``languages``) so it stops firing
  on files it was never meant for,
* it carries ``fixtures`` (positive/negative samples) so changing it is a
  regression test, not a vibe.

P0 ships the deterministic layer only.  The OCR engine (P1) plugs into the same
rule keys, which is what makes the ablation ``rules-only`` vs ``ocr`` possible.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# (dir -> (mtime, rules)); see load_rules()
_CACHE: dict[str, tuple[float, list[Rule]]] = {}

LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
}

SEVERITIES = ("blocker", "high", "medium", "low")


@dataclass(slots=True)
class Rule:
    key: str
    message: str
    severity: str = "medium"
    pattern: str = ""
    paths: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    enabled: bool = True
    owner: str = ""
    fixtures: dict = field(default_factory=dict)
    _regex: re.Pattern[str] | None = None

    def compiled(self) -> re.Pattern[str] | None:
        if not self.pattern:
            return None
        if self._regex is None:
            self._regex = re.compile(self.pattern)
        return self._regex

    def applies_to(self, path: str) -> bool:
        if not self.enabled:
            return False
        if self.languages:
            ext = Path(path).suffix.lower()
            if LANG_BY_EXT.get(ext) not in self.languages:
                return False
        if not self.paths:
            return True
        return any(_glob_match(path, g) for g in self.paths)


def _glob_match(path: str, glob: str) -> bool:
    """Match with ``**`` semantics on posix-ish paths (fnmatch's ``*`` crosses ``/``)."""
    path = path.replace("\\", "/").lstrip("./")
    pattern = glob.replace("\\", "/")
    if pattern.startswith("./"):
        pattern = pattern[2:]
    if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
        return True
    return fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern.replace("**/", ""))


def load_rules(rules_dir: Path | str) -> list[Rule]:
    """Load and cache rules by directory mtime.

    The worker used to re-read 9 YAML files on every run.  Caching by mtime
    keeps rule edits live (touch the file, next run reloads) without paying
    disk I/O per review.
    """
    root = Path(rules_dir)
    if not root.exists():
        log.warning("rules dir %s does not exist; running with zero rules", root)
        return []

    cache_key = str(root.resolve())
    latest_mtime = max((p.stat().st_mtime for p in root.rglob("*.y*ml")), default=0.0)
    cached = _CACHE.get(cache_key)
    if cached is not None and cached[0] == latest_mtime:
        return cached[1]

    rules: list[Rule] = []
    for path in sorted(root.rglob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            log.error("invalid rule file %s: %s", path, exc)
            continue
        for raw in doc.get("rules") or []:
            if not isinstance(raw, dict) or "key" not in raw or "message" not in raw:
                log.warning("skipping malformed rule in %s: %r", path, raw)
                continue
            severity = str(raw.get("severity", "medium")).lower()
            rules.append(
                Rule(
                    key=str(raw["key"]),
                    message=str(raw["message"]),
                    severity=severity if severity in SEVERITIES else "medium",
                    pattern=str(raw.get("pattern", "")),
                    paths=[str(p) for p in raw.get("paths") or []],
                    languages=[str(x).lower() for x in raw.get("languages") or []],
                    enabled=bool(raw.get("enabled", True)),
                    owner=str(doc.get("owner", raw.get("owner", ""))),
                    fixtures=raw.get("fixtures") or {},
                )
            )
    log.info("loaded %d rules from %s", len(rules), root)
    _CACHE[cache_key] = (latest_mtime, rules)
    return rules


def clear_rule_cache() -> None:
    _CACHE.clear()
