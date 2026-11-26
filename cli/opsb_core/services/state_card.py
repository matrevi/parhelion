"""Platform-built problem state card (<= 2000 bytes) carried into worker briefs and compaction checkpoints."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from .. import config as _cfg

MAX_CARD_BYTES = 2000
RULES_LINE = "rules: device work only via parhelion_test; submit via parhelion_submit_gate; no ssh/flock."


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def _sha7(sha: Any) -> str:
    return str(sha or "")[:7] or "-"


def _fmt_score(score: Any) -> str:
    if score is None:
        return "-"
    try:
        return f"{float(score):.2f}"
    except (TypeError, ValueError):
        return _clip(score, 12)


def _receipts(ws: Path, title: str) -> list[dict[str, Any]]:
    rows = _cfg.read_jsonl(ws / "results" / "submit-queue-outcome.jsonl")
    return [r for r in rows if isinstance(r, dict) and str(r.get("title") or "") == title]


def _best_line(rows: list[dict[str, Any]], head: str) -> str:
    passing = [r for r in rows if r.get("status") == "Pass" and isinstance(r.get("score"), (int, float))]
    if passing:
        best = max(passing, key=lambda r: float(r["score"]))
        best_s = f"{_fmt_score(best['score'])} {_sha7(best.get('sha'))} ({str(best.get('submission_id') or '-')[:8]})"
    else:
        best_s = "n/a"
    return f"best: {best_s} | head: {head}"


def _head_text(ws: Path, title: str) -> str:
    try:
        from ..drivers.brief import resolve_problem_repo
        from .git_fast import read_head_fast

        _, repo, _ = resolve_problem_repo(ws, title)
        sha, _, subject = read_head_fast(repo) if repo.is_dir() else ("", None, "")
        return f"{_sha7(sha)} {_clip(subject, 60)}".strip() if sha else "n/a"
    except Exception:
        return "n/a"


def _receipts_line(rows: list[dict[str, Any]]) -> str:
    recent = list(reversed(rows[-3:]))
    if not recent:
        return "last receipts: n/a"
    parts = [f"{_clip(r.get('status') or '?', 24)} {_fmt_score(r.get('score'))} {_sha7(r.get('sha'))}" for r in recent]
    return "last receipts: " + "; ".join(parts)


def _public_gap_line(rows: list[dict[str, Any]]) -> str:
    latest = next((r for r in reversed(rows) if r.get("public_times_us")), None)
    if not latest:
        return "public gap (ours/tbest µs): n/a"
    ours = list(latest.get("public_times_us") or [])
    tbest = list(latest.get("public_tbest_us") or [])
    parts = []
    for idx, value in enumerate(ours[:5]):
        best = tbest[idx] if idx < len(tbest) else None
        parts.append(f"{idx + 1:02d} {_fmt_us(value)}/{_fmt_us(best)}")
    return "public gap (ours/tbest µs): " + (" · ".join(parts) or "n/a")


def _fmt_us(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def _tests_line(ws: Path, title: str, agent_id: str | None) -> str:
    try:
        test_runs = importlib.import_module(__package__ + ".test_runs")  # work package A; optional until merged
        from ..drivers.base import resolve_workspace_state_dir

        root = resolve_workspace_state_dir(ws) / "test_runs"
        records = []
        for path in root.glob("*.json") if root.is_dir() else []:
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(rec, dict) or str(rec.get("problem") or "") != title:
                continue
            if agent_id and str(rec.get("agent_id") or "") != agent_id:
                continue
            records.append(rec)
        records.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        lines = [_clip(test_runs.compact_line(r), 300) for r in records[:3]]
        return "last tests: " + ("; ".join(lines) if lines else "n/a")
    except Exception:
        return "last tests: n/a"


def _suite_line(ws: Path, title: str) -> str:
    try:
        load_suite = importlib.import_module("scheduler.suite").load_suite  # work package B1; optional until merged
        suite = load_suite(ws, title)
        extra = len(suite.get("timed") or [])
        # timing_scope "official" (the B1 default) times every official case plus the listed extras.
        timed = f"official+{extra}" if suite.get("timing_scope") == "official" else str(extra)
        return f"suite: v{int(suite.get('suite_version') or 0)} · timed {timed} · levels smoke/standard/final"
    except Exception:
        return "suite: n/a"


def _fit(lines: list[str]) -> str:
    text = "\n".join(lines)
    raw = text.encode("utf-8")
    if len(raw) <= MAX_CARD_BYTES:
        return text
    # Keep the rules line intact; shrink the variable sections in the middle.
    head = "\n".join(lines[:-1]).encode("utf-8")
    budget = MAX_CARD_BYTES - len(lines[-1].encode("utf-8")) - 1
    return head[:budget].decode("utf-8", errors="ignore").rstrip() + "\n" + lines[-1]


def build(ws: Path, title: str, agent_id: str | None = None) -> str:
    """Return the plain-text state card for one problem; never raises."""
    try:
        ws = Path(ws).expanduser().resolve()
        rows = _receipts(ws, title)
        lines = [
            f"STATE {title} @ {_cfg.utc_now().strftime('%H:%M')}",
            _best_line(rows, _head_text(ws, title)),
            _receipts_line(rows),
            _public_gap_line(rows),
            _tests_line(ws, title, agent_id),
            _suite_line(ws, title),
            RULES_LINE,
        ]
        return _fit(lines)
    except Exception:
        return f"STATE {title}: unavailable"
