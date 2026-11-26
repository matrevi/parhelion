"""Deterministic gate-time knowledge hints: at most two related anti-pattern/bug lines, ≤600 bytes.

The submit gate attaches these to its reply for both the preview and the enqueue call (P28 scope 4).
They are advisory only: nothing in this module feeds the gate stages, the priority or the approval
flag, so a hint can never change a verdict. Relevance is a pure keyword intersection — no model call,
no network, no workspace mutation, and no KB seeding (a missing KB simply yields no hints).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from ..config import read_json
from .store import VALID_PILLARS, triad_kb_dirs

MAX_HINTS = 2
MAX_HINT_BYTES = 600
MIN_KEYWORD_HITS = 2
MIN_LINE_BYTES = 80

# Submission notes and commit subjects are short; these tokens carry no mechanism.
_STOPWORDS = frozenset({
    "and", "the", "for", "with", "from", "that", "this", "into", "than", "then", "when", "where",
    "have", "has", "had", "was", "were", "are", "but", "not", "its", "it's", "our", "your",
    "try", "tries", "tried", "test", "tests", "tested", "check", "adds", "added", "make", "made",
    "more", "less", "best", "better", "good", "bad", "new", "old", "same", "only", "also", "just",
    "after", "before", "while", "should", "would", "could", "about", "over", "under", "again",
    "score", "points", "receipt", "version", "commit", "change", "changes", "update", "updates",
})
# Split identifiers and hyphenated ids so `HW-noinline-hot-loop` shares `noinline` with a note.
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_LIST_KEYS = ("tags", "bottlenecks", "applicable_problems")


def _tokens(text: Any) -> set[str]:
    toks = _TOKEN_RE.findall(str(text or "").lower())
    return {tok for tok in toks if tok not in _STOPWORDS and not tok.isdigit()}


def _list_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value)
    return ""


def _entry_blob(entry: dict[str, Any]) -> str:
    parts = [
        str(entry.get("id") or ""), str(entry.get("title") or ""), str(entry.get("summary") or ""),
        str(entry.get("lesson") or ""), str(entry.get("fix") or ""), str(entry.get("category") or ""),
        str(entry.get("problem") or ""),
        *(_list_text(entry.get(key)) for key in _LIST_KEYS),
    ]
    return " ".join(part for part in parts if part)


def _iter_candidates(parhelion_home: Path | None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield `(pillar, raw_entry)` for anti-patterns and bugs, without normalizing or seeding."""
    dirs = triad_kb_dirs(parhelion_home)
    for pillar in VALID_PILLARS:
        idx = dirs[pillar] / "index.json"
        if not idx.is_file():
            continue
        raw = read_json(idx, {"entries": []})
        for entry in (raw.get("entries") if isinstance(raw, dict) else []) or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            if pillar == "bugs" or entry.get("status") == "anti-pattern" or entry.get("portability") == "anti-pattern":
                yield pillar, entry


def _same_problem(entry: dict[str, Any], problem_lc: str) -> bool:
    if not problem_lc:
        return False
    if str(entry.get("problem") or "").strip().lower() == problem_lc:
        return True
    return problem_lc in _tokens(_list_text(entry.get("applicable_problems")))


def _clip(text: str, limit_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit_bytes:
        return text
    return raw[: max(0, limit_bytes - 3)].decode("utf-8", "ignore").rstrip() + "..."


def _hint_line(pillar: str, entry: dict[str, Any], limit_bytes: int) -> str:
    kind = "bugs" if pillar == "bugs" else "anti-pattern"
    label = " ".join(str(entry.get("category") or entry.get("status") or "").split())
    title = " ".join(str(entry.get("title") or entry.get("id") or "").split())
    detail = " ".join(str(entry.get("lesson") or entry.get("summary") or "").split())
    head = f"!{entry.get('id')} [{kind}|{label}]: {title}"
    return _clip(f"{head} — {detail}" if detail else head, limit_bytes)


def select_gate_hints(
    *, problem: str, subject: str = "", note: str = "", parhelion_home: Path | None = None,
    limit: int = MAX_HINTS, max_bytes: int = MAX_HINT_BYTES,
) -> list[str]:
    """Return ≤`limit` one-line hints, total ≤`max_bytes` UTF-8 bytes, deterministically ordered.

    Relevance: the same problem name (bugs and applicable problems), or at least `MIN_KEYWORD_HITS`
    mechanism keywords shared with the hypothesis subject / submission note. Same-problem entries rank
    first, then keyword hits, then id — so two identical calls always return the same lines.
    """
    lim = max(0, int(limit))
    budget = max(0, int(max_bytes))
    if lim == 0 or budget == 0:
        return []
    problem_lc = str(problem or "").strip().lower()
    keywords = _tokens(f"{subject} {note}")
    ranked: list[tuple[int, int, str, str, dict[str, Any]]] = []
    for pillar, entry in _iter_candidates(parhelion_home):
        entry_tokens = _tokens(_entry_blob(entry))
        hits = len(keywords & entry_tokens)
        same = _same_problem(entry, problem_lc)
        if not (same or hits >= MIN_KEYWORD_HITS):
            continue
        ranked.append((0 if same else 1, -hits, str(entry.get("id")), pillar, entry))
    ranked.sort(key=lambda row: (row[0], row[1], row[2]))

    per_line = max(MIN_LINE_BYTES, (budget - (lim - 1)) // lim)
    lines: list[str] = []
    for *_, pillar, entry in ranked[:lim]:
        line = _hint_line(pillar, entry, per_line)
        if len("\n".join([*lines, line]).encode("utf-8")) <= budget:
            lines.append(line)
    return lines


def gate_hints(
    workspace: Path, problem: str, sha: str, note: str = "",
    parhelion_home: Path | None = None,
) -> list[str]:
    """`select_gate_hints` for the submit gate; reads the commit subject as the hypothesis title.

    Advisory code must never break a submission, so a subject lookup or KB read failure yields [].
    """
    subject = ""
    try:
        from ..services.git_fast import read_commit_subject

        subject = read_commit_subject(Path(workspace), sha)
    except Exception:
        subject = ""
    try:
        return select_gate_hints(problem=problem, subject=subject, note=note, parhelion_home=parhelion_home)
    except Exception:
        return []
