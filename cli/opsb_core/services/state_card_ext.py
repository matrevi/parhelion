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


def judge_to_local(ws: Path, title: str) -> dict[str, str]:
    """``{judge_key: local_id}`` from the case map: the gate's pairing is keyed by local case ids,
    the card rows by judge keys, so a speedup is looked up through this map."""
    try:
        _cfg.ensure_scheduler_path()
        from scheduler import case_map
        doc = case_map.current(Path(ws), title, write=False)
    except Exception:
        return {}
    return {str(p["judge"]): str(p["local"]) for p in (doc or {}).get("pairs") or []
            if isinstance(p, dict) and p.get("judge") and p.get("local")}


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


def base_of(ws: Path, title: str) -> str:
    """The same-generation baseline: the SHA that last refreshed the board, else the newest board mover."""
    rows = receipts_rows(ws, title)
    board = ""
    try:
        _cfg.ensure_scheduler_path()
        from scheduler import receipt_predict
        board = str(receipt_predict.board_sha(Path(ws), title) or "")
    except Exception:
        board = ""
    return board or next((str(r.get("sha") or "") for r in reversed(rows)
                          if receipts_module().beat_board(r)), "")


def case_rows(ws: Path, title: str) -> list[dict[str, Any]]:
    """One row per timed judge case: ours, tbest, gap, speedup and the head SHA's prediction."""
    rows = receipts_rows(ws, title)
    head = head_sha(ws, title)
    base = base_of(ws, title)
    pair = speedups(ws, title, head, base)
    local_of = judge_to_local(ws, title) if pair else {}
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
            "speedup": _num(pair.get("cases", {}).get(local_of.get(key, key))) if pair else None,
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
            "base_sha": base_of(ws, title), "cases": rows,
            "targets": targets(rows, weight), "receipts": receipts_summary(receipts_rows(ws, title)),
        }
    except Exception:
        return {"title": title, "worktree": worktree_of(ws, agent_id or ""), "error": "unavailable",
                "cases": [], "targets": [], "receipts": []}


def render(doc: dict[str, Any]) -> str:
    """The card text, at most ``MAX_EXT_BYTES`` bytes; the rules line stays the last line."""
    from .state_card import RULES_LINE
    cases = doc.get("cases") or []
    head = str(doc.get("head_sha") or "")[:7]
    lines = [f"EXT {doc.get('title')} @ {_cfg.utc_now().strftime('%H:%M')}"
             + (f" | worktree {doc['worktree']}" if doc.get("worktree") else "")]
    if head:
        lines.append(f"head: {head}" + (f" | base: {str(doc['base_sha'])[:7]}" if doc.get("base_sha") else ""))
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
    elif not speed:
        lines.append("case gap ours/tbest µs: n/a (no receipt with per-case times yet)")
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


# --- 4.3 P27: the intel block of the start-up pack ---------------------------------------------

INTEL_MAX_BYTES = 700
SIX_SHOTS = 6  # AGENTS.md 6: six paid attempts without a gain is the handover trigger
RECENT_SHOTS = 3
MECHANISM_CHARS = 44


def weight_of(ws: Path, title: str) -> int:
    """The problem's difficulty weight (the multiplier of the weighted space)."""
    try:
        from ..drivers.brief import resolve_problem_repo
        meta = resolve_problem_repo(Path(ws), title)[2] or {}
        return int(meta.get("difficulty") or _cfg.DEFAULT_DIFFICULTIES.get(title, 2))
    except Exception:
        return int(_cfg.DEFAULT_DIFFICULTIES.get(title, 2))


def standing(ws: Path, title: str, weight: int = 1) -> dict[str, Any]:
    """Leader score, our board score and the weighted gap; every source is a local cache, no network."""
    _cfg.ensure_scheduler_path()
    try:
        from ..services.board_refresh_service import live_board_path
        rows = (_cfg.read_json(live_board_path(Path(ws)), {}) or {}).get("problems") or {}
        row = rows.get(title) if isinstance(rows, dict) else None
        if isinstance(row, dict) and (row.get("frontier_top1") is not None or row.get("our_score") is not None):
            leader, mine = _num(row.get("frontier_top1")), _num(row.get("our_score"))
            return {"leader": leader, "board": mine, "weight": weight,
                    "gap_weighted": round(max(0.0, (leader - mine)) * weight, 2)
                    if leader is not None and mine is not None else None,
                    "source": "live-board"}
    except Exception:
        pass
    try:  # the slim board cache brief.py's leader line reads; absent when the contest never stored one
        from ..services.contest_service import load_slim_boards
        boards = load_slim_boards(Path(ws) / "results" / "scoring-evidence" / "board-summary.json")
        rows = ((boards.get(title) or {}).get("rows") or []) if isinstance(boards, dict) else []
        snapshot = _cfg.read_json(Path(ws) / "results" / "score-snapshot.json", {}) or {}
        mine = _num((snapshot.get(title) or {}).get("score")) if isinstance(snapshot, dict) else None
        leader = _num((rows[0] or {}).get("score")) if rows else None
        if leader is not None or mine is not None:
            return {"leader": leader, "board": mine, "weight": weight,
                    "gap_weighted": round(max(0.0, (leader - mine)) * weight, 2)
                    if leader is not None and mine is not None else None,
                    "source": "board-summary"}
    except Exception:
        pass
    return {"leader": None, "board": None, "weight": weight, "gap_weighted": None, "source": "none"}


MECHANISM_TYPES = ("hypothesis", "experiment", "insight", "decision")


def _mechanism(rows: list[dict[str, Any]], sha: str, note: Any) -> str:
    """The claim of the hypothesis that produced ``sha``, else the receipt note (the plan's order).

    ``measurement`` rows are the platform's own score lines ("official Pass ..."), not a mechanism, so
    they never win here; a hypothesis or experiment on the same SHA does.
    """
    prefix = str(sha or "")[:7]
    if prefix:
        for row in reversed(rows):
            if str(row.get("type") or "") not in MECHANISM_TYPES:
                continue
            entry_sha = str(row.get("sha") or "")
            if entry_sha and (entry_sha.startswith(prefix) or prefix.startswith(entry_sha[:7])):
                if str(row.get("claim") or "").strip():
                    return _clip(row["claim"], MECHANISM_CHARS)
    text = str(note or "").strip()
    if text.lower().startswith("official "):
        return "-"
    return _clip(text, MECHANISM_CHARS) if text else "-"


def ledger_state(ws: Path, title: str) -> dict[str, Any]:
    """Open hypothesis ids, attempts left under ``ledger.budget``, and the six-shot no-gain count."""
    try:
        from ..ledger import budget as _budget
        from ..ledger import store as _store
        from ..ledger import view as _view
        rows = _store.read_problem(Path(ws), title)
    except Exception:
        return {"open": [], "budget_left": None, "budget": None, "no_gain": None}
    open_rows = _view.query(rows, "open")
    ids = [f"{r.get('id')}" for r in open_rows if r.get("id")][:3]
    left = _budget.ATTEMPT_BUDGET
    if open_rows:
        newest = max(open_rows, key=_view.order_key)
        left = max(0, _budget.ATTEMPT_BUDGET - len(_budget.hypothesis_attempts(rows, str(newest.get("id") or ""))))
    elif rows:
        every = _budget.attempts(rows)
        if every:
            last = every[-1]["bottleneck"]
            left = max(0, _budget.ATTEMPT_BUDGET - sum(1 for a in every if a["bottleneck"] == last))
    return {"open": ids, "budget_left": left, "budget": _budget.ATTEMPT_BUDGET,
            "no_gain": no_gain_streak(receipts_rows(Path(ws), title))}


def no_gain_streak(rows: list[dict[str, Any]], limit: int = SIX_SHOTS) -> int:
    """Consecutive judged receipts, newest first, that did not refresh the board."""
    receipts = receipts_module()
    run = 0
    for row in reversed(rows):
        if not receipts.judged(row):
            continue
        if receipts.beat_board(row):
            break
        run += 1
        if run >= limit:
            break
    return run


def rule_line(ws: Path, title: str) -> str:
    """4.3 P22: one line with the board mode and sigma hat, when the snapshot exists."""
    try:
        from .judge_rules import status_line
        line = status_line(Path(ws), [{"title": title}]) or {}
    except Exception:
        return ""
    if not line:
        return ""
    parts = [str(line.get("board_mode") or "unknown")]
    if line.get("sigma_hat") is not None:
        parts.append(f"sigma {float(line['sigma_hat']):.2f}")
    tail = ""
    if line.get("scope"):
        tail = f" ({line['scope']}" + (f", df {line['df']}" if line.get("df") else "") + ")"
    return "rule: " + ", ".join(parts) + tail


def intel(ws: Path | str, title: str, agent_id: str | None = None) -> dict[str, Any]:
    """The four intel items of 4.3 P27 as plain data (never raises)."""
    root = Path(ws).expanduser().resolve()
    try:
        weight = weight_of(root, title)
        rows = receipts_rows(root, title)
        ledger = ledger_rows_for(root, title)
        recent = []
        receipts = receipts_module()
        for row in list(reversed(rows))[:RECENT_SHOTS]:
            if not receipts.judged(row):
                continue
            recent.append({"sha": _sha7(row.get("sha")), "score": receipts.official_score(row),
                           "high": receipts.beat_board(row),
                           "mechanism": _mechanism(ledger, str(row.get("sha") or ""),
                                                   row.get("note") or row.get("detail"))})
        return {"title": title, "weight": weight, "standing": standing(root, title, weight),
                "recent": recent, "ledger": ledger_state(root, title), "rule": rule_line(root, title)}
    except Exception:
        return {"title": title, "error": "unavailable"}


def ledger_rows_for(ws: Path, title: str) -> list[dict[str, Any]]:
    try:
        from ..ledger import store as _store
        return _store.read_problem(Path(ws), title)
    except Exception:
        return []


def render_intel(doc: dict[str, Any]) -> str:
    """The intel block: standing, the last three shots, the stop condition and the rule line."""
    if doc.get("error") or not doc.get("title"):
        return ""
    stand = doc.get("standing") or {}
    weight = int(stand.get("weight") or 1)
    leader, board, gap = stand.get("leader"), stand.get("board"), stand.get("gap_weighted")
    head = f"INTEL {doc['title']} (weight x{weight}): leader " + (
        f"{leader:.2f}" if isinstance(leader, (int, float)) else "n/a")
    head += ", board " + (f"{board:.2f}" if isinstance(board, (int, float)) else "n/a")
    head += ", weighted gap " + (f"{gap:.2f}" if isinstance(gap, (int, float)) else "n/a")
    lines = [head]
    recent = doc.get("recent") or []
    if recent:
        lines.append("recent: " + " · ".join(
            f"{r['sha']} {r['mechanism']} " + (f"{r['score']:.2f}" if isinstance(r.get("score"), (int, float))
                                               else "-") + (" high" if r.get("high") else "")
            for r in recent))
    ledger = doc.get("ledger") or {}
    stop = []
    if ledger.get("open"):
        stop.append("open " + ",".join(str(x) for x in ledger["open"]))
    if ledger.get("budget_left") is not None:
        stop.append(f"attempts left {ledger['budget_left']}/{ledger['budget']}")
    if ledger.get("no_gain") is not None:
        stop.append(f"{ledger['no_gain']}/{SIX_SHOTS} paid shots without a new high"
                    + (" -> handover threshold" if ledger["no_gain"] >= SIX_SHOTS else ""))
    if not ledger.get("open") and ledger.get("budget_left") is not None:
        stop.append("no open hypothesis: record one before the next change")
    if stop:
        lines.append("stop when: " + " · ".join(stop))
    if doc.get("rule"):
        lines.append(str(doc["rule"]))
    text = "\n".join(lines)
    if len(text.encode("utf-8")) <= INTEL_MAX_BYTES:
        return text
    out: list[str] = []
    for line in lines:
        candidate = "\n".join(out + [line])
        if len(candidate.encode("utf-8")) > INTEL_MAX_BYTES:
            break
        out.append(line)
    if not out:  # a budget below one line still keeps the head of the standing line
        return lines[0].encode("utf-8")[: max(0, INTEL_MAX_BYTES)].decode("utf-8", errors="ignore").rstrip()
    return "\n".join(out)


def intel_text(ws: Path | str, title: str, agent_id: str | None = None) -> str:
    """The rendered intel block (at most ``INTEL_MAX_BYTES``); empty when nothing is known yet."""
    try:
        return render_intel(intel(ws, title, agent_id))
    except Exception:
        return ""
