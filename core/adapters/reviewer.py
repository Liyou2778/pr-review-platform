"""Reviewer engines behind one interface.

``RuleReviewer``  — deterministic regex layer.  Zero tokens, zero network,
                    always available.  It is also the baseline in the ablation.
``OcrReviewer``   — upstream Alibaba ``open-code-review`` CLI.  Runs against a
                    real git workspace; output is normalised into the same
                    ``FindingDraft`` shape so nothing downstream changes.

The interface is what matters: swapping the engine must not touch the platform
(queue, dedupe, comments, metrics).  That is the whole point of the adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from core.review import FindingDraft, iter_added_lines
from core.rules import Rule
from core.tmp import TMP_ROOT

log = logging.getLogger(__name__)

# Upstream uses critical/high/medium/low/info; our platform uses four levels.
SEVERITY_MAP = {
    "critical": "blocker",
    "blocker": "blocker",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "info": "low",
}


class ReviewError(Exception):
    def __init__(self, message: str, error_type: str = "review_error") -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(slots=True)
class ReviewContext:
    repo_full_name: str
    pr_number: int
    base_sha: str
    head_sha: str
    diff_text: str
    workspace_path: str | None = None


@dataclass(slots=True)
class ReviewResult:
    engine: str
    findings: list[FindingDraft] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    meta: dict = field(default_factory=dict)


class RuleReviewer:
    engine = "rules"

    def __init__(self, rules: list[Rule]) -> None:
        self.rules = [r for r in rules if r.pattern]

    async def review(self, ctx: ReviewContext) -> ReviewResult:
        drafts: list[FindingDraft] = []
        added = iter_added_lines(ctx.diff_text)
        for line in added:
            for rule in self.rules:
                if not rule.applies_to(line.path):
                    continue
                regex = rule.compiled()
                if regex is None:
                    continue
                match = regex.search(line.text)
                if not match:
                    continue
                drafts.append(
                    FindingDraft(
                        file_path=line.path,
                        line_start=line.line_no,
                        severity=rule.severity,
                        rule_key=rule.key,
                        message=rule.message,
                        evidence={
                            "engine": self.engine,
                            "snippet": line.text.strip()[:200],
                            "matched": match.group(0)[:120],
                            "pattern": rule.pattern,
                            "owner": rule.owner,
                        },
                    )
                )
        return ReviewResult(engine=self.engine, findings=drafts)


def _pick(mapping: dict, *names: str, default=None):
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return default


def _parse_findings(payload) -> list[FindingDraft]:
    """Tolerant parser: upstream schema is not part of our contract yet (P1)."""
    raw_items: list = []
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        for key in ("findings", "comments", "issues", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                raw_items = value
                break
        else:
            nested = payload.get("data")
            if isinstance(nested, list):
                raw_items = nested

    drafts: list[FindingDraft] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        path = _pick(raw, "file_path", "path", "file", "filename")
        line = _pick(raw, "line_start", "line", "start_line", "new_line", default=1)
        message = _pick(raw, "message", "content", "body", "comment", "text", default="")
        if not path or not message:
            continue
        try:
            line_no = max(1, int(line))
        except (TypeError, ValueError):
            line_no = 1
        raw_severity = str(_pick(raw, "severity", "level", default="medium")).lower()
        severity = SEVERITY_MAP.get(raw_severity, "medium")
        end_line = _pick(raw, "line_end", "end_line", default=line_no)
        try:
            end_no = max(line_no, int(end_line))
        except (TypeError, ValueError):
            end_no = line_no
        drafts.append(
            FindingDraft(
                file_path=str(path),
                line_start=line_no,
                line_end=end_no,
                severity=severity,
                rule_key=str(_pick(raw, "rule_key", "rule", "category", "type", default="ocr")),
                message=str(message),
                evidence={
                    "engine": "ocr",
                    "category": raw.get("category"),
                    "suggestion_code": raw.get("suggestion_code"),
                    "existing_code": raw.get("existing_code"),
                },
            )
        )
    return drafts


class OcrReviewer:
    """Runs ``ocr review`` inside a prepared workspace and normalises the JSON."""

    engine = "ocr"

    def __init__(
        self,
        command: str = "ocr",
        timeout_s: int = 600,
        ocr_home: str = "",
        price_in_per_1m: float = 0.27,
        price_out_per_1m: float = 1.10,
    ) -> None:
        self.command = command
        self.timeout_s = timeout_s
        self.ocr_home = ocr_home
        self.price_in = price_in_per_1m
        self.price_out = price_out_per_1m

    def _env(self) -> dict[str, str]:
        """Point the CLI's config dir at a writable project-local home.

        The CLI chmods ``$HOME/.opencodereview/config.json``; on Windows
        profiles where that path is not writable the whole review fails.
        """
        env = dict(os.environ)
        if self.ocr_home:
            home = str(Path(self.ocr_home).resolve())
            Path(home).mkdir(parents=True, exist_ok=True)
            env["USERPROFILE"] = home
            env["HOME"] = home
        return env

    async def review(self, ctx: ReviewContext) -> ReviewResult:
        if not ctx.workspace_path:
            raise ReviewError(
                "ocr mode requires a git workspace; run with OCR_MODE=cli after P1",
                "ocr_not_ready",
            )
        workspace = Path(ctx.workspace_path)
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ocr-out-", dir=str(TMP_ROOT)) as tmp:
            out_file = Path(tmp) / "review.json"
            args = [
                self.command,
                "review",
                "--from",
                ctx.base_sha or "HEAD~1",
                "--to",
                ctx.head_sha or "HEAD",
                "--format",
                "json",
                "--output",
                str(out_file),
                "--audience",
                "agent",
                "--timeout",
                str(max(1, self.timeout_s // 60)),
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(workspace),
                    env=self._env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise ReviewError(f"ocr binary not found: {self.command}", "ocr_missing") from exc
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
            except TimeoutError as exc:
                proc.kill()
                raise ReviewError(f"ocr timed out after {self.timeout_s}s", "ocr_timeout") from exc
            if proc.returncode != 0:
                tail = (stderr or b"").decode("utf-8", "replace")[-500:]
                raise ReviewError(f"ocr exited {proc.returncode}: {tail}", "ocr_failed")
            if not out_file.exists():
                raise ReviewError("ocr produced no output file", "ocr_no_output")
            try:
                payload = json.loads(out_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ReviewError(f"ocr output is not valid JSON: {exc}", "ocr_parse") from exc

        payload = payload if isinstance(payload, dict) else {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        tokens_in = int(_pick(summary, "input_tokens", "prompt_tokens", default=0) or 0)
        tokens_out = int(_pick(summary, "output_tokens", "completion_tokens", default=0) or 0)
        cost = (
            tokens_in / 1_000_000.0 * self.price_in
            + tokens_out / 1_000_000.0 * self.price_out
        )
        return ReviewResult(
            engine=self.engine,
            findings=_parse_findings(payload),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=round(cost, 6),
            meta={
                "provider": (payload.get("llm") or {}).get("provider"),
                "model": (payload.get("llm") or {}).get("model"),
                "session_id": payload.get("session_id"),
                "files_reviewed": summary.get("files_reviewed"),
                "cache_read_tokens": summary.get("cache_read_tokens"),
                "stderr_tail": (stderr or b"").decode("utf-8", "replace")[-300:],
            },
        )


def get_reviewer(settings, rules: list[Rule]):
    mode = (settings.ocr_mode or "fake").lower()
    if mode == "cli":
        return OcrReviewer(
            command=settings.ocr_command,
            timeout_s=settings.ocr_timeout_s,
            ocr_home=settings.ocr_home,
            price_in_per_1m=settings.llm_price_in_per_1m,
            price_out_per_1m=settings.llm_price_out_per_1m,
        )
    return RuleReviewer(rules)
