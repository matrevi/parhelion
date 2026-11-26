"""Git facts the ledger needs: does a SHA exist, its subject, and a stable hash of its change.

Every call is bounded (a few seconds) and returns an empty value on any failure: a missing repo or
a slow disk must never block a ledger write or a handoff.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
TIMEOUT = 5.0


def git_out(repo: Path | str, *args: str, stdin: str | None = None) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True,
                              timeout=TIMEOUT, stdin=None if stdin is not None else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def valid_sha(sha: str) -> bool:
    return bool(SHA_RE.match(str(sha or "")))


def commit_exists(repo: Path | str, sha: str) -> bool:
    """``git cat-file -e <sha>^{commit}`` in ``repo`` (a worktree shares its repository's objects)."""
    if not valid_sha(sha) or not Path(repo).is_dir():
        return False
    try:
        proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def subject(repo: Path | str, sha: str) -> str:
    if not valid_sha(sha):
        return ""
    return (git_out(repo, "log", "-1", "--format=%s", sha) or "").strip()


def diff_hash(repo: Path | str, sha: str) -> str:
    """Stable patch id of the change a commit makes (same change, same hash, whatever its parent)."""
    if not valid_sha(sha):
        return ""
    patch = git_out(repo, "show", "--format=", "--patch", "--no-color", sha)
    if not patch or not patch.strip():
        return ""
    out = git_out(repo, "patch-id", "--stable", stdin=patch) or ""
    return out.split()[0][:16] if out.split() else ""


def problem_repo(workspace: Path | str, problem: str) -> Path | None:
    """The problem's repository inside the contest workspace, or None."""
    try:
        from ..drivers.brief import resolve_problem_repo
        _, repo, _ = resolve_problem_repo(Path(workspace).expanduser(), problem)
    except Exception:
        return None
    return repo if (repo / ".git").exists() else None
