"""P1 contract tests: upstream payload normalisation and comment payload.

These do not call the model.  They pin the two places where our platform and
the upstream CLI can silently disagree:

* field names (``content`` vs ``message``, ``start_line`` vs ``line``)
* severity vocabulary (``critical`` vs ``blocker``)

A drift here produces comments on the wrong line or findings with the wrong
severity — both are worse than a crash, because they look plausible.
"""

from __future__ import annotations

from core.adapters.github import build_review_payload
from core.adapters.reviewer import _parse_findings

UPSTREAM_PAYLOAD = {
    "status": "success",
    "llm": {"provider": "deepseek", "model": "deepseek-chat"},
    "summary": {
        "files_reviewed": 1,
        "comments": 2,
        "total_tokens": 10273,
        "input_tokens": 9548,
        "output_tokens": 725,
        "cache_read_tokens": 4608,
    },
    "comments": [
        {
            "path": "app.py",
            "content": "Hardcoded live-looking API key committed to source.",
            "suggestion_code": 'API_KEY = os.environ["API_KEY"]',
            "existing_code": 'API_KEY = "sk-live-123"',
            "start_line": 4,
            "end_line": 4,
            "category": "security",
            "severity": "critical",
        },
        {
            "path": "app.py",
            "content": "Bare `except:` swallows KeyboardInterrupt.",
            "start_line": 16,
            "end_line": 19,
            "category": "bug",
            "severity": "high",
        },
    ],
    "session_id": "ce3dd4ab",
}


def test_parse_upstream_schema_maps_fields_and_severity():
    drafts = _parse_findings(UPSTREAM_PAYLOAD)
    assert len(drafts) == 2

    first = drafts[0]
    assert first.file_path == "app.py"
    assert first.line_start == 4 and first.line_end == 4
    assert first.message.startswith("Hardcoded")
    assert first.rule_key == "security"  # category is the stable key upstream gives us
    assert first.severity == "blocker"  # critical -> blocker
    assert first.evidence["suggestion_code"].startswith("API_KEY")

    second = drafts[1]
    assert second.line_start == 16 and second.line_end == 19
    assert second.severity == "high"


def test_parse_unknown_severity_defaults_to_medium():
    payload = {"comments": [{"path": "a.py", "content": "x", "start_line": 1, "severity": "weird"}]}
    assert _parse_findings(payload)[0].severity == "medium"


def test_parse_ignores_items_without_path_or_message():
    payload = {"comments": [{"path": "a.py"}, {"content": "no path"}, "not-a-dict"]}
    assert _parse_findings(payload) == []


def test_comment_payload_is_one_review_with_inline_comments():
    payload = build_review_payload(
        "deadbeef",
        [
            {
                "file_path": "app.py",
                "line_start": 4,
                "severity": "blocker",
                "rule_key": "security",
                "message": "hardcoded key",
            },
            {
                "file_path": "app.py",
                "line_start": 16,
                "severity": "high",
                "rule_key": "bug",
                "message": "bare except",
            },
        ],
    )
    assert payload["commit_id"] == "deadbeef"
    assert payload["event"] == "COMMENT"
    assert len(payload["comments"]) == 2
    assert payload["comments"][0]["side"] == "RIGHT"
    assert payload["comments"][0]["line"] == 4
    assert "blocker" in payload["comments"][0]["body"]
    assert "security" in payload["comments"][0]["body"]
