"""Shape and list views of the platform report (4.2 D): history, queue, board, bottleneck, env.

``handlers_report`` owns the entry point and the two per-case views; this module owns the views
that read a list or the local environment, plus the shaping helpers both modules use (byte budget,
rounding, clipping, report documents). Every view reuses the service that owns its calculation:
the canonical receipt journal, the queue snapshot with its gate lamps, the board caches, the PMU
card plus KB query, and the local environment. ``history`` and ``queue`` page with
``offset``/``limit`` or ``cursor``; a page above the MCP output cap is re-cut by
``mcp.bound.page`` before anything is spilled.
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
from .paging import paginate

BIG, ENV_BYTES, LIMIT_MAX = 4096, 1024, 50
RULES_BYTES = 2048
CLOSE_BYTES = 3072
RULES_FORMULA = "case = 100/(1+ln(t/tbest)/ln1.5); problem = mean over the roster"
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

    ws = Path(workspace)
    titles = _titles(ws, problem, role)
    journal = receipts.canonical(receipts.read_rows(receipts.ledger_path(ws)))
    problems: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    total = 0
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
        total = len(rows)
        # Newest first: offset walks back in time while the default page stays the latest receipts.
        timeline = [_receipt_entry(row, receipts) for row in reversed(rows)]
    out = {
        "view": "history", "problems": problems, "receipts": timeline,
        "columns": ["sid", "sha", "status", "score", "beat_board", "at", "note", "cases", "mean_ratio"],
        "case_columns": ["case", "ours_us", "tbest_us", "ratio"],
    }
    if problem:
        paginate(out, "receipts", args, total=total, default=10, maximum=LIMIT_MAX)
    return out


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

    ws = Path(workspace)
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
        "items": [_queue_item(item) for item in items],
    }
    return paginate(out, "items", args, total=len(items), default=20, maximum=LIMIT_MAX)


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


# ---------------------------------------------------------------- rules (4.3 P22)

def _board_sid(ws: Path, title: str) -> str:
    """The submission the board currently shows for this problem, if any."""
    live = (_doc(ws / "results" / "scoring-evidence" / "live-board-tbest.json").get("problems") or {})
    sid = str((live.get(title) or {}).get("board_submission_id") or "")
    return sid or str(((_doc(ws / "results" / "score-snapshot.json").get(title) or {})).get("submission_id") or "")


def _tbest_change(rows: list[dict[str, Any]], receipts: Any) -> dict[str, Any]:
    """When the tbest this problem is scored against last moved, from the receipt snapshots."""
    seen: dict[str, Any] = {}
    observed, changed_at, changed = None, None, 0
    for row in sorted(rows, key=lambda r: str(receipts.tbest_observed_at(r) or "")):
        best = receipts.case_tbest(row)
        if not best:
            continue
        stamp = str(receipts.tbest_observed_at(row) or "")[:19] or None
        if seen:
            moved = sum(1 for key, value in best.items() if seen.get(key) != value)
            if moved:
                changed_at, changed = stamp, moved
        seen, observed = best, stamp
    return {"cases": len(seen), "observed_at": observed, "changed_at": changed_at, "changed_cases": changed}


def _rule_split(rows: list[dict[str, Any]], receipts: Any, board_sid: str) -> dict[str, Any]:
    """Public/hidden split of the best and the board receipt, only where the identity verifies."""
    accepted: list[dict[str, Any]] = []
    worst, public_n = 0.0, 0
    for row in rows:
        times, tbest = receipts.case_times(row), receipts.case_tbest(row)
        score = receipts.official_score(row)
        if not times or not tbest or not score or score <= 0:
            continue
        computed = receipts.judge_score(times, tbest)
        if computed is None or abs(computed - score) > 0.01:
            continue  # the stored tbest is not the one this score was judged against
        public, _ = receipts.public_times(row)
        # `public_times_us` lists the first timed cases in judge order, so the public slice is the
        # timed prefix; the roster can hold untimed cases inside it and those score nothing.
        timed = [key for key in times if times[key] is not None]
        if not public or len(public) >= len(timed):
            continue
        head = timed[:len(public)]
        if any(tbest.get(key) is None for key in head):
            continue
        if any(abs(times[key] - seen) > 1e-6 for key, seen in zip(head, public)):
            continue  # the public slice is not this submission's timed prefix
        visible = round(sum(receipts.case_points(times[key], tbest[key]) for key in head) / len(tbest), 2)
        worst, public_n = max(worst, abs(computed - score)), len(public)
        accepted.append({"sid": str(row.get("submission_id") or ""), "sha": _sha7(row.get("sha")),
                         "score": round(float(score), 2), "public": visible,
                         "hidden": round(float(score) - visible, 2)})
    if not accepted:
        return {}
    doc: dict[str, Any] = {"verified": {"rows": len(accepted), "public_cases": public_n,
                                        "max_err": round(worst, 4)}}
    for name, item in (("best", max(accepted, key=lambda row: row["score"])),
                       ("board", next((row for row in accepted if row["sid"] == board_sid), None))):
        if item is not None:
            doc[name] = {key: item[key] for key in ("sha", "score", "public", "hidden")}
    return doc


def _fit(doc: dict[str, Any], budget: int, optional: tuple[str, ...], rows: str = "") -> dict[str, Any]:
    """Drop the optional parts, then trailing rows, until the reply fits ``budget`` bytes."""
    for key in optional:
        if _bytes(doc) <= budget:
            return doc
        if key in doc:
            doc.pop(key)
            doc["truncated"] = True
    while rows and _bytes(doc) > budget and len(doc.get(rows) or []) > 1:
        doc[rows].pop()
        doc["truncated"] = True
    return doc


def _rules(workspace: Path | str, problem: str | None, args: dict[str, Any], role: str) -> dict[str, Any]:
    _cfg.ensure_scheduler_path()
    from ..services import judge_rules
    from scheduler import judge_noise, receipts

    ws = Path(workspace)
    stored = judge_rules.snapshots(ws)
    if not problem:
        out: dict[str, Any] = {
            "view": "rules", "formula": RULES_FORMULA,
            "columns": ["title", "mode", "cases", "sigma_hat", "fetched_at"], "problems": [],
        }
        for title in sorted(set(_titles(ws, problem, role)) | set(stored)):
            doc = stored.get(title) or {}
            if not doc:
                continue
            out["problems"].append([
                title, doc.get("ranking_submission_mode"), (doc.get("testcases") or {}).get("total"),
                _round((judge_noise.sigma_for(ws, title) or {}).get("sigma"), 2),
                str(doc.get("fetched_at") or "")[:10] or None,
            ])
        if not out["problems"]:
            out["reason"] = "no rule snapshot yet: run `opsb workspace rules --refresh`"
        return _fit(out, RULES_BYTES, ("formula",), rows="problems")
    title = str(problem)
    doc = stored.get(title) or {}
    rows = receipts.load(ws, title)
    noise = judge_noise.sigma_for(ws, title)
    out: dict[str, Any] = {
        "view": "rules", "problem": title, "formula": RULES_FORMULA,
        "rule": {key: doc[key] for key in ("score_mode", "use_baseline", "ranking_submission_mode",
                                           "iterations", "judge_revision", "ranking_testcase_count",
                                           "testcases", "time_limit", "fetched_at") if key in doc},
        "flips": judge_noise.flip_count(rows, title),  # 4.3 P24: same-source verdict changes
        "tbest": _tbest_change(rows, receipts),
        "noise": ({"sigma_hat": _round(noise.get("sigma"), 2), "df": noise.get("df"),
                   "scope": noise.get("scope")} if noise else {}),
        "split": _rule_split(rows, receipts, _board_sid(ws, title)),
    }
    if not doc:
        out["reason"] = "no rule snapshot yet: run `opsb workspace rules --refresh`"
    out = {key: value for key, value in out.items() if value not in ({}, None)}
    return _fit(out, RULES_BYTES, ("split", "tbest", "noise", "flips", "formula"))


# ---------------------------------------------------------------- sweep (4.3 P27)

SWEEP_BYTES = 4096
SWEEP_METRICS = ("size", "compile", "cold", "pmu")
SWEEP_COLUMNS: dict[str, str] = {
    "size": "size_bytes", "compile": "compile_sec", "cold": "cold_tax_us", "pmu": "pmu",
}
_PMU_PIPES = ("aiv_vec_ratio", "aiv_mte2_ratio", "aiv_mte3_ratio", "aiv_scalar_ratio",
              "bank_conflict_ratio")


def _board_receipt(ws: Path, title: str, receipts: Any) -> dict[str, Any]:
    """The receipt the board currently shows for this problem (empty when nothing is on the board)."""
    sid = _board_sid(ws, title)
    if not sid:
        return {}
    return next((r for r in receipts.canonical(receipts.read_rows(receipts.ledger_path(ws)))
                 if str(r.get("submission_id") or "") == sid), {})


def _formal_sha(ws: Path, title: str) -> str:
    """SHA of this problem's newest formal bench run (``formal: true``), or ``""``."""
    root = ws / "results" / title
    newest: tuple[str, str] = ("", "")
    for path in (root.glob("*/bench-runs/*/report.json") if root.is_dir() else []):
        doc = _doc(path)
        if doc.get("formal") is not True:
            continue
        stamp = str(doc.get("finished_at") or "")
        if stamp >= newest[0]:
            newest = (stamp, str(doc.get("sha") or path.parents[2].name))
    return newest[1]


def _sweep_value(card: dict[str, Any], metric: str) -> Any:
    """The one number (or short string) this metric takes from a fast-feedback card."""
    elf = ((card.get("elf_guard") or {}).get("metrics") or {})
    cold = ((card.get("cold3") or {}).get("metrics") or {})
    if metric == "size":
        return elf.get("kernel_o_sum_bytes")
    if metric == "compile":
        return _round(elf.get("phase_a_cpu_sec"), 2)
    if metric == "cold":
        return _round(cold.get("cold_tax_us"), 2)
    pmu = card.get("pmu") or {}
    ratios = {name: _num(pmu.get(name)) for name in _PMU_PIPES}
    ratios = {name: value for name, value in ratios.items() if value is not None}
    if not ratios:
        return None
    pipe = max(ratios, key=lambda name: ratios[name])
    return f"{(card.get('dominant_bottleneck') or pipe).replace('_ratio', '')} {ratios[pipe] * 100:.1f}%"


def _sweep(workspace: Path | str, args: dict[str, Any], role: str) -> dict[str, Any]:
    """Cross-problem metric sweep: one row per problem, read from stored runs and cards (4.3 P27).

    Orchestrator only. Nothing is measured here: the numbers come from the bench-run reports and the
    stored ``fast_feedback`` cards (ELF/size, cold3, PMU), so no device run is triggered. The public
    case times are the online ones of the receipt the board shows, and are omitted when there is none.
    """
    if role != "orchestrator":
        raise PermissionError("view=sweep is orchestrator only")
    metric = str(args.get("metric") or "size").strip().lower()
    if metric not in SWEEP_METRICS:
        raise ValueError("metric must be one of " + ", ".join(SWEEP_METRICS))
    _cfg.ensure_scheduler_path()
    from ..fast_feedback import pmu_torch_card
    from ..services.contest_service import resolve_workspace_meta
    from scheduler import receipts

    ws = Path(workspace)
    journal = receipts.canonical(receipts.read_rows(receipts.ledger_path(ws)))
    # The manifest names the problems; a workspace without one (an older contest layout) still has
    # receipts, and a problem with a receipt is exactly what a cross-problem sweep is about.
    titles = {str(p.get("title")) for p in resolve_workspace_meta(ws).get("problems") or [] if p.get("title")}
    titles |= {str(r.get("title")) for r in journal if r.get("title")}
    titles = sorted(titles)
    rows: list[list[Any]] = []
    for title in titles:
        sid = _board_sid(ws, title)
        board_row = next((r for r in journal if sid and str(r.get("submission_id") or "") == sid), {})
        board_sha = _sha7(board_row.get("sha"))
        board_score = _round(receipts.official_score(board_row), 2) if board_row else None
        formal = _formal_sha(ws, title)
        try:
            card = pmu_torch_card.build_pmu_torch_card(ws, title, sha=formal or None, top_n=3)
        except Exception:
            card = {}
        value = _sweep_value(card, metric) if card else None
        basis = "formal" if formal and card.get("sha") == formal else ("latest" if card else "-")
        public = [round(v, 2) for v in receipts.public_times(board_row)[0]][:5] if board_row else None
        rows.append([title, board_sha, board_score, _sha7(formal), basis, value, public])
    out: dict[str, Any] = {
        "view": "sweep", "metric": metric,
        "columns": ["title", "board_sha", "board_score", "run_sha", "basis", SWEEP_COLUMNS[metric],
                    "public_us"],
        "problems": rows,
        "source": "stored bench-run reports and fast_feedback cards; no device run",
    }
    return _fit(out, SWEEP_BYTES, ("source",), rows="problems")

CLOSE_COLUMNS = ("title", "mode", "board_sha", "board_score", "best_sha", "best_score", "gap",
                 "sigma_hat", "restore", "merge", "tag")


def _close(workspace: Path | str, problem: str | None, args: dict[str, Any], role: str) -> dict[str, Any]:
    """4.3 P25 closing reconciliation: the three lists plus one row per problem, ``<=CLOSE_BYTES``.

    The numbers come from ``scheduler.close_sweep``: the board is replayed as of the close, so a
    post-contest re-score of the live snapshot does not move the G5 gap. A worker reads its own
    problem only; the three lists are filtered with the same rule as the table.
    """
    _cfg.ensure_scheduler_path()
    from scheduler import close_sweep
    from scheduler.estimator import ScoreEstimator

    ws = Path(workspace)
    keep = {title.lower() for title in _titles(ws, problem, role)}
    doc = close_sweep.sweep(ws, ScoreEstimator(ws))
    rows = [row for row in doc["problems"] if str(row["title"]).lower() in keep]
    listed = {key: {str(r["title"]).lower() for r in doc[key]} for key in ("restore", "merge", "tag")}
    restore = [[r["title"], r["best_sha"], r["gap"], r["sigma_hat"]] for r in doc["restore"]
               if str(r["title"]).lower() in keep]
    merge = [[r["title"], r["best_sha"], r["branches"]] for r in doc["merge"] if str(r["title"]).lower() in keep]
    tag = [[r["title"], r["tag"], r["target"]] for r in doc["tag"] if str(r["title"]).lower() in keep]
    out: dict[str, Any] = {
        "view": "close", "as_of": str(doc["as_of"])[:19] + "Z", "columns": list(CLOSE_COLUMNS),
        "closing": {key: doc["closing"].get(key) for key in
                    ("contest_end", "window_min", "restore_boost", "in_window", "minutes_left")},
        "restore": restore, "merge": merge, "tag": tag,
        "problems": [[r["title"], r["mode"], r["board_sha"], r["board_score"], r["best_sha"],
                      r["best_score"], r["gap"], r["sigma_hat"],
                      *[str(r["title"]).lower() in listed[key] for key in ("restore", "merge", "tag")]]
                     for r in rows],
        # totals cover what this caller may see: a worker's view must not count another problem
        "totals": {"restore": len(restore), "restore_gap": round(sum(row[2] for row in restore), 2),
                   "merge": len(merge), "tag": len(tag)},
    }
    return _fit(out, CLOSE_BYTES, ("closing", "merge", "tag"), rows="problems")


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
