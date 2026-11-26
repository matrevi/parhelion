"""Which files a worker should change (4.2 G, section 4 of the start-up pack).

The list comes from git, not from a model: the current best SHA is diffed against the problem-template
baseline (the repository's oldest root commit), the changed files are ranked code-first by changed
lines, each file keeps up to ``HUNKS_PER_FILE`` new-side line ranges from ``git diff -U0``, and the
kernel entry files are appended so the pack always names the file the official template edits. Every
git call is bounded and returns an empty value on failure, so a missing repository degrades to the
entry-file list instead of blocking a launch.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..ledger.gitref import git_out

FILES_LIMIT = 5
HUNKS_PER_FILE = 3
ENTRY_FILES = ("kernel.asc", "main.asc")
CODE_EXTS = frozenset({".asc", ".c", ".cc", ".cpp", ".h", ".hpp", ".py"})
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


# --- section 4: the files to change --------------------------------------------------------------

def template_baseline(worktree: Path | str, sha: str = "HEAD") -> str:
    """The problem-template baseline: the repository's oldest root commit reachable from ``sha``."""
    out = git_out(Path(worktree).expanduser(), "rev-list", "--max-parents=0", str(sha or "HEAD"))
    roots = [line.strip() for line in (out or "").splitlines() if line.strip()]
    return roots[-1] if roots else ""


def _hunks(worktree: Path, base: str, best: str, path: str) -> list[list[int]]:
    out = git_out(worktree, "diff", "-U0", f"{base}..{best}", "--", path) or ""
    ranges: list[list[int]] = []
    for start, count in HUNK_RE.findall(out):
        first = int(start)
        count = int(count or 1)
        if count <= 0:
            continue
        last = first + count - 1
        if ranges and first <= ranges[-1][1] + 1:
            ranges[-1][1] = last
        else:
            ranges.append([first, last])
    return ranges[:HUNKS_PER_FILE]


def change_targets(worktree: Path | str, best_sha: str = "", limit: int = FILES_LIMIT) -> dict[str, Any]:
    """``{base, best, files:[{path, hunks, changed, entry}]}`` from ``git diff`` in the worktree."""
    repo = Path(worktree).expanduser().resolve()
    best = str(best_sha or "").strip() or (git_out(repo, "rev-parse", "HEAD") or "").strip()
    base = template_baseline(repo, best or "HEAD") if best else ""
    files: list[dict[str, Any]] = []
    if best and base and best != base:
        numstat = git_out(repo, "diff", "--numstat", f"{base}..{best}") or ""
        rows: list[tuple[int, str]] = []
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path = parts[0], parts[1], parts[2].strip()
            try:
                changed = int(added) + int(deleted)
            except ValueError:
                changed = 0  # binary or rename: keep the file, without a line count
            rows.append((changed, path))
        rows.sort(key=lambda r: (0 if Path(r[1]).suffix.lower() in CODE_EXTS else 1, -r[0], r[1]))
        for changed, path in rows[:limit]:
            files.append({"path": path, "hunks": _hunks(repo, base, best, path), "changed": changed,
                          "entry": False})
    listed = {f["path"] for f in files}
    for name in ENTRY_FILES:
        if (repo / name).is_file() and name not in listed:
            files.append({"path": name, "hunks": [], "changed": 0, "entry": True})
    return {"base": base, "best": best, "files": files[: max(limit, limit + len(ENTRY_FILES))]}


def section_files(targets: dict[str, Any]) -> str:
    files = targets.get("files") or []
    if not files:
        return "No git history here; edit the kernel entry file the brief names."
    best = str(targets.get("best") or "")[:7]
    base = str(targets.get("base") or "")[:7]
    head = f"Current best `{best}` against the template baseline `{base}`." if best and base else \
        "Entry files of this template:"
    lines = [head]
    for row in files:
        ranges = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in row.get("hunks") or [])
        if row.get("entry"):
            lines.append(f"- `{row['path']}` (kernel entry file; read-only in the official template)")
        elif ranges:
            lines.append(f"- `{row['path']}` lines {ranges} ({row.get('changed') or 0} changed lines)")
        else:
            lines.append(f"- `{row['path']}` ({row.get('changed') or 0} changed lines)")
    lines.append("Read these ranges first, then call parhelion_status: it already reports the score, the "
                 "per-case gap and the queue, so a broad survey before your first platform call is wasted "
                 "context.")
    return "\n".join(lines)


__all__ = [
    "CODE_EXTS",
    "ENTRY_FILES",
    "FILES_LIMIT",
    "HUNKS_PER_FILE",
    "change_targets",
    "section_files",
    "template_baseline",
]
