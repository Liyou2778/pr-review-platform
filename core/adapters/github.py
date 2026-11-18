"""GitHub adapter: webhook parsing, diff fetch, review payload.

Kept deliberately thin and side-effect free so it can be unit tested without a
network.  Write operations are gated by ``GITHUB_DRY_RUN`` at the call site,
not here — an adapter that decides policy is an adapter you cannot test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

SUPPORTED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
COMMENT_SEVERITY_TAG = {
    "blocker": "🔴 blocker",
    "high": "🟠 high",
    "medium": "🟡 medium",
    "low": "🔵 low",
}


class GitHubError(Exception):
    pass


@dataclass(slots=True)
class PullRequestEvent:
    repo_full_name: str
    pr_number: int
    action: str
    base_sha: str
    head_sha: str
    clone_url: str = ""
    title: str = ""
    sender: str = ""


def parse_webhook(event: str, payload: dict) -> PullRequestEvent | None:
    """Return a PullRequestEvent for reviewable actions, else None."""
    if event != "pull_request":
        return None
    action = str(payload.get("action") or "")
    if action not in SUPPORTED_ACTIONS:
        return None
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    full_name = repo.get("full_name") or ""
    number = payload.get("number") or pr.get("number")
    if not full_name or not number:
        return None
    return PullRequestEvent(
        repo_full_name=full_name,
        pr_number=int(number),
        action=action,
        base_sha=str((pr.get("base") or {}).get("sha") or ""),
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        clone_url=str(repo.get("clone_url") or ""),
        title=str(pr.get("title") or ""),
        sender=str((payload.get("sender") or {}).get("login") or ""),
    )


async def fetch_diff(
    repo_full_name: str,
    pr_number: int,
    token: str = "",
    timeout_s: float = 30.0,
) -> str:
    """Fetch a PR unified diff.

    The public ``.diff`` endpoint needs no token for public repos, which keeps
    P0 runnable without credentials.  Private repos need a token (P1).
    """
    url = f"https://github.com/{repo_full_name}/pull/{pr_number}.diff"
    headers = {"Accept": "text/plain", "User-Agent": "pr-review-platform/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"fetch diff transport error: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise GitHubError(f"fetch diff failed: HTTP {resp.status_code} for {url}")
    return resp.text


async def fetch_pull_request_meta(
    repo_full_name: str, pr_number: int, token: str = "", timeout_s: float = 30.0
) -> dict:
    """Head/base SHAs for a PR.

    A review comment needs a real commit id; hashing the diff is not one, and
    GitHub rejects it with 422 "Variable $commitOID ... invalid value".
    """
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "pr-review-platform/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"fetch PR meta transport error: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise GitHubError(f"fetch PR meta failed: HTTP {resp.status_code}")
    data = resp.json()
    return {
        "head_sha": str((data.get("head") or {}).get("sha") or ""),
        "base_sha": str((data.get("base") or {}).get("sha") or ""),
        "title": str(data.get("title") or ""),
        "state": str(data.get("state") or ""),
    }


def build_review_payload(head_sha: str, findings: list[dict]) -> dict:
    """Build a single review with N inline comments (one notification, not N)."""
    comments = []
    for item in findings:
        tag = COMMENT_SEVERITY_TAG.get(item["severity"], item["severity"])
        body = f"**{tag}** · `{item['rule_key']}`\n\n{item['message']}"
        comments.append(
            {
                "path": item["file_path"],
                "line": max(1, int(item["line_start"])),
                "side": "RIGHT",
                "body": body,
            }
        )
    counts: dict[str, int] = {}
    for item in findings:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    order = ("blocker", "high", "medium", "low")
    breakdown = " · ".join(f"{name} {counts[name]}" for name in order if name in counts)
    body = f"自动化评审：发现 {len(comments)} 条问题"
    body = f"{body}（{breakdown}）。" if breakdown else body + "。"
    return {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": body,
        "comments": comments,
    }


async def post_review(
    repo_full_name: str,
    pr_number: int,
    payload: dict,
    token: str,
    api_base: str = "https://api.github.com",
    timeout_s: float = 30.0,
) -> dict:
    if not token:
        raise GitHubError("posting a review requires GITHUB_TOKEN")
    url = f"{api_base.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "pr-review-platform/0.1",
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # A flaky network must surface as "review not delivered", never as
            # "review failed" — the findings are already persisted.
            raise GitHubError(f"post review transport error: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise GitHubError(f"post review failed: HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()
