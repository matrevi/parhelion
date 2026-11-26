"""Git metadata reader: refs and HEAD straight from the files (<0.1ms), one bounded subprocess for ancestry."""
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


def read_refs(git_dir: Path) -> dict[str, str]:
    """Every ``refs/*`` of a repository: loose ref files first, ``packed-refs`` fills the gaps."""
    refs: dict[str, str] = {}
    for base, prefix in ((git_dir / "refs" / "heads", "refs/heads/"),
                         (git_dir / "refs" / "tags", "refs/tags/")):
        if not base.is_dir():
            continue
        for entry in base.rglob("*"):
            try:
                sha = entry.read_text(encoding="utf-8").strip()[:40] if entry.is_file() else ""
            except OSError:
                continue
            if len(sha) == 40:
                refs[prefix + entry.relative_to(base).as_posix()] = sha
    try:
        lines = (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        if len(sha) == 40 and name.strip() and name.strip() not in refs:
            refs[name.strip()] = sha
    return refs


def main_head(git_dir: Path) -> str:
    """The ``main`` commit, or the checked-out HEAD when the repository has no ``main``."""
    refs = read_refs(git_dir)
    if "refs/heads/main" in refs:
        return refs["refs/heads/main"]
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if head.startswith("ref: "):
        return refs.get(head[5:].strip(), "")
    return head[:40] if len(head) >= 40 else ""


def best_tag_of(refs: dict[str, str]) -> dict[str, object]:
    """The highest-scored ``best/*`` tag: ``{tag, tag_score, tag_sha, tag_on_best}``."""
    tags: list[tuple[float, str, str]] = []
    for name, sha in refs.items():
        if not name.startswith("refs/tags/best/"):
            continue
        short = name[len("refs/tags/best/"):]
        try:
            score = float(short.split("-", 1)[0])
        except ValueError:
            score = 0.0
        tags.append((score, short, sha))
    if not tags:
        return {"tag": "", "tag_score": None, "tag_sha": "", "tag_on_best": ""}
    tags.sort(key=lambda row: (-row[0], row[1]))
    score, short, sha = tags[0]
    return {"tag": short, "tag_score": score, "tag_sha": sha[:7],
            "tag_on_best": next((name for _s, name, one in tags if one == sha), "")}


def branches_containing(repo_dir: Path, sha: str) -> tuple[list[str], str]:
    """``(branches, error)`` holding ``sha`` through one bounded ``git branch --contains``."""
    if not sha or not (repo_dir / ".git").exists():
        return [], "no repository"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "branch", "--contains", sha, "--format=%(refname:short)"],
            capture_output=True, text=True, timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"git unavailable: {type(exc).__name__}"
    if proc.returncode != 0:
        return [], (proc.stderr or "the SHA is not in this repository").strip()[:160]
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()], ""


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


def read_commit_subject(repo_dir: Path, rev: str) -> str:
    if not (repo_dir / ".git").exists() or not rev:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "-1", "--format=%s", rev],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""
