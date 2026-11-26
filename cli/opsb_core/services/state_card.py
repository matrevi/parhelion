"""Platform-built problem state card (<= 2000 bytes) carried into worker briefs and compaction checkpoints."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from .. import config as _cfg
from .judge_score_fetch import receipts_module

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
    """One row per submission (scheduler.receipts), so a backfilled receipt is not listed twice."""
    rows = receipts_module().canonical(_cfg.read_jsonl(ws / "results" / "submit-queue-outcome.jsonl"))
    return [r for r in rows if isinstance(r, dict) and str(r.get("title") or "") == title]


def _score(row: dict[str, Any]) -> float | None:
    return receipts_module().official_score(row)


def _best_line(rows: list[dict[str, Any]], head: str) -> str:
    passing = [r for r in rows if r.get("status") == "Pass" and isinstance(_score(r), (int, float))]
    if passing:
        best = max(passing, key=lambda r: float(_score(r)))
        best_s = f"{_fmt_score(_score(best))} {_sha7(best.get('sha'))} ({str(best.get('submission_id') or '-')[:8]})"
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
    parts = [f"{_clip(r.get('status') or '?', 24)} {_fmt_score(_score(r))} {_sha7(r.get('sha'))}" for r in recent]
    return "last receipts: " + "; ".join(parts)


def _case_gap_line(ws: Path | None, title: str, rows: list[dict[str, Any]]) -> str | None:
    """The five judge cases losing most points on the newest receipt with per-case times.

    Labels are local case IDs where the case map pairs them (``results/<title>/.case-map.json``)
    and judge roster positions (``#NN``) otherwise.
    """
    receipts = receipts_module()
    latest = next((r for r in reversed(rows) if receipts.case_times(r) and receipts.case_tbest(r)), None)
    if latest is None:
        return None
    times, tbest = receipts.case_times(latest), receipts.case_tbest(latest)
    labels: dict[str, str] = {}
    if ws is not None:
        doc = _cfg.read_json(ws / "results" / title / ".case-map.json", {})
        for pair in (doc.get("pairs") or []) if isinstance(doc, dict) else []:
            if isinstance(pair, dict) and pair.get("judge_mapped"):
                labels[str(pair.get("judge"))] = f"L{pair.get('local')}"
    scored = [(receipts.case_points(times.get(k), b), idx, k) for idx, (k, b) in enumerate(tbest.items())
              if b is not None and times.get(k)]
    parts = [f"{labels.get(k, f'#{idx + 1:02d}')} {_fmt_us(times[k])}/{_fmt_us(tbest[k])}"
             for _, idx, k in sorted(scored)[:5]]
    return f"case gap (worst {len(parts)}/{len(tbest)}, ours/tbest µs): " + (" · ".join(parts) or "n/a")


def _public_gap_line(rows: list[dict[str, Any]], ws: Path | None = None, title: str = "") -> str:
    per_case = _case_gap_line(ws, title, rows)
    if per_case:
        return per_case
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


def _noise_line(ws: Path, title: str) -> str | None:
    """Noisy cases of the newest run (CV over 3%, scheduler.verdicts) and the judge calibration labels."""
    try:
        verdicts = importlib.import_module("scheduler.verdicts")
        calibration = importlib.import_module("scheduler.case_calibration")
        pointers = [_cfg.read_json(p, {}) for p in (ws / "results" / title).glob("*/latest-bench.json")]
        newest = max((p for p in pointers if isinstance(p, dict) and p.get("report")),
                     key=lambda p: str(p.get("finished_at") or ""), default=None)
        report = _cfg.read_json(Path(newest["report"]), {}) if newest else {}
        noisy = verdicts.noisy_cases(report) if isinstance(report, dict) else []
        stored = list(calibration.load(ws, title).values())
        labels = calibration.table(ws, title, records=stored)["labels"] if stored else {}
        if not noisy and not labels:
            return None
        parts = [f"noisy (CV>3%, no gain claims): {','.join(noisy[:6]) or 'none'}"]
        if labels:
            parts.append(f"judge calibration: trusted {len(labels['trusted'])} · noisy {len(labels['noisy'])} · "
                         f"divergent {','.join(labels['divergent'][:6]) or 'none'}")
        return _clip(" · ".join(parts), 300)
    except Exception:
        return None


def _fit(lines: list[str]) -> str:
    text = "\n".join(lines)
    raw = text.encode("utf-8")
    if len(raw) <= MAX_CARD_BYTES:
        return text
    # Keep the rules line intact; shrink the variable sections in the middle.
    head = "\n".join(lines[:-1]).encode("utf-8")
    budget = MAX_CARD_BYTES - len(lines[-1].encode("utf-8")) - 1
    return head[:budget].decode("utf-8", errors="ignore").rstrip() + "\n" + lines[-1]


def build(ws: Path, title: str, agent_id: str | None = None, extended: bool = False) -> str:
    """Return the plain-text state card for one problem; never raises.

    ``extended`` gives the per-case card of ``state_card_ext`` (4.1 #13 #14), used by the compaction
    seed and by ``parhelion_status(detail=true)``; this simple card keeps its 2000-byte ceiling.
    """
    if extended:
        from . import state_card_ext
        return state_card_ext.build(ws, title, agent_id)
    try:
        ws = Path(ws).expanduser().resolve()
        rows = _receipts(ws, title)
        lines = [
            f"STATE {title} @ {_cfg.utc_now().strftime('%H:%M')}",
            _best_line(rows, _head_text(ws, title)),
            _receipts_line(rows),
            _public_gap_line(rows, ws, title),
            _tests_line(ws, title, agent_id),
            _suite_line(ws, title),
            _noise_line(ws, title),
            RULES_LINE,
        ]
        return _fit([line for line in lines if line])
    except Exception:
        return f"STATE {title}: unavailable"
