"""The platform report views behind ``parhelion_report`` (4.2 D).

Every view reuses the service that owns its calculation (see each ``_view`` helper): the state
card, the P10 paired report or separate minima, the canonical receipt journal, the queue snapshot
with its gate lamps, the board caches, the PMU card plus KB query, and the local environment.
A worker reads its own problem only; ``board``/``env`` also serve workers. Hard budgets:
``per_case``/``diff`` 4KB, ``env`` 1KB; list views use ``limit``.
"""
from __future__ import annotations

import errno
import json
import math
import os
import shutil
from collections import Counter
from pathlib import Path

from .. import __version__ as OPSB_VERSION
from .. import config as _cfg

VIEWS = ("per_case", "diff", "history", "queue", "board", "bottleneck", "env")
BIG, ENV_BYTES, LIMIT_MAX = 4096, 1024, 50
PER_CASE_COLUMNS = ("case", "ours_us", "tbest_us", "gap_pct", "speedup", "band_pct", "verdict", "noisy", "judge", "mapped", "local", "timer", "passed", "exit_code", "mismatch_n", "strict_fp32", "cal", "wa", "run")
DIFF_COLUMNS = ("case", "ratio", "lo", "hi", "n", "method", "timer", "bootstrap", "verdict")
FORBIDDEN = ("account_id", "account_label", "user_id", "cookies", "cookie", "token", "session_file")

def _num(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None

def _round(value, digits=3):
    return round(_num(value), digits) if _num(value) is not None else None

def _clip(value, limit):
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

def _sha7(value):
    return str(value or "")[:7]

def _bytes(doc):
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

def _limit(args, default=10):
    try:
        return max(1, min(LIMIT_MAX, int(args.get("limit"))))
    except (TypeError, ValueError):
        return default

def _trim(doc, key, budget, floor=1):
    rows = doc.get(key) or []
    while len(rows) > floor and _bytes(doc) > budget:
        rows.pop()
        doc["truncated"] = True
    doc.update(shown=len(rows)) if doc.get("truncated") else None
    return doc

def _caller(args, workspace):
    from .handlers_ledger import problem_of

    worker, agent, asked = bool(args.get("_worker_request")), str(args.get("_caller_agent_id") or "").strip(), str(args.get("problem") or "").strip()
    if not worker:
        return "orchestrator", asked
    if not agent:
        raise PermissionError("A bound worker identity is required")
    mine = problem_of(Path(workspace), agent)
    if not mine or (asked and asked.lower() != mine.lower()):
        raise PermissionError(f"You are staffed on {mine or 'no problem'}; a worker reads only its own report")
    return "worker", mine

def _titles(workspace, problem, role):
    if problem:
        return [problem]
    if role == "worker":
        raise PermissionError("the problem of this call is unknown: pass problem or use a bound worker")
    from ..services.contest_service import resolve_workspace_meta

    return [str(p.get("title")) for p in resolve_workspace_meta(Path(workspace)).get("problems") or [] if p.get("title")]

def _resolve(workspace, title, value):
    sha = str(value or "").strip()
    root = Path(workspace) / "results" / title
    if len(sha) < 7 or not root.is_dir():
        return sha
    return next((c.name for c in sorted(root.iterdir()) if c.is_dir() and c.name.lower().startswith(sha.lower())), "") or sha

def _doc(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def _docs(workspace, title, sha=""):
    roots, patterns = [Path(workspace) / "results" / title, _cfg.workspace_data_dir(workspace)], [f"{sha or '*'}/bench-runs/*/report.json", "bench-runs/*/report.json", "bench-runs/*/*/report.json"]
    seen, docs = set(), []
    for root in roots:
        for pattern in patterns:
            for path in sorted(root.glob(pattern)) if root.is_dir() else []:
                doc, seen = (_doc(path) if path not in seen and path.is_file() else {}), seen | {path}
                for case in (doc.get("cases") or []) if isinstance(doc, dict) else []:
                    samples = [v for v in (case.get("device_us") or []) if _num(v)] if isinstance(case, dict) and case.get("min_us") is None else []
                    case.update(min_us=min(samples)) if isinstance(case, dict) and samples else None
                if doc and (not sha or str(doc.get("sha") or "").lower().startswith(sha.lower())):
                    docs.append(doc)
    return docs

def _best(docs):
    return max(docs, key=lambda d: (d.get("formal") is True, str(d.get("finished_at") or "")), default={})

def _cases_of(doc):
    return {str(c.get("id")): c for c in doc.get("cases") or [] if isinstance(c, dict) and c.get("id")}

def _local_metrics(workspace, title, sha, base):
    _cfg.ensure_scheduler_path()
    from scheduler import verdicts
    after, before = _best(_docs(workspace, title, sha)), _best(_docs(workspace, title, base))
    rows_before, out = _cases_of(before), {}
    for case_id, case in _cases_of(after).items():
        prior, status, bad = rows_before.get(case_id), str(case.get("status") or ""), case.get("mismatches")
        timer = str(case.get("timing_source") or after.get("timing_source") or "")
        out[case_id] = {"run": _sha7(after.get("run_id")), "timer": "profiler" if "profiler" in timer else "event" if timer else "", "exit_code": case.get("exit_code"),
                        "passed": True if status == "PASS" else False if status in ("WA", "RE", "TLE", "CE", "SKIP") else None,
                        "mismatch_n": len(bad) if isinstance(bad, list) else bad if type(bad) is int else None, "verdict": verdicts.paired(prior, case, before_source=before.get("timing_source"), after_source=after.get("timing_source")) if prior else None}
    return out, after

def _wa_flags(workspace, title, head, receipts):
    _cfg.ensure_scheduler_path()
    from scheduler import wa_repro
    try:
        planned = wa_repro.load_manifest(Path(workspace), title).get("receipts") or {}
    except Exception:
        planned = {}
    for row in reversed(receipts):
        sid = str(row.get("submission_id") or "")
        if sid not in planned:
            continue
        state = wa_repro.status(Path(workspace), title, sid, str(row.get("sha") or ""), cand_sha=head or None)
        judges = {str(b.get("judge")): str(state.get("state") or "") for b in planned[sid].get("bases") or [] if b.get("judge")}
        locals_ = {str(b.get("local")): str(state.get("state") or "") for b in planned[sid].get("bases") or [] if b.get("local")}
        return {"receipt": wa_repro.sid8(sid), "state": state.get("state"), "variants": len(state.get("variants") or []), "judges": judges, "locals": locals_}
    return {}

def _per_case(workspace, problem, args):
    from ..services import state_card_ext
    _cfg.ensure_scheduler_path()
    from scheduler import case_calibration, case_map
    ws, title = Path(workspace), str(problem)
    data = state_card_ext.data(ws, title, str(args.get("_caller_agent_id") or "") or None)
    head = _resolve(workspace, title, args.get("sha")) or str(data.get("head_sha") or "")
    base = _resolve(workspace, title, args.get("base")) or str(data.get("base_sha") or "")
    pair = state_card_ext.speedups(ws, title, head, base) if head and base and head != base else {}
    local_of = {str(p.get("judge")): str(p.get("local")) for p in (case_map.current(ws, title, write=False).get("pairs") or []) if p.get("judge")}
    try:  # the per-case calibration label lives in the stored records; an absent map is not fatal
        calibration = case_calibration.table(ws, title).get("cases") or {}
    except Exception:
        calibration = {}
    metrics, after = _local_metrics(ws, title, head, base)
    wa, strict, rows = _wa_flags(workspace, title, head, state_card_ext.receipts_rows(ws, title)), bool((after.get("settings") or {}).get("strict_fp32")), []
    for row in data.get("cases") or []:
        judge, local = str(row.get("judge") or ""), local_of.get(str(row.get("judge") or ""))
        case, verdict = metrics.get(local) or {}, (metrics.get(local) or {}).get("verdict") or {}
        rows.append([row.get("case"), row.get("ours_us"), row.get("tbest_us"), round(float(row["gap_pct"]) * 100, 2) if row.get("gap_pct") is not None else None,
                     _round((pair or {}).get("cases", {}).get(local), 4) if local else None, round(float(verdict["band"]) * 100, 2) if verdict.get("band") is not None else None,
                     verdict.get("verdict") or "unknown", verdict.get("noisy"), judge, bool(local), local, case.get("timer") or "",
                     case.get("passed"), case.get("exit_code"), case.get("mismatch_n"), strict,
                     (calibration.get(local) or {}).get("label") if local else None, (wa.get("judges") or {}).get(judge, "") or (wa.get("locals") or {}).get(local or "", ""), case.get("run")])
    out = {"view": "per_case", "problem": title, "sha": _sha7(head), "base": _sha7(base), "head": _sha7(data.get("head_sha")), "speedup_confidence": (pair or {}).get("confidence"), "wa_state": wa.get("state"), "wa_receipt": wa.get("receipt"), "strict_fp32": strict, "columns": PER_CASE_COLUMNS, "cases": rows}
    return _trim(out, "cases", BIG)

def _verdict_of(ci):
    return "improved" if (_num(ci.get("lo")) or 0.0) > 1.0 else "regressed" if (_num(ci.get("hi")) or 2.0) < 1.0 else "indistinguishable"

def _diff(workspace, problem, args):
    from ..ledger.gitref import git_out, problem_repo
    from ..services import state_card_ext
    _cfg.ensure_scheduler_path()
    from scheduler import paired_timing, timing
    ws, title = Path(workspace), str(problem)
    cand, base = _resolve(workspace, title, args.get("sha")) or state_card_ext.head_sha(ws, title), _resolve(workspace, title, args.get("base")) or state_card_ext.base_of(ws, title)
    if not cand or not base or cand == base:
        raise ValueError("diff needs two different SHAs: pass sha and base, or run a formal test first")
    paired, rows, source, geomean = paired_timing.load(ws, title, cand, base), [], "separate-runs", None
    if paired:
        source = "paired-report"
        summary = (paired.get("digest") or {}).get("paired")
        geomean = _round((summary or {}).get("geomean_ratio"), 4) if isinstance(summary, dict) else None
        for case in paired.get("cases") or []:
            if not isinstance(case, dict):
                continue
            ci, boot = case.get("pair") or {}, case.get("pair_bootstrap") or {}
            if isinstance(ci, dict):
                boot = boot if isinstance(boot, dict) else {}
                rows.append([case.get("id"), _round(ci.get("ratio"), 4), _round(ci.get("lo"), 4), _round(ci.get("hi"), 4), ci.get("n"), ci.get("method"), ci.get("source") or "event",
                             [_round(boot.get("lo"), 4), _round(boot.get("hi"), 4)] if boot.get("lo") is not None else None, _verdict_of(ci)])
    else:
        docs = {side: _docs(ws, title, sha) for side, sha in (("cand", cand), ("base", base))}
        minima = {side: {src: timing.best_minima(docs[side], src) for src in timing.SOURCES} for side in docs}
        chosen = timing.common_source(minima["cand"], minima["base"])
        if chosen:
            timer, cand_min, base_min = chosen
            after_map, before_map = _cases_of(_best(docs["cand"])), _cases_of(_best(docs["base"]))

            def samples(case, src):
                values = case.get(f"{src}_us") or (case.get("device_us") if src == "event" else None)
                return [float(v) for v in values or [] if _num(v) and float(v) > 0]
            for case_id in sorted(set(cand_min) & set(base_min)):
                ci = paired_timing.unpaired_interval(samples(after_map.get(case_id) or {}, timer), samples(before_map.get(case_id) or {}, timer)) or {}
                rows.append([case_id, _round(base_min[case_id] / cand_min[case_id], 4), _round(ci.get("lo"), 4), _round(ci.get("hi"), 4),
                             ci.get("n"), ci.get("method") or "minima", timer, None, _verdict_of(ci)])
    repo = problem_repo(ws, title)
    stat = [_clip(line, 160) for line in (git_out(repo, "diff", "--stat", base, cand) if repo else "").strip().splitlines() if line.strip()][:12]
    out = {"view": "diff", "problem": title, "sha": _sha7(cand), "base": _sha7(base), "source": source, "geomean": geomean, "git": {"stat": stat}, "columns": DIFF_COLUMNS, "cases": rows}
    out = _trim(out, "cases", BIG, floor=0)
    while _bytes(out) > BIG and out["git"]["stat"]:
        out["git"]["stat"].pop()
    return out

def _history(workspace, problem, args, role):
    _cfg.ensure_scheduler_path()
    from scheduler import receipts
    ws, limit, titles = Path(workspace), _limit(args), _titles(Path(workspace), problem, role)
    journal, problems, timeline = receipts.canonical(receipts.read_rows(receipts.ledger_path(ws))), [], []
    for title in titles:
        rows = [r for r in journal if str(r.get("title") or "") == title]
        judged = [r for r in rows if receipts.judged(r)]
        scores = [s for s in (receipts.official_score(r) for r in judged) if s is not None]
        latest = judged[-1] if judged else {}
        problems.append({"title": title, "judged": len(judged), "rows": len(rows), "best_judged": max(scores) if scores else None, "latest": {"sha": _sha7(latest.get("sha")), "status": latest.get("status"), "score": receipts.official_score(latest) if latest else None}})
        for row in (rows[-limit:] if problem else []):
            entry = {"sid": str(row.get("submission_id") or ""), "sha": _sha7(row.get("sha")), "status": row.get("status"), "score": receipts.official_score(row), "beat_board": receipts.beat_board(row), "at": str(row.get("submitted_at") or row.get("finished_at") or "")[:19], "note": _clip(row.get("note") or row.get("detail") or "", 80)}
            times, tbest = receipts.case_times(row), receipts.case_tbest(row)
            if times and tbest:
                ratios, cases = [], [[k, _round(times.get(k), 2), _round(tbest.get(k), 2)] for k in list(tbest)[:20]]
                for item in cases:
                    item.append(round(item[2] / item[1], 3) if _num(item[1]) and _num(item[2]) and item[1] > 0 and item[2] > 0 else None)
                    ratios += [item[3]] if item[3] is not None else []
                entry.update(cases=cases, mean_ratio=round(sum(ratios) / len(ratios), 3) if ratios else None)
            timeline.append(entry)
    out = {"view": "history", "problems": problems, "receipts": timeline,
           "columns": ["sid", "sha", "status", "score", "beat_board", "at", "note", "cases", "mean_ratio"],
           "case_columns": ["case", "ours_us", "tbest_us", "ratio"]}
    return _trim(out, "receipts", BIG, floor=0)

def _queue_item(item):
    keys, gates = ("evaluated", "safety_ok", "noise_ok", "circuit_ok", "dispatch_ok", "all_pass", "delta_expected", "ucb_delta", "calibration_trust", "explore_eligible", "wspt_score"), (item.get("gates") if isinstance(item.get("gates"), dict) else {})
    card = {"id": item.get("id"), "kind": item.get("kind"), "title": item.get("title"), "sha": _sha7(item.get("sha")), "badge": item.get("badge"), "raw_status": item.get("raw_status"),
            "current_score": item.get("current_score"), "estimated_score": item.get("estimated_score"), "actual_score": item.get("actual_score"), "delta_score": item.get("delta_score"),
            "breakthrough_delta": item.get("breakthrough_delta"), "queued_at": item.get("queued_at"), "finished_at": item.get("finished_at"), "submission_id": item.get("submission_id"), "phase": item.get("phase"), "elapsed_sec": item.get("elapsed_sec"),
            "note": _clip(item.get("note") or "", 120), "detail": _clip(item.get("detail") or "", 160), "next_step": _clip(item.get("next_step") or "", 160), "gates": {k: gates.get(k) for k in keys},
            "can_approve": item.get("can_approve"), "can_skip": item.get("can_skip"), "can_restore": item.get("can_restore"), "archive_rank": item.get("archive_rank"), "experience_id": item.get("experience_id")}
    return {k: v for k, v in card.items() if k not in FORBIDDEN}

def _queue(workspace, problem, args, role):
    from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
    from ..services.queue_service import compute_workspace_snapshot

    ws, limit, allowed = Path(workspace), _limit(args, 20), set(_titles(Path(workspace), problem, role))
    snap = compute_workspace_snapshot(ws, _cfg.ConfigRepository.load())
    try:
        bridge = invoke_bridge(ws, "status", ["--offline"], use_cache=True)
        merge_bridge_enrichment(snap, bridge) if isinstance(bridge, dict) else None
    except Exception:
        pass
    sched = snap.get("scheduler") or {}
    items = [it for it in snap.get("submission_queue") or [] if str(it.get("title") or "") in allowed]
    by_kind = dict(Counter(str(item.get("kind")) for item in items))
    out = {"view": "queue", "allowed": sorted(allowed), "threshold": sched.get("breakthrough_threshold"), "count": len(items), "by_kind": by_kind,
           "active_submissions": len((sched.get("pipeline") or {}).get("active_submissions") or []), "items": [_queue_item(it) for it in items[:limit]]}
    return _trim(out, "items", BIG)

def _env(workspace):
    ws = Path(workspace)
    state = _cfg.read_json(_cfg.DAEMON_STATE_PATH, {}) or {}
    pid = int(state.get("pid") or 0) if isinstance(state, dict) else 0
    try:
        os.kill(pid, 0) if pid > 0 else None
        running = pid > 0
    except OSError as exc:
        running = pid > 0 and exc.errno == errno.EPERM
    daemon, workspace = {"running": running, "pid": pid or None, "started_at": str(state.get("started_at") or "")[:19] if running else None}, {"root": str(ws), "results": str(ws / "results"), "state_dir": str(_cfg.workspace_state_dir(ws))}
    out = {"view": "env", "platform": {"version": OPSB_VERSION, "root": str(_cfg.SUPERVISOR_ROOT)}, "daemon": daemon, "workspace": workspace, "disk": {"free_gb": round(shutil.disk_usage(str(ws)).free / 1e9, 1) if ws.is_dir() else None}}
    return out if _bytes(out) <= ENV_BYTES else {k: v for k, v in out.items() if k != "disk"}

def _board(workspace, problem, args, role):
    from ..services import board_refresh_service, ranking_service
    from ..services.contest_service import resolve_workspace_meta
    ws, limit = Path(workspace), _limit(args, 30)
    problems = {str(p.get("title")): p for p in resolve_workspace_meta(ws).get("problems") or [] if p.get("title")}
    snapshot, live, _ = board_refresh_service.current_board(ws, problems)
    payload = _cfg.read_json(board_refresh_service.live_board_path(ws), {}) or {}
    observed = str(payload.get("fetched_at") or "")[:19] if isinstance(payload, dict) else ""
    ranking, rows = ranking_service.team_ranking(ws, allow_fetch=False) or {}, []
    for title in sorted(set(_titles(ws, problem, role)) & (set(problems) | set(live) | set(snapshot))):
        row, fallback = live.get(title) or {}, snapshot.get(title) or {}
        tbest = [v for v in row.get("tbest") or [] if _num(v) is not None]
        card = {"title": title, "listed": row.get("listed", fallback.get("listed")), "score": _round(row.get("our_score", fallback.get("score")), 2),
                "rank": row.get("our_rank", fallback.get("rank")), "total_teams": row.get("ranking_total"), "board_submission_id": row.get("board_submission_id", fallback.get("submission_id")),
                "frontier": {"top1": _round(row.get("frontier_top1"), 2), "top5": _round(row.get("frontier_top5"), 2)}, "observed_at": observed or None}
        card.update(tbest=[{"case": i, "tbest_us": _round(v, 3)} for i, v in enumerate(tbest[:limit], 1)]) if problem else None
        rows.append(card)
    team = {"rank": ranking.get("rank"), "score": _round(ranking.get("score"), 2), "total": ranking.get("total"),
            "state": ranking.get("ranking_state"), "fetched_at": str(ranking.get("fetched_at") or "")[:19] or None}
    out = {"view": "board", "observed_at": observed or None, "team": team, "problems": rows}
    while _bytes(out) > BIG and out["problems"] and out["problems"][0].get("tbest"):
        out["problems"][0]["tbest"].pop()
    return out

def _bottleneck(workspace, problem, args):
    from ..fast_feedback import pmu_torch_card
    ws, title = Path(workspace), str(problem)
    sha = _resolve(workspace, title, args.get("sha")) or None
    card = pmu_torch_card.build_pmu_torch_card(ws, title, sha=sha, top_n=3)
    pmu = card.get("pmu") or {}
    active = {name: _num(pmu.get(name)) for name in ("aiv_vec_ratio", "aiv_mte2_ratio", "aiv_mte3_ratio", "aiv_scalar_ratio", "bank_conflict_ratio")}
    active = {name: value for name, value in active.items() if value is not None}
    category = str(card.get("dominant_bottleneck") or "unknown")
    pipe = {"vec-bound": "aiv_vec_ratio", "mte2-bound": "aiv_mte2_ratio", "mte3-bound": "aiv_mte3_ratio", "scalar-bound": "aiv_scalar_ratio", "bank-conflict": "bank_conflict_ratio"}.get(category)
    limited = active.get(pipe) if pipe else None
    fixes = []
    if category not in ("unknown", "balanced"):
        try:
            from ..knowledge.query import query_kb

            fixes = [[e.get("id"), _clip(e.get("title"), 80), e.get("confidence"), _clip(e.get("summary"), 160)] for e in query_kb(bottlenecks=[category], limit=3, auto_seed=False).get("results") or []]
        except Exception:
            fixes = []
    run_dir = ws / "results" / title / (card.get("sha") or sha or "")
    runs = sorted(run_dir.glob("bench-runs/*/report.json"), key=lambda path: str(_doc(path).get("finished_at") or ""))
    runs = [path for path in runs if _doc(path).get("formal") is True] or runs
    log = run_dir / "validation.log"
    raw = {"report": str(runs[-1]) if runs else None, "log": str(log) if log.is_file() else None, "run_dir": str(run_dir) if run_dir.is_dir() else None}
    metrics = {name: round(value * 100, 1) for name, value in active.items()}
    top = [{"bucket": b.get("bucket"), "ours_us": b.get("ours_us"), "torch_us": b.get("torch_us"), "ratio_vs_torch": b.get("ratio_vs_torch"), "headroom": b.get("headroom_score")} for b in (card.get("top_buckets") or [])[:3]]
    return {"view": "bottleneck", "problem": title, "sha": _sha7(card.get("sha") or sha), "category": category, "limited_resource_pct": round(limited * 100, 1) if limited is not None else None,
            "pmu_available": bool(card.get("pmu_available")), "pmu": metrics, "top_metrics": top, "fix_columns": ["id", "title", "confidence", "summary"], "fixes": fixes, "raw": raw}

def tool_parhelion_report(args, workspace):
    """Read one platform report view; ``mcp.tools`` already fixed which roles may call it."""
    view = str(args.get("view") or "").strip().lower()
    if view not in VIEWS:
        raise ValueError("view must be one of " + ", ".join(VIEWS))
    ws = Path(workspace).expanduser().resolve()
    role, problem = _caller(args, ws)
    if view == "env":
        return _env(ws)
    if view in ("board", "history", "queue"):
        return {"board": _board, "history": _history, "queue": _queue}[view](ws, problem, args, role)
    if not problem:
        raise PermissionError("the problem of this call is unknown: pass problem or use a bound worker")
    return {"per_case": _per_case, "diff": _diff, "bottleneck": _bottleneck}[view](ws, problem, args)
