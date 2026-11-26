"""The extended state card (4.1 #13 #14): per-case evidence for the seed and for ``parhelion_status(detail=true)``.

The simple card of ``state_card`` stays the 2000-byte card of a brief. This one may grow to
``MAX_EXT_BYTES`` and answers, per case:

- speedup against the same-generation baseline, from the gate's own pairing
  (``scheduler.estimator.ScoreEstimator._case_bench_pair``), with a confidence label derived from how
  many official cases were paired and how many formal runs each side has;
- the gap to tbest, ``(ours - tbest) / tbest``, from the newest receipt that carries per-case times and
  tbest, labelled with the judge case map when one is stored (``L05``) and by roster position otherwise
  (``#03``);
- the predicted per-case time of the current head SHA (``scheduler.receipt_predict``) when a case map is
  already stored, so the card and the gate read one prediction;
- the last three receipts (SHA, status, official score, whether the board moved) and the worktree path;
- research R8: the three cases with the largest expected gain, weight times the same-generation gap, and
  the numeric target (the current gap) written out.

Every platform import is optional and bounded: a card that cannot read a source lists the line it has
and never raises.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config as _cfg
from .judge_score_fetch import receipts_module

MAX_EXT_BYTES = 8000
TARGETS = 3
CASE_LINES = 60
CONF_RUNS = 2
CONF_COVERAGE = 0.7


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _sha7(value: Any) -> str:
    return str(value or "")[:7] or "-"


def receipts_rows(ws: Path, title: str) -> list[dict[str, Any]]:
    rows = receipts_module().canonical(_cfg.read_jsonl(ws / "results" / "submit-queue-outcome.jsonl"))
    return [r for r in rows if isinstance(r, dict) and str(r.get("title") or "") == title]


def worktree_of(ws: Path, agent_id: str) -> str:
    """The worker's worktree path from the bus (read-only); empty when the bus or the agent is absent."""
    import sqlite3
    from ..agents.bus_store import agent_bus_db_path
    if not agent_id:
        return ""
    try:
        db = agent_bus_db_path(Path(ws))
    except OSError:
        return ""
    if not db.is_file():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return ""
    try:
        row = conn.execute("SELECT worktree FROM agents WHERE agent_id = ?", (str(agent_id),)).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    return str(row[0] or "") if row else ""


def head_sha(ws: Path, title: str) -> str:
    try:
        from ..drivers.brief import resolve_problem_repo
        from .git_fast import read_head_fast
        _, repo, _ = resolve_problem_repo(ws, title)
        sha, _, _ = read_head_fast(repo) if repo.is_dir() else ("", None, "")
        return str(sha or "")
    except Exception:
        return ""


def labels(ws: Path, title: str) -> dict[str, str]:
    """Judge case -> ``L<local>`` where the stored case map pairs them (empty without one)."""
    _cfg.ensure_scheduler_path()
    from scheduler import case_map
    try:
        doc = case_map.current(Path(ws), title, write=False)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for pair in (doc or {}).get("pairs") or []:
        if isinstance(pair, dict) and pair.get("judge_mapped"):
            out[str(pair.get("judge"))] = f"L{pair.get('local')}"
    return out


def speedups(ws: Path, title: str, head: str, base: str) -> dict[str, Any]:
    """Per-case speedup of ``head`` against ``base`` from the gate's pairing, plus its confidence."""
    if not head or not base or head == base:
        return {}
    try:
        _cfg.ensure_scheduler_path()
        from scheduler.estimator import ScoreEstimator
        pair = ScoreEstimator(Path(ws))._case_bench_pair(title, head, base)
    except Exception:
        return {}
    if not isinstance(pair, dict):
        return {}
    cases = pair.get("cases") if isinstance(pair.get("cases"), dict) else {}
    coverage = (pair.get("matched") or 0) / max(1, pair.get("official") or 1)
    runs = min(int(pair.get("runs_cand") or 0), int(pair.get("runs_base") or 0))
    confidence = ("measured" if coverage >= CONF_COVERAGE and runs >= CONF_RUNS
                  else "inferred" if cases else "unknown")
    return {"cases": {str(k): v for k, v in cases.items()}, "confidence": confidence,
            "matched": pair.get("matched"), "official": pair.get("official"), "runs": runs,
            "base_sha": base, "geo": pair.get("geo")}


def predictions(ws: Path, title: str, head: str, base: str) -> dict[str, Any]:
    """Predicted per-case times of ``head`` against tbest (``receipt_predict``) when a map is stored."""
    _cfg.ensure_scheduler_path()
    from scheduler import case_map, receipt_predict
    try:
        doc = case_map.current(Path(ws), title, write=False)
        if not case_map.usable(doc):
            return {}
        pred = receipt_predict.predict(Path(ws), title, head, [base], doc=doc, allow_other=False,
                                      write_map=False)
    except Exception:
        return {}
    if not isinstance(pred, dict):
        return {}
    return {"cases": pred.get("cases") or {}, "tbest_observed_at": pred.get("tbest_observed_at"),
            "tbest_source": pred.get("tbest_source"), "coverage": pred.get("coverage"),
            "base_sha": pred.get("base_sha")}


def receipt_cases(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The newest receipt with per-case times and tbest: ``{judge: {ours, tbest, observed_at}}``."""
    receipts = receipts_module()
    for row in reversed(rows):
        times, tbest = receipts.case_times(row), receipts.case_tbest(row)
        if not times or not tbest:
            continue
        return {"sha": str(row.get("sha") or ""), "at": receipts.tbest_observed_at(row),
                "cases": {str(k): {"ours": times.get(k), "tbest": tbest.get(k)} for k in tbest}}
    return {}


def case_rows(ws: Path, title: str) -> list[dict[str, Any]]:
    """One row per timed judge case: ours, tbest, gap, speedup and the head SHA's prediction."""
    rows = receipts_rows(ws, title)
    head = head_sha(ws, title)
    board = ""
    try:
        _cfg.ensure_scheduler_path()
        from scheduler import receipt_predict
        board = str(receipt_predict.board_sha(Path(ws), title) or "")
    except Exception:
        board = ""
    base = board or next((str(r.get("sha") or "") for r in reversed(rows) if receipts_module().beat_board(r)), "")
    pair = speedups(ws, title, head, base)
    pred = predictions(ws, title, head, base) if head else {}
    received = receipt_cases(rows)
    names = labels(ws, title)
    receipts = receipts_module()
    out: list[dict[str, Any]] = []
    keys = list((received.get("cases") or {}).keys()) or list((pred.get("cases") or {}).keys())
    for position, key in enumerate([str(k) for k in keys][:CASE_LINES], start=1):
        got = (received.get("cases") or {}).get(key) or {}
        forecast = (pred.get("cases") or {}).get(key) or {}
        ours = _num(got.get("ours")) if got else _num(forecast.get("t_pred"))
        tbest = _num(got.get("tbest")) or _num(forecast.get("tbest"))
        gap = round((ours - tbest) / tbest, 4) if ours and tbest else None
        points = round(receipts.case_points(ours, tbest), 2) if ours and tbest else None
        out.append({
            "case": names.get(key, f"#{position:02d}"), "judge": key,
            "speedup": _num(pair.get("cases", {}).get(key)) if pair else None,
            "confidence": pair.get("confidence") if pair else None,
            "ours_us": round(ours, 2) if ours else None, "tbest_us": round(tbest, 2) if tbest else None,
            "gap_pct": gap, "points": points,
            "source": "receipt" if got else "predict" if forecast else None,
        })
    return out


def targets(rows: list[dict[str, Any]], weight: float = 1.0) -> list[dict[str, Any]]:
    """Research R8: the cases with the largest ``weight x same-generation gap`` (top three, with numbers)."""
    scored = [r for r in rows if r.get("gap_pct") is not None]
    scored.sort(key=lambda r: (-(float(r["gap_pct"]) * weight), str(r["case"])))

    def headroom(row: dict[str, Any]) -> float:
        points = row.get("points")
        return round(max(0.0, 100.0 - float(points)) * weight, 2) if isinstance(points, (int, float)) else 0.0

    return [{"case": r["case"], "gap_pct": r["gap_pct"], "headroom_pts": headroom(r),
             "ours_us": r.get("ours_us"), "tbest_us": r.get("tbest_us")} for r in scored[:TARGETS]]


def receipts_summary(rows: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """The last ``limit`` receipts: SHA, status, official score, and whether the board moved."""
    receipts = receipts_module()
    out = []
    for row in list(reversed(rows))[:limit]:
        out.append({"sha": _sha7(row.get("sha")), "status": _clip(row.get("status") or "?", 24),
                    "score": receipts.official_score(row), "refresh": receipts.beat_board(row),
                    "submitted_at": str(row.get("submitted_at") or row.get("finished_at") or "")[:19]})
    return out


def data(ws: Path | str, title: str, agent_id: str | None = None) -> dict[str, Any]:
    """Everything the extended card shows, as plain data (never raises)."""
    ws = Path(ws).expanduser().resolve()
    try:
        rows = case_rows(ws, title)
        weight = 1.0
        return {
            "title": title, "worktree": worktree_of(ws, agent_id or ""), "head_sha": head_sha(ws, title),
            "cases": rows, "targets": targets(rows, weight),
            "receipts": receipts_summary(receipts_rows(ws, title)),
        }
    except Exception:
        return {"title": title, "worktree": worktree_of(ws, agent_id or ""), "error": "unavailable",
                "cases": [], "targets": [], "receipts": []}


def render(doc: dict[str, Any]) -> str:
    """The card text, at most ``MAX_EXT_BYTES`` bytes; the rules line stays the last line."""
    from .state_card import RULES_LINE
    cases = doc.get("cases") or []
    lines = [f"EXT {doc.get('title')} @ {_cfg.utc_now().strftime('%H:%M')}"
             + (f" | worktree {doc['worktree']}" if doc.get("worktree") else "")]
    if doc.get("error"):
        lines.append("case detail: unavailable")
    speed = [c for c in cases if c.get("speedup") is not None]
    if speed:
        conf = speed[0].get("confidence") or "unknown"
        body = " · ".join(f"{c['case']} {float(c['speedup']):.3f}x" for c in speed)
        lines.append(f"speedup vs same-generation base ({conf}): {body}")
    gap = [c for c in cases if c.get("gap_pct") is not None]
    if gap:
        body = " · ".join(f"{c['case']} {c.get('ours_us') or '-'}/{c.get('tbest_us') or '-'} "
                          f"{float(c['gap_pct']) * 100:+.1f}%" for c in gap)
        lines.append(f"case gap ours/tbest µs: {body}")
    if doc.get("targets"):
        body = " · ".join(f"{t['case']} {float(t['gap_pct']) * 100:+.1f}% (headroom {t['headroom_pts']} pts)"
                          for t in doc["targets"])
        lines.append(f"targets (largest weight x gap): {body}")
    if doc.get("receipts"):
        body = " · ".join(f"{r.get('status')} {r.get('score') if r.get('score') is not None else '-'} {r.get('sha')}"
                          + (" board" if r.get("refresh") else "") for r in doc["receipts"])
        lines.append("last receipts: " + body)
    lines.append(RULES_LINE)
    return _fit(lines)


def _fit(lines: list[str]) -> str:
    text = "\n".join(lines)
    if len(text.encode("utf-8")) <= MAX_EXT_BYTES:
        return text
    body = "\n".join(lines[:-1]).encode("utf-8")
    budget = MAX_EXT_BYTES - len(lines[-1].encode("utf-8")) - 1
    return body[:budget].decode("utf-8", errors="ignore").rstrip() + "\n" + lines[-1]


def build(ws: Path | str, title: str, agent_id: str | None = None) -> str:
    """The extended card text for one problem (never raises)."""
    return render(data(ws, title, agent_id))
