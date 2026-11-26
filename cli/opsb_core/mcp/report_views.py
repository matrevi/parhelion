"""Shape and list views of the platform report (4.2 D): history, queue, board, bottleneck, env.

``handlers_report`` owns the entry point and the two per-case views; this module owns the views
that read a list or the local environment, plus the shaping helpers both modules use (byte budget,
rounding, clipping, report documents). Every view reuses the service that owns its calculation:
the canonical receipt journal, the queue snapshot with its gate lamps, the board caches, the PMU
card plus KB query, and the local environment. Hard budgets: ``per_case``/``diff`` 4KB, ``env``
1KB; the list views take ``limit``.
"""
from __future__ import annotations

import errno
import json
import math
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .. import __version__ as OPSB_VERSION
from .. import config as _cfg

BIG, ENV_BYTES, LIMIT_MAX = 4096, 1024, 50
FORBIDDEN = ("account_id", "account_label", "user_id", "cookies", "cookie", "token", "session_file")


# ---------------------------------------------------------------- shaping helpers

def _num(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _round(value: Any, digits: int = 3) -> float | None:
    number = _num(value)
    return round(number, digits) if number is not None else None


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _sha7(value: Any) -> str:
    return str(value or "")[:7]


def _bytes(doc: Any) -> int:
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _limit(args: dict[str, Any], default: int = 10) -> int:
    try:
        return max(1, min(LIMIT_MAX, int(args.get("limit"))))
    except (TypeError, ValueError):
        return default


def _trim(doc: dict[str, Any], key: str, budget: int, floor: int = 1) -> dict[str, Any]:
    """Drop trailing rows until the reply fits ``budget`` bytes."""
    rows = doc.get(key) or []
    while len(rows) > floor and _bytes(doc) > budget:
        rows.pop()
        doc["truncated"] = True
    if doc.get("truncated"):
        doc["shown"] = len(rows)
    return doc


def _doc(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _resolve(workspace: Path | str, title: str, value: Any) -> str:
    """The full SHA of a prefix under this problem's results, or the argument unchanged."""
    sha = str(value or "").strip()
    root = Path(workspace) / "results" / title
    if len(sha) < 7 or not root.is_dir():
        return sha
    return next((c.name for c in sorted(root.iterdir())
                 if c.is_dir() and c.name.lower().startswith(sha.lower())), "") or sha


def _titles(workspace: Path | str, problem: str | None, role: str) -> list[str]:
    if problem:
        return [problem]
    if role == "worker":
        raise PermissionError("the problem of this call is unknown: pass problem or use a bound worker")
    from ..services.contest_service import resolve_workspace_meta

    return [str(p.get("title")) for p in resolve_workspace_meta(Path(workspace)).get("problems") or []
            if p.get("title")]


# ---------------------------------------------------------------- history

def _receipt_entry(row: dict[str, Any], receipts: Any) -> dict[str, Any]:
    entry = {
        "sid": str(row.get("submission_id") or ""), "sha": _sha7(row.get("sha")), "status": row.get("status"),
        "score": receipts.official_score(row), "beat_board": receipts.beat_board(row),
        "at": str(row.get("submitted_at") or row.get("finished_at") or "")[:19],
        "note": _clip(row.get("note") or row.get("detail") or "", 80),
    }
    times, tbest = receipts.case_times(row), receipts.case_tbest(row)
    if not (times and tbest):
        return entry
    ratios: list[float] = []
    cases: list[list[Any]] = []
    for key in list(tbest)[:20]:
        item = [key, _round(times.get(key), 2), _round(tbest.get(key), 2)]
        comparable = _num(item[1]) and _num(item[2]) and item[1] > 0 and item[2] > 0
        item.append(round(item[2] / item[1], 3) if comparable else None)
        if item[3] is not None:
            ratios.append(item[3])
        cases.append(item)
    entry.update(cases=cases, mean_ratio=round(sum(ratios) / len(ratios), 3) if ratios else None)
    return entry


def _history(workspace: Path | str, problem: str | None, args: dict[str, Any], role: str) -> dict[str, Any]:
    _cfg.ensure_scheduler_path()
    from scheduler import receipts

    ws, limit = Path(workspace), _limit(args)
    titles = _titles(ws, problem, role)
    journal = receipts.canonical(receipts.read_rows(receipts.ledger_path(ws)))
    problems: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    for title in titles:
        rows = [r for r in journal if str(r.get("title") or "") == title]
        judged = [r for r in rows if receipts.judged(r)]
        scores = [s for s in (receipts.official_score(r) for r in judged) if s is not None]
        latest = judged[-1] if judged else {}
        problems.append({
            "title": title, "judged": len(judged), "rows": len(rows),
            "best_judged": max(scores) if scores else None,
            "latest": {"sha": _sha7(latest.get("sha")), "status": latest.get("status"),
                       "score": receipts.official_score(latest) if latest else None},
        })
        if not problem:
            continue
        timeline = [_receipt_entry(row, receipts) for row in rows[-limit:]]
    out = {
        "view": "history", "problems": problems, "receipts": timeline,
        "columns": ["sid", "sha", "status", "score", "beat_board", "at", "note", "cases", "mean_ratio"],
        "case_columns": ["case", "ours_us", "tbest_us", "ratio"],
    }
    return _trim(out, "receipts", BIG, floor=0)


# ---------------------------------------------------------------- queue

def _queue_item(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("evaluated", "safety_ok", "noise_ok", "circuit_ok", "dispatch_ok", "all_pass", "delta_expected",
            "ucb_delta", "calibration_trust", "explore_eligible", "wspt_score")
    gates = item.get("gates") if isinstance(item.get("gates"), dict) else {}
    card = {
        "id": item.get("id"), "kind": item.get("kind"), "title": item.get("title"),
        "sha": _sha7(item.get("sha")), "badge": item.get("badge"), "raw_status": item.get("raw_status"),
        "current_score": item.get("current_score"), "estimated_score": item.get("estimated_score"),
        "actual_score": item.get("actual_score"), "delta_score": item.get("delta_score"),
        "breakthrough_delta": item.get("breakthrough_delta"), "queued_at": item.get("queued_at"),
        "finished_at": item.get("finished_at"), "submission_id": item.get("submission_id"),
        "phase": item.get("phase"), "elapsed_sec": item.get("elapsed_sec"),
        "note": _clip(item.get("note") or "", 120), "detail": _clip(item.get("detail") or "", 160),
        "next_step": _clip(item.get("next_step") or "", 160), "gates": {k: gates.get(k) for k in keys},
        "can_approve": item.get("can_approve"), "can_skip": item.get("can_skip"),
        "can_restore": item.get("can_restore"), "archive_rank": item.get("archive_rank"),
        "experience_id": item.get("experience_id"),
    }
    return {key: value for key, value in card.items() if key not in FORBIDDEN}


def _queue(workspace: Path | str, problem: str | None, args: dict[str, Any], role: str) -> dict[str, Any]:
    from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
    from ..services.queue_service import compute_workspace_snapshot

    ws, limit = Path(workspace), _limit(args, 20)
    allowed = set(_titles(ws, problem, role))
    snap = compute_workspace_snapshot(ws, _cfg.ConfigRepository.load())
    try:
        bridge = invoke_bridge(ws, "status", ["--offline"], use_cache=True)
        if isinstance(bridge, dict):
            merge_bridge_enrichment(snap, bridge)
    except Exception:
        pass
    sched = snap.get("scheduler") or {}
    items = [it for it in snap.get("submission_queue") or [] if str(it.get("title") or "") in allowed]
    out = {
        "view": "queue", "allowed": sorted(allowed), "threshold": sched.get("breakthrough_threshold"),
        "count": len(items), "by_kind": dict(Counter(str(item.get("kind")) for item in items)),
        "active_submissions": len((sched.get("pipeline") or {}).get("active_submissions") or []),
        "items": [_queue_item(item) for item in items[:limit]],
    }
    return _trim(out, "items", BIG)


# ---------------------------------------------------------------- board / bottleneck / env

def _board(workspace: Path | str, problem: str | None, args: dict[str, Any], role: str) -> dict[str, Any]:
    from ..services import board_refresh_service, ranking_service
    from ..services.contest_service import resolve_workspace_meta

    ws, limit = Path(workspace), _limit(args, 30)
    problems = {str(p.get("title")): p for p in resolve_workspace_meta(ws).get("problems") or []
                if p.get("title")}
    snapshot, live, _ = board_refresh_service.current_board(ws, problems)
    payload = _cfg.read_json(board_refresh_service.live_board_path(ws), {}) or {}
    observed = str(payload.get("fetched_at") or "")[:19] if isinstance(payload, dict) else ""
    ranking, rows = ranking_service.team_ranking(ws, allow_fetch=False) or {}, []
    for title in sorted(set(_titles(ws, problem, role)) & (set(problems) | set(live) | set(snapshot))):
        row, fallback = live.get(title) or {}, snapshot.get(title) or {}
        tbest = [v for v in row.get("tbest") or [] if _num(v) is not None]
        card = {
            "title": title, "listed": row.get("listed", fallback.get("listed")),
            "score": _round(row.get("our_score", fallback.get("score")), 2),
            "rank": row.get("our_rank", fallback.get("rank")), "total_teams": row.get("ranking_total"),
            "board_submission_id": row.get("board_submission_id", fallback.get("submission_id")),
            "frontier": {"top1": _round(row.get("frontier_top1"), 2),
                         "top5": _round(row.get("frontier_top5"), 2)},
            "observed_at": observed or None,
        }
        if problem:
            card["tbest"] = [{"case": i, "tbest_us": _round(v, 3)} for i, v in enumerate(tbest[:limit], 1)]
        rows.append(card)
    team = {"rank": ranking.get("rank"), "score": _round(ranking.get("score"), 2),
            "total": ranking.get("total"), "state": ranking.get("ranking_state"),
            "fetched_at": str(ranking.get("fetched_at") or "")[:19] or None}
    out = {"view": "board", "observed_at": observed or None, "team": team, "problems": rows}
    while _bytes(out) > BIG and out["problems"] and out["problems"][0].get("tbest"):
        out["problems"][0]["tbest"].pop()
    return out


def _bottleneck(workspace: Path | str, problem: str, args: dict[str, Any]) -> dict[str, Any]:
    from ..fast_feedback import pmu_torch_card

    ws, title = Path(workspace), str(problem)
    sha = _resolve(workspace, title, args.get("sha")) or None
    card = pmu_torch_card.build_pmu_torch_card(ws, title, sha=sha, top_n=3)
    pmu = card.get("pmu") or {}
    active = {name: _num(pmu.get(name)) for name in (
        "aiv_vec_ratio", "aiv_mte2_ratio", "aiv_mte3_ratio", "aiv_scalar_ratio", "bank_conflict_ratio")}
    active = {name: value for name, value in active.items() if value is not None}
    category = str(card.get("dominant_bottleneck") or "unknown")
    pipe = {"vec-bound": "aiv_vec_ratio", "mte2-bound": "aiv_mte2_ratio", "mte3-bound": "aiv_mte3_ratio",
            "scalar-bound": "aiv_scalar_ratio", "bank-conflict": "bank_conflict_ratio"}.get(category)
    limited = active.get(pipe) if pipe else None
    fixes: list[list[Any]] = []
    if category not in ("unknown", "balanced"):
        try:
            from ..knowledge.query import query_kb

            fixes = [[e.get("id"), _clip(e.get("title"), 80), e.get("confidence"), _clip(e.get("summary"), 160)]
                     for e in query_kb(bottlenecks=[category], limit=3, auto_seed=False).get("results") or []]
        except Exception:
            fixes = []
    run_dir = ws / "results" / title / (card.get("sha") or sha or "")
    runs = sorted(run_dir.glob("bench-runs/*/report.json"),
                  key=lambda path: str(_doc(path).get("finished_at") or ""))
    runs = [path for path in runs if _doc(path).get("formal") is True] or runs
    log = run_dir / "validation.log"
    raw = {"report": str(runs[-1]) if runs else None, "log": str(log) if log.is_file() else None,
           "run_dir": str(run_dir) if run_dir.is_dir() else None}
    metrics = {name: round(value * 100, 1) for name, value in active.items()}
    top = [{"bucket": b.get("bucket"), "ours_us": b.get("ours_us"), "torch_us": b.get("torch_us"),
            "ratio_vs_torch": b.get("ratio_vs_torch"), "headroom": b.get("headroom_score")}
           for b in (card.get("top_buckets") or [])[:3]]
    return {"view": "bottleneck", "problem": title, "sha": _sha7(card.get("sha") or sha), "category": category,
            "limited_resource_pct": round(limited * 100, 1) if limited is not None else None,
            "pmu_available": bool(card.get("pmu_available")), "pmu": metrics, "top_metrics": top,
            "fix_columns": ["id", "title", "confidence", "summary"], "fixes": fixes, "raw": raw}


def _env(workspace: Path | str) -> dict[str, Any]:
    ws = Path(workspace)
    state = _cfg.read_json(_cfg.DAEMON_STATE_PATH, {}) or {}
    pid = int(state.get("pid") or 0) if isinstance(state, dict) else 0
    try:
        os.kill(pid, 0) if pid > 0 else None
        running = pid > 0
    except OSError as exc:
        running = pid > 0 and exc.errno == errno.EPERM
    daemon = {"running": running, "pid": pid or None,
              "started_at": str(state.get("started_at") or "")[:19] if running else None}
    workspace_info = {"root": str(ws), "results": str(ws / "results"),
                      "state_dir": str(_cfg.workspace_state_dir(ws))}
    out = {"view": "env", "platform": {"version": OPSB_VERSION, "root": str(_cfg.SUPERVISOR_ROOT)},
           "daemon": daemon, "workspace": workspace_info,
           "disk": {"free_gb": round(shutil.disk_usage(str(ws)).free / 1e9, 1) if ws.is_dir() else None}}
    if _bytes(out) <= ENV_BYTES:
        return out
    return {key: value for key, value in out.items() if key != "disk"}
