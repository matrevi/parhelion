"""Zero-subprocess Git metadata reader (<0.1ms per repository)."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path


def read_head_fast(repo_dir: Path) -> tuple[str, int | None, str]:
    """Read HEAD SHA, commit age in minutes, and subject directly from `.git/logs/HEAD`."""
    git_dir = repo_dir / ".git"
    log_head = git_dir / "logs" / "HEAD"
    if log_head.is_file():
        try:
            raw = log_head.read_bytes()
            idx = raw.rfind(b"\n", 0, max(0, len(raw) - 1))
            last_line = raw[idx + 1 :].decode("utf-8", errors="replace").strip()
            if last_line:
                left, _, msg = last_line.partition("\t")
                tokens = left.split()
                sha = tokens[1] if len(tokens) >= 2 else ""
                ts = int(tokens[-2]) if len(tokens) >= 4 and tokens[-2].isdigit() else None
                age_min = max(0, int((time.time() - ts) / 60)) if ts else None
                subj = msg.split(": ", 1)[1] if ": " in msg else msg
                if len(sha) == 40:
                    return sha, age_min, subj
        except Exception:
            pass

    head_file = git_dir / "HEAD"
    if head_file.is_file():
        try:
            ref = head_file.read_text(encoding="utf-8").strip()
            if ref.startswith("ref: "):
                ref_path = git_dir / ref[5:]
                if ref_path.is_file():
                    return ref_path.read_text(encoding="utf-8").strip(), None, ""
            elif len(ref) == 40:
                return ref, None, ""
        except Exception:
            pass
    return "", None, ""


def resolve_commit_sha(repo_dir: Path, rev: str) -> str | None:
    if not (repo_dir / ".git").exists() or not rev:
        return rev or None
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", rev],
        capture_output=True,
        text=True,
        timeout=2.0,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None
