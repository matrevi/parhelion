"""The ledger-only seed: what a new session gets when the model's handoff cannot be trusted.

Fixed rules, the platform state card, the ledger summary, and the worktree's ``git status`` and
``git diff --stat``. Nothing in it comes from the model, so a failed handoff cannot corrupt it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from . import gitref, summary

SEED_PREFIX = "Preserve this checkpoint and await the next task."
STATUS_MAX = 600
DIFF_MAX = 400
FALLBACK_NOTE = "The previous session's handoff could not be validated; this seed is built from the ledger and the worktree."
CLOSING = "Continue from the open hypotheses and unexploited insights above."


def _clip_tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def worktree_state(worktree: Path | str) -> str:
    """``git status --short`` and ``git diff --stat HEAD`` of the worktree, bounded; empty when clean or unreadable."""
    status = gitref.git_out(worktree, "status", "--short") or ""
    diff = gitref.git_out(worktree, "diff", "--stat", "HEAD") or ""
    parts = []
    if status.strip():
        parts.append("git status:\n" + _clip_tail(status, STATUS_MAX))
    if diff.strip():
        parts.append("git diff --stat:\n" + _clip_tail(diff, DIFF_MAX))
    return "\n".join(parts) if parts else "worktree: clean"


def build(*, card: str, rows: Iterable[dict[str, Any]], worktree: Path | str, own: str = "",
          rules: str = "") -> str:
    """The complete fallback seed text."""
    block = summary.render(rows, own=own)
    parts = [SEED_PREFIX, FALLBACK_NOTE, rules.strip(), card.strip(), block, worktree_state(worktree), CLOSING]
    return "\n".join(p for p in parts if p)
