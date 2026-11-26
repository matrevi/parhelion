"""Timing ownership: the measured harness is the platform's, not the candidate's (4.2 R1 item 1).

The runner (runtime/npu_runtime/reference_cases.py) builds the device program from the commit
under test: it takes the template host program ``main.asc``, inserts the platform's own event
timing around the kernel launch and the stream synchronisation, and drives it from the device
broker; ``sample.py`` and ``reference.py`` produce the inputs and the expected outputs that
case_batch compares on CPU. Those three files therefore decide what is timed and what counts as
correct, and the judge replaces them with its own copies (runtime/judge/project_files.py).

A candidate may only change the kernel. ``protected_changes`` compares each protected file at
the commit under test with the version the problem repository first added; a run whose
protected files differ is still executed for diagnosis but is never formal evidence.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

PROTECTED = ("main.asc", "sample.py", "reference.py")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _blob(repo: Path, rev: str, name: str) -> str | None:
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}:{name}") or None
    except subprocess.CalledProcessError:
        return None


def protected_changes(repo: Path, sha: str) -> list[str]:
    """Protected files whose content at ``sha`` differs from the version first added to the repo."""
    changed = []
    for name in PROTECTED:
        current = _blob(repo, sha, name)
        if current is None:
            continue  # catalog() already refuses a commit without the template files
        try:
            added = _git(repo, "log", "--diff-filter=A", "--format=%H", "--reverse", sha, "--", name).split()
        except subprocess.CalledProcessError:
            added = []
        if added and _blob(repo, added[0], name) != current:
            changed.append(name)
    return changed


def describe(changed: list[str]) -> str:
    return ("protected template files differ from the problem's originals: " + ", ".join(changed)
            + "; the platform owns timing and comparison, so this run is not formal evidence")
