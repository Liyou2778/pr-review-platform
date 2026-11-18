"""Git workspaces for the OCR engine.

A PR review needs two commits present in one repository: the merge base and the
head.  ``ocr review --from X --to Y`` then computes the diff itself, which is
the only way to get the upstream's deterministic file selection and rule
matching (we must not re-implement that).

Kept as a tiny, explicit module so the cost is visible: one shallow clone per
run.  P2 replaces it with a repo pool + worktree cache.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

from core.tmp import workdir

log = logging.getLogger(__name__)


class WorkspaceError(Exception):
    def __init__(self, message: str, error_type: str = "workspace_error") -> None:
        super().__init__(message)
        self.error_type = error_type


def _auth_url(clone_url: str, token: str) -> str:
    if not token or not clone_url.startswith("https://github.com/"):
        return clone_url
    return clone_url.replace("https://", f"https://x-access-token:{token}@", 1)


async def _git(args: list[str], cwd: str | None = None, timeout_s: float = 300.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        raise WorkspaceError(f"git {args[0]} timed out", "git_timeout") from exc
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace")[-400:]
        raise WorkspaceError(f"git {args[0]} failed: {tail}", "git_failed")
    return (stdout or b"").decode("utf-8", "replace")


async def prepare_workspace(
    clone_url: str,
    base_sha: str,
    head_sha: str,
    token: str = "",
    keep: bool = False,
) -> str:
    """Clone ``clone_url`` and check out ``head_sha``; return the directory.

    Caller is responsible for cleanup unless ``keep`` is set.
    """
    if not clone_url:
        raise WorkspaceError("repo has no clone_url; cannot prepare a workspace", "no_clone_url")

    root = workdir("ocr-ws-")
    repo_dir = root / "repo"
    url = _auth_url(clone_url, token)
    log.info("cloning %s -> %s", clone_url, repo_dir)
    await _git(
        ["clone", "--filter=blob:none", "--no-checkout", "--quiet", url, str(repo_dir)],
        timeout_s=300.0,
    )
    # Fetch both ends explicitly: a PR head is often not on any branch.
    await _git(["fetch", "--quiet", "origin", base_sha, head_sha], cwd=str(repo_dir), timeout_s=300.0)
    await _git(["checkout", "--quiet", "--detach", head_sha], cwd=str(repo_dir), timeout_s=120.0)
    return str(repo_dir)


def cleanup_workspace(path: str) -> None:
    if not path:
        return
    shutil.rmtree(path, ignore_errors=True)
