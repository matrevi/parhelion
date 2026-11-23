"""Focused 9-tool MCP registry for Parhelion (`parhelion_*`) with lazy Track 1/2 imports."""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .. import config as _cfg
from ..drivers.base import load_parhelion_config, resolve_parhelion_home
from ..services.breakthrough_service import load_catalog
from ..services.contest_service import resolve_workspace_meta
from ..services.git_fast import resolve_commit_sha
from ..services.queue_service import check_commit_safety_gate, compute_workspace_snapshot, estimate_score_from_note

SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "entry"


def _find_problem(problems: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    for p in problems:
        if str(p.get("title", "")).lower() == q:
            return p
    return None


def _latest_result_dir(workspace: Path, title: str, sha: str | None = None) -> Path | None:
    res_root = workspace / "results" / title
    if not res_root.is_dir():
        return None
    if sha:
        for cand in res_root.iterdir():
            if cand.is_dir() and cand.name.startswith(sha[:7]):
                return cand
        return None
    dirs = [d for d in res_root.iterdir() if d.is_dir() and (d / "result.json").is_file()]
    if not dirs:
        return None
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return dirs[0]


def _validate_evidence(
    *,
    problem: str,
    sha: str,
    verdict: str,
    delta_raw: float | None = None,
    delta_us: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    if not str(problem or "").strip():
        raise ValueError("Evidence gate rejected write: non-empty 'problem' is required.")
    sha_s = str(sha or "").strip()
    if not SHA_RE.match(sha_s):
        raise ValueError("Evidence gate rejected write: 'sha' must be 7-40 hex characters.")
    v = str(verdict or "").strip().lower()
    if v not in ("win", "fail", "anti-pattern"):
        raise ValueError("Evidence gate rejected write: 'verdict' must be 'win', 'fail', or 'anti-pattern'.")
    note_s = str(note or "").strip()
    note_has_metric = ("profiler" in note_s.lower()) or ("online" in note_s.lower())
    if delta_raw is None and delta_us is None and not note_has_metric:
        raise ValueError(
            "Evidence gate rejected write: provide at least one measurable metric "
            "('delta_raw', 'delta_us', or 'note' citing 'profiler' / 'online')."
        )
    return {
        "problem": str(problem).strip(),
        "sha": sha_s,
        "verdict": v,
        "delta_raw": float(delta_raw) if delta_raw is not None else None,
        "delta_us": float(delta_us) if delta_us is not None else None,
        "note": note_s,
        "recorded_at": _cfg.utc_now().isoformat(),
    }


def _bayesian_confidence(wins: int, fails: int, *, prior_alpha: float = 1.0, prior_beta: float = 1.0) -> float:
    return round((prior_alpha + wins) / (prior_alpha + prior_beta + wins + fails), 3)


def _fallback_read_index(pillar: str) -> tuple[Path, dict[str, Any]]:
    base = resolve_parhelion_home() / pillar
    idx_path = base / "index.json"
    raw = _cfg.read_json(idx_path, {"schema_version": 1, "entries": []})
    if not isinstance(raw, dict):
        raw = {"schema_version": 1, "entries": []}
    if not isinstance(raw.get("entries"), list):
        raw["entries"] = []
    return idx_path, raw


def _fallback_write_index(idx_path: Path, payload: dict[str, Any]) -> None:
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = idx_path.with_suffix(".lock")
    with lock_path.open("a", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            tmp = idx_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, idx_path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _append_kb_audit(event: dict[str, Any]) -> None:
    ph = resolve_parhelion_home()
    _cfg.append_jsonl_locked(ph / ".kb-evolution.lock", ph / "kb-evolution.jsonl", event)


# ---------------------------------------------------------------------------
# 9 Focused MCP Tool Handlers
# ---------------------------------------------------------------------------


def tool_parhelion_status(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    cfg = load_parhelion_config()
    snap = compute_workspace_snapshot(workspace, cfg)
    prob_filter = str(args.get("problem") or "").strip().lower()
    include_npu = bool(args.get("include_npu", False))
    include_queue = bool(args.get("include_queue", True))

    problems = snap.get("problems") or []
    if prob_filter:
        problems = [p for p in problems if str(p.get("title", "")).lower() == prob_filter]

    compact_problems = [
        {
            "title": p.get("title"),
            "difficulty": p.get("difficulty"),
            "status": p.get("status"),
            "current_score": p.get("current_score"),
            "best_score": p.get("best_score"),
            "leader_score": p.get("leader_score"),
            "rank": p.get("rank"),
            "wspt_tag": p.get("wspt_tag"),
            "head_sha": str(p.get("head_sha") or "")[:12],
            "best_sha": str(p.get("best_sha") or "")[:12],
            "escape_lock": bool((p.get("calibration") or {}).get("escape_lock")),
        }
        for p in problems
    ]

    npu_state: dict[str, Any] = {"online": False, "skipped": True}
    if include_npu:
        try:
            from ..services.npu_service import poll_npu_telemetry

            ssh_host = cfg.get("npu_ssh_host") or snap["contest"].get("npu_ssh_host") or "npu"
            probs_map = {p["title"]: p for p in (snap.get("problems") or [])}
            npu_state = poll_npu_telemetry(ssh_host, probs_map)
        except Exception as exc:
            npu_state = {"online": False, "error": str(exc)}

    sched = snap.get("scheduler") or {}
    out: dict[str, Any] = {
        "sampled_at": snap.get("sampled_at"),
        "contest": {
            "title": snap["contest"].get("title"),
            "workspace": snap["contest"].get("workspace"),
            "weighted_score": snap["contest"].get("weighted_score"),
            "max_weighted_score": snap["contest"].get("max_weighted_score"),
            "raw_score": snap["contest"].get("raw_score"),
            "passing_count": snap["contest"].get("passing_count"),
            "problem_count": snap["contest"].get("problem_count"),
        },
        "scheduler": {
            "rate_ready": sched.get("rate_ready"),
            "cooldown_remaining_sec": sched.get("cooldown_remaining_sec"),
            "used_today_24h": sched.get("used_today_24h"),
            "daily_quota_limit": sched.get("daily_quota_limit"),
            "closing_reserve": sched.get("closing_reserve"),
            "available_normal_shots": sched.get("available_normal_shots"),
            "noise_floor_delta": sched.get("noise_floor_delta"),
            "actionable_count": sched.get("actionable_count"),
        },
        "npu": npu_state,
        "problems": compact_problems,
    }
    if include_queue:
        q_items = snap.get("submission_queue") or []
        if prob_filter:
            q_items = [q for q in q_items if str(q.get("title", "")).lower() == prob_filter]
        out["queue_preview"] = q_items[:12]
    return out


def tool_parhelion_bench_diagnose(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    cfg = load_parhelion_config()
    snap = compute_workspace_snapshot(workspace, cfg)
    prob_q = str(args.get("problem") or "").strip()

    targets = snap.get("problems") or []
    if prob_q:
        matched = _find_problem(targets, prob_q)
        if matched is None:
            raise ValueError(f"Unknown problem '{prob_q}' in workspace {workspace}")
        targets = [matched]

    diag_items: list[dict[str, Any]] = []
    for p in targets:
        title = str(p["title"])
        cal = dict(p.get("calibration") or {})
        strata = sorted(cal.get("strata") or [], key=lambda s: float(s.get("headroom_pts") or 0.0), reverse=True)
        diag_items.append(
            {
                "title": title,
                "current_score": p.get("current_score"),
                "best_score": p.get("best_score"),
                "mae_pts": cal.get("mae_pts"),
                "loocv_mae_pts": cal.get("loocv_mae_pts"),
                "loocv_spearman": cal.get("loocv_spearman"),
                "calibration_trust": cal.get("calibration_trust"),
                "last_residual_pts": cal.get("last_residual_pts"),
                "escape_lock": bool(cal.get("escape_lock")),
                "escape_sha": cal.get("escape_sha"),
                "top_headroom_strata": strata[:3],
                "strong_positive_buckets": cal.get("strong_positive_buckets") or [],
                "misleading_anti_corr_buckets": cal.get("misleading_anti_corr_buckets") or [],
                "high_residual_events": (cal.get("high_residual_events") or [])[-5:],
                "coverage_grid": cal.get("coverage_grid"),
            }
        )
    if prob_q and diag_items:
        return diag_items[0]
    return {"workspace": str(workspace), "problem_count": len(diag_items), "problems": diag_items}


def tool_parhelion_pmu_card(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_pmu_card")
    sha = args.get("sha")
    top_n = int(args.get("top_n") or 5)

    try:
        from ..services import fast_feedback_service  # type: ignore

        fn = getattr(fast_feedback_service, "build_pmu_torch_card", None)
        if callable(fn):
            res = fn(workspace, problem, sha=sha, top_n=top_n)
            if isinstance(res, dict):
                return {"available": True, **res}
    except ImportError:
        pass

    r_dir = _latest_result_dir(workspace, problem, sha=sha)
    local_res = _cfg.read_json(r_dir / "result.json", {}) if r_dir else {}
    return {
        "available": False,
        "title": problem,
        "sha": sha or (r_dir.name if r_dir else None),
        "dominant_bottleneck": "unknown",
        "reason": "fast_feedback_service not installed (Track 1 pending merge)",
        "local_result": local_res,
    }


def tool_parhelion_elf_cold3_check(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_elf_cold3_check")
    sha = args.get("sha")

    try:
        from ..services import fast_feedback_service  # type: ignore

        elf_fn = getattr(fast_feedback_service, "evaluate_elf_guard", None)
        cold_fn = getattr(fast_feedback_service, "evaluate_cold3_probe", None)
        if callable(elf_fn) and callable(cold_fn):
            elf = elf_fn(workspace, problem, sha=sha)
            cold = cold_fn(workspace, problem, sha=sha)
            rank = {"ok": 0, "warn": 1, "redline": 2}
            s_elf = str((elf or {}).get("status") or "ok")
            s_cold = str((cold or {}).get("status") or "ok")
            worst = s_elf if rank.get(s_elf, 0) >= rank.get(s_cold, 0) else s_cold
            return {
                "available": True,
                "title": problem,
                "sha": sha or (elf or {}).get("sha") or (cold or {}).get("sha"),
                "status": worst,
                "elf_guard": elf,
                "cold3_probe": cold,
            }
    except ImportError:
        pass

    r_dir = _latest_result_dir(workspace, problem, sha=sha)
    local_res = _cfg.read_json(r_dir / "result.json", {}) if r_dir else {}
    return {
        "available": False,
        "title": problem,
        "sha": sha or (r_dir.name if r_dir else None),
        "status": "unknown",
        "reason": "fast_feedback_service not installed (Track 1 pending merge)",
        "local_result": local_res,
    }


def tool_parhelion_kb_query(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    bottlenecks = [str(x) for x in (args.get("bottlenecks") or []) if str(x).strip()]
    regime = args.get("regime")
    dtype = args.get("dtype")
    pillar = args.get("pillar")
    text = str(args.get("text") or "")
    include_anti = bool(args.get("include_anti_patterns", True))
    limit = int(args.get("limit") or 8)
    want_digest = bool(args.get("digest", False))

    try:
        from ..services import triad_kb_service  # type: ignore

        ensure_fn = getattr(triad_kb_service, "ensure_triad_kb_seeded", None)
        if callable(ensure_fn):
            try:
                ensure_fn(parhelion_home=resolve_parhelion_home())
            except TypeError:
                ensure_fn()
        q_fn = getattr(triad_kb_service, "query_kb", None)
        if callable(q_fn):
            res = q_fn(
                bottlenecks=bottlenecks or None,
                regime=regime,
                dtype=dtype,
                pillar=None if pillar in (None, "all") else pillar,
                text=text,
                include_anti_patterns=include_anti,
                limit=limit,
            )
            if isinstance(res, dict):
                if want_digest:
                    fmt_fn = getattr(triad_kb_service, "format_prompt_cache_digest", None)
                    if callable(fmt_fn):
                        res["digest"] = fmt_fn(res)
                return res
    except ImportError:
        pass

    # Standalone fallback before Track 2 merge: query PARHELION_HOME/{snippets,community} + breakthrough/
    entries: list[dict[str, Any]] = []
    anti_patterns: list[dict[str, Any]] = []
    pillars_to_scan = ("snippets", "community") if pillar in (None, "all") else (pillar,) if pillar in ("snippets", "community") else ()
    for pil in pillars_to_scan:
        _, raw_idx = _fallback_read_index(pil)
        for item in raw_idx["entries"]:
            if not isinstance(item, dict):
                continue
            item_copy = {**item, "pillar": pil}
            if item_copy.get("status") == "anti-pattern" or item_copy.get("portability") == "anti-pattern":
                if include_anti:
                    anti_patterns.append(item_copy)
            else:
                entries.append(item_copy)

    if pillar in (None, "all", "breakthrough"):
        cat = load_catalog(workspace)
        for bt in cat.get("entries") or []:
            entries.append(
                {
                    "id": bt.get("id"),
                    "pillar": "breakthrough",
                    "title": bt.get("title"),
                    "summary": bt.get("transferable_rule") or bt.get("bottleneck"),
                    "bottlenecks": bt.get("tags") or [bt.get("category")],
                    "regimes": [],
                    "dtypes": [],
                    "portability": "native-accel",
                    "status": bt.get("status", "verified"),
                    "confidence": 0.9,
                    "doc_path": bt.get("abs_doc_path") or bt.get("doc_path"),
                }
            )

    def _score(ent: dict[str, Any]) -> float:
        blob = json.dumps(ent, ensure_ascii=False).lower()
        score = float(ent.get("confidence") or 0.5)
        if bottlenecks:
            ent_bn = {str(x).lower() for x in (ent.get("bottlenecks") or [])}
            overlap = sum(1 for b in bottlenecks if b.lower() in ent_bn or b.lower() in blob)
            score += overlap * 1.5
        if regime and str(regime).lower() in blob:
            score += 1.0
        if dtype and str(dtype).lower() in blob:
            score += 0.5
        if text and text.lower() in blob:
            score += 1.0
        return score

    entries.sort(key=_score, reverse=True)
    anti_patterns.sort(key=_score, reverse=True)
    out: dict[str, Any] = {
        "entries": entries[:limit],
        "anti_patterns": anti_patterns[:limit] if include_anti else [],
        "total_matched": len(entries),
        "source": "parhelion_fallback",
    }
    if want_digest:
        dig_lines = ["## Parhelion KB Digest"]
        for ap in out["anti_patterns"][:2]:
            dig_lines.append(f"- [ANTI-PATTERN] {ap.get('id')}: {ap.get('summary')}")
        for e in out["entries"][:4]:
            dig_lines.append(f"- [{e.get('pillar')}] {e.get('id')}: {e.get('summary')}")
        out["digest"] = "\n".join(dig_lines)
    return out


def tool_parhelion_kb_record(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    pillar = str(args.get("pillar") or "").strip().lower()
    if pillar not in ("snippets", "community"):
        raise ValueError("Invalid 'pillar': must be 'snippets' or 'community'.")
    title = str(args.get("title") or "").strip()
    summary = str(args.get("summary") or "").strip()
    if not title or not summary:
        raise ValueError("'title' and 'summary' must be non-empty.")

    evidence = _validate_evidence(
        problem=str(args.get("problem") or ""),
        sha=str(args.get("sha") or ""),
        verdict=str(args.get("verdict") or ""),
        delta_raw=args.get("delta_raw"),
        delta_us=args.get("delta_us"),
        note=str(args.get("note") or ""),
    )
    evidence["workspace"] = str(workspace)

    portability = str(args.get("portability") or ("native-accel" if pillar == "snippets" else "portable"))
    if evidence["verdict"] == "anti-pattern":
        portability = "anti-pattern"

    payload: dict[str, Any] = {
        "title": title,
        "summary": summary,
        "bottlenecks": [str(x) for x in (args.get("bottlenecks") or []) if str(x).strip()],
        "regimes": [str(x) for x in (args.get("regimes") or []) if str(x).strip()],
        "dtypes": [str(x) for x in (args.get("dtypes") or []) if str(x).strip()],
        "portability": portability,
        "code": args.get("code"),
        "doc_markdown": args.get("doc_markdown"),
        "evidence": evidence,
    }

    try:
        from ..services import triad_kb_service  # type: ignore

        ensure_fn = getattr(triad_kb_service, "ensure_triad_kb_seeded", None)
        if callable(ensure_fn):
            try:
                ensure_fn(parhelion_home=resolve_parhelion_home())
            except TypeError:
                ensure_fn()
        rec_fn = getattr(triad_kb_service, "record_kb_entry", None)
        if callable(rec_fn):
            return rec_fn(pillar, payload)
    except ImportError:
        pass

    # Standalone evidence-gated fallback store
    prefix = "SNP" if pillar == "snippets" else "COM"
    entry_id = f"{prefix}-{_slugify(title)}"
    ph = resolve_parhelion_home()
    idx_path, raw_idx = _fallback_read_index(pillar)

    wins = 1 if evidence["verdict"] == "win" else 0
    fails = 1 if evidence["verdict"] in ("fail", "anti-pattern") else 0
    if evidence["verdict"] == "anti-pattern" or portability == "anti-pattern":
        status = "anti-pattern"
    elif pillar == "snippets" and wins > 0:
        status = "verified"
    elif wins > 0 and ((evidence.get("delta_raw") or 0) >= 1.5):
        status = "verified"
    else:
        status = "unverified"

    if pillar == "snippets":
        doc_rel = f"code/{entry_id}.h"
        doc_abs = ph / "snippets" / doc_rel
        doc_abs.parent.mkdir(parents=True, exist_ok=True)
        doc_abs.write_text(str(args.get("code") or f"// {title}\n// {summary}\n"), encoding="utf-8")
    else:
        doc_rel = f"entries/{entry_id}.md"
        doc_abs = ph / "community" / doc_rel
        doc_abs.parent.mkdir(parents=True, exist_ok=True)
        doc_abs.write_text(
            str(args.get("doc_markdown") or f"# {title}\n\n{summary}\n"),
            encoding="utf-8",
        )

    entry = {
        "id": entry_id,
        "pillar": pillar,
        "title": title,
        "summary": summary,
        "bottlenecks": payload["bottlenecks"],
        "regimes": payload["regimes"],
        "dtypes": payload["dtypes"],
        "portability": portability,
        "status": status,
        "confidence": _bayesian_confidence(wins, fails),
        "wins": wins,
        "fails": fails,
        "evidence": [evidence],
        "doc_path": doc_rel,
    }
    raw_idx["entries"] = [e for e in raw_idx["entries"] if e.get("id") != entry_id] + [entry]
    raw_idx["updated_at"] = _cfg.utc_now().isoformat()
    _fallback_write_index(idx_path, raw_idx)
    _append_kb_audit(
        {
            "ts": _cfg.utc_now().isoformat(),
            "action": "record",
            "entry_id": entry_id,
            "pillar": pillar,
            "evidence": evidence,
        }
    )
    return {"ok": True, "entry": entry}


def tool_parhelion_kb_feedback(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    entry_id = str(args.get("entry_id") or "").strip()
    if not entry_id:
        raise ValueError("'entry_id' is required for parhelion_kb_feedback")
    evidence = _validate_evidence(
        problem=str(args.get("problem") or ""),
        sha=str(args.get("sha") or ""),
        verdict=str(args.get("verdict") or ""),
        delta_raw=args.get("delta_raw"),
        delta_us=args.get("delta_us"),
        note=str(args.get("note") or ""),
    )
    evidence["workspace"] = str(workspace)
    promote_code = args.get("promote_snippet_code")

    try:
        from ..services import triad_kb_service  # type: ignore

        fb_fn = getattr(triad_kb_service, "record_kb_feedback", None)
        if callable(fb_fn):
            return fb_fn(
                entry_id,
                evidence["verdict"],
                evidence,
                promote_snippet_code=promote_code,
            )
    except ImportError:
        pass

    for pil in ("community", "snippets"):
        idx_path, raw_idx = _fallback_read_index(pil)
        for idx, ent in enumerate(raw_idx["entries"]):
            if not isinstance(ent, dict) or ent.get("id") != entry_id:
                continue
            wins = int(ent.get("wins") or 0)
            fails = int(ent.get("fails") or 0)
            if evidence["verdict"] == "win":
                wins += 1
            else:
                fails += 1
            ent["wins"] = wins
            ent["fails"] = fails
            ent.setdefault("evidence", []).append(evidence)
            ent["confidence"] = _bayesian_confidence(wins, fails)
            if evidence["verdict"] == "anti-pattern" or (fails >= 3 and wins == 0):
                ent["status"] = "anti-pattern"
            elif ent.get("status") == "unverified" and evidence["verdict"] == "win" and (evidence.get("delta_raw") or 0) >= 1.5:
                ent["status"] = "verified"
            raw_idx["entries"][idx] = ent
            raw_idx["updated_at"] = _cfg.utc_now().isoformat()
            _fallback_write_index(idx_path, raw_idx)
            _append_kb_audit(
                {
                    "ts": _cfg.utc_now().isoformat(),
                    "action": "feedback",
                    "entry_id": entry_id,
                    "pillar": pil,
                    "evidence": evidence,
                }
            )
            return {"ok": True, "entry": ent}
    raise ValueError(f"KB entry '{entry_id}' not found")


def tool_parhelion_test_plan(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_test_plan")
    mode = str(args.get("mode") or "full").strip().lower()
    if mode not in ("build-only", "fast", "full"):
        raise ValueError("Invalid 'mode': must be 'build-only', 'fast', or 'full'.")
    execute = bool(args.get("execute", False))

    meta = resolve_workspace_meta(workspace)
    prob = _find_problem(meta.get("problems") or [], problem)
    title = str(prob["title"]) if prob else problem
    repo_dir = (workspace / prob["path"]) if prob else (workspace / title)
    raw_sha = str(args.get("sha") or "HEAD")
    sha = resolve_commit_sha(repo_dir, raw_sha) or raw_sha

    lane_map = {
        "build-only": {"phase": "Phase A CPU Compile", "queue": "CPU", "estimated_die_lock_sec": 0, "flag": "--build-only"},
        "fast": {"phase": "Phase A+B+C Fast Lane", "queue": "Q0", "estimated_die_lock_sec": 10, "flag": "--fast"},
        "full": {"phase": "Phase A+B+C Full Adversarial + profiler", "queue": "Q1", "estimated_die_lock_sec": 18, "flag": None},
    }
    lane = lane_map[mode]
    opsb_cmd = [_cfg.resolve_python_exe(workspace), str(_cfg.CLI_DIR / "opsb"), "test", title, "--workspace", str(workspace)]
    if sha and sha != "HEAD":
        opsb_cmd.extend(["--sha", sha])
    if lane["flag"]:
        opsb_cmd.append(str(lane["flag"]))

    plan = {
        "ok": True,
        "title": title,
        "sha": sha,
        "mode": mode,
        "lane": lane,
        "executed": execute,
        "command": opsb_cmd,
    }
    if not execute:
        return plan

    proc = subprocess.run(opsb_cmd, cwd=str(workspace), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300.0)
    plan["exit_code"] = proc.returncode
    plan["stdout"] = proc.stdout[-4000:]
    plan["stderr"] = proc.stderr[-2000:]
    plan["ok"] = proc.returncode == 0
    return plan


def tool_parhelion_submit_gate(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_submit_gate")
    note = str(args.get("note") or "").strip()
    enqueue = bool(args.get("enqueue", False))
    force = bool(args.get("force", False))

    cfg = load_parhelion_config()
    snap = compute_workspace_snapshot(workspace, cfg)
    prob = _find_problem(snap.get("problems") or [], problem)
    if prob is None:
        raise ValueError(f"Unknown problem '{problem}' in workspace {workspace}")

    title = str(prob["title"])
    repo_dir = workspace / prob["path"]
    raw_sha = str(args.get("sha") or prob.get("head_sha") or "HEAD")
    sha = resolve_commit_sha(repo_dir, raw_sha) or raw_sha
    if not sha or sha == "HEAD":
        raise ValueError(f"Unable to resolve commit SHA '{raw_sha}' for {title}")

    cal = prob.get("calibration") or {}
    disk_cal = _cfg.read_json(workspace / "results" / title / "calibration.json", {})
    escape_locked = bool(cal.get("escape_lock") or (isinstance(disk_cal, dict) and disk_cal.get("escape_lock")))
    safety_passed = check_commit_safety_gate(workspace, title, sha) or (
        prob.get("best_sha") == sha and float(prob.get("best_score") or 0) > 0
    )
    safety_ok = safety_passed and not escape_locked

    est = estimate_score_from_note(note, float(prob.get("current_score") or 0.0), float(prob.get("best_score") or 0.0))
    if est is None:
        est = float(prob["best_score"]) if (prob.get("best_sha") == sha and float(prob.get("best_score") or 0) > 0) else round(float(prob.get("current_score") or 0.0) + 3.0, 2)

    cur_sc = float(prob.get("current_score") or 0.0)
    noise_floor = float(snap["scheduler"]["noise_floor_delta"])
    delta_exp = round(est - cur_sc, 2)
    is_restore_zero = cur_sc == 0.0 and est > 0.0
    noise_ok = is_restore_zero or cur_sc == 0.0 or delta_exp >= noise_floor
    circuit_ok = is_restore_zero or not bool(prob.get("circuit_open"))
    quota_ok = is_restore_zero or int(snap["scheduler"]["available_normal_shots"]) > 0
    all_pass = safety_ok and noise_ok and circuit_ok and quota_ok

    reasons: list[str] = []
    if escape_locked:
        reasons.append("ESCAPE_LOCK active: verify escape reproducer before submitting")
    elif not safety_passed:
        reasons.append("G1 Safety failed: commit lacks passing NPU extended validation")
    if not noise_ok:
        reasons.append(f"G2 Noise Floor: Δexpected={delta_exp:+.2f} < {noise_floor}")
    if not circuit_ok:
        reasons.append(f"G3 Circuit Open: 12h pressure={prob.get('pressure_12h')}")
    if not quota_ok:
        reasons.append("G4 Quota Reserve: daily normal shots exhausted")

    verdict: dict[str, Any] = {
        "title": title,
        "sha": sha,
        "current_score": cur_sc,
        "estimated_score": est,
        "delta_expected": delta_exp,
        "wspt_priority": round(int(prob.get("difficulty") or 2) * max(0.0, delta_exp), 2),
        "gates": {
            "safety_ok": safety_ok,
            "noise_ok": noise_ok,
            "circuit_ok": circuit_ok,
            "quota_ok": quota_ok,
            "all_pass": all_pass,
        },
        "reasons": reasons,
        "enqueued": False,
    }
    if not enqueue:
        verdict["dry_run"] = True
        return verdict
    if not all_pass and not force:
        verdict["reason"] = "; ".join(reasons)
        return verdict

    q_path = workspace / "results" / "submit-queue.jsonl"
    o_path = workspace / "results" / "submit-queue-outcome.jsonl"
    done_keys = {(o.get("title"), str(o.get("sha") or "")[:7], o.get("queued_at")) for o in _cfg.read_jsonl(o_path)}
    pending = [
        q
        for q in _cfg.read_jsonl(q_path)
        if q.get("title") == title
        and str(q.get("sha") or "").startswith(sha[:7])
        and (q.get("title"), str(q.get("sha") or "")[:7], q.get("queued_at")) not in done_keys
    ]
    if pending and not force:
        verdict["reason"] = f"Duplicate in flight: {title} {sha[:7]} already queued"
        return verdict

    record = {
        "title": title,
        "sha": sha,
        "note": note or "queued via parhelion-mcp",
        "queued_at": _cfg.utc_now().isoformat(),
        "status": "queued",
        "estimated_score": est,
    }
    if force:
        record["force"] = True
    _cfg.append_jsonl_locked(workspace / "results" / ".submit-queue.lock", q_path, record)
    _cfg.invalidate_ttl_cache("scheduler_bridge")
    verdict["enqueued"] = True
    verdict["queued_record"] = record
    return verdict


# ---------------------------------------------------------------------------
# Schema Registry
# ---------------------------------------------------------------------------

ToolHandler = Callable[[dict[str, Any], Path], dict[str, Any]]

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "parhelion_status",
        "description": "Live contest scores, active NPU telemetry (optional), quota & queue summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Optional problem title filter"},
                "include_npu": {"type": "boolean", "description": "Probe live NPU via SSH (default false)"},
                "include_queue": {"type": "boolean", "description": "Include queue preview (default true)"},
            },
        },
        "handler": tool_parhelion_status,
    },
    {
        "name": "parhelion_bench_diagnose",
        "description": "Diagnose +corr headroom strata, residual gaps, calibration trust & ESCAPE_LOCK state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Optional operator problem title"},
                "sha": {"type": "string", "description": "Optional commit SHA"},
            },
        },
        "handler": tool_parhelion_bench_diagnose,
    },
    {
        "name": "parhelion_pmu_card",
        "description": "Generate compact ours/torch ratio and PMU pipe bottleneck diagnostic card.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Operator problem title"},
                "sha": {"type": "string", "description": "Optional commit SHA"},
                "top_n": {"type": "integer", "description": "Top worst buckets to surface (default 5)"},
            },
            "required": ["problem"],
        },
        "handler": tool_parhelion_pmu_card,
    },
    {
        "name": "parhelion_elf_cold3_check",
        "description": "Check ELF .o/.text/template instantiation redlines and 3-run cold-start DVFS spikes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Operator problem title"},
                "sha": {"type": "string", "description": "Optional commit SHA"},
            },
            "required": ["problem"],
        },
        "handler": tool_parhelion_elf_cold3_check,
    },
    {
        "name": "parhelion_kb_query",
        "description": "Query Triad KB (breakthrough + snippets + community + anti-patterns) by symptom, regime, or dtype.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bottlenecks": {"type": "array", "items": {"type": "string"}},
                "regime": {"type": "string"},
                "dtype": {"type": "string"},
                "pillar": {"type": "string", "enum": ["breakthrough", "snippets", "community", "all"]},
                "text": {"type": "string"},
                "include_anti_patterns": {"type": "boolean"},
                "limit": {"type": "integer"},
                "digest": {"type": "boolean"},
            },
        },
        "handler": tool_parhelion_kb_query,
    },
    {
        "name": "parhelion_kb_record",
        "description": "Propose a new snippet, community card, or anti-pattern (strictly evidence-gated).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pillar": {"type": "string", "enum": ["snippets", "community"]},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "verdict": {"type": "string", "enum": ["win", "fail", "anti-pattern"]},
                "bottlenecks": {"type": "array", "items": {"type": "string"}},
                "regimes": {"type": "array", "items": {"type": "string"}},
                "dtypes": {"type": "array", "items": {"type": "string"}},
                "portability": {"type": "string", "enum": ["native-accel", "portable", "remap-to-accel", "anti-pattern"]},
                "code": {"type": "string"},
                "doc_markdown": {"type": "string"},
                "delta_raw": {"type": "number"},
                "delta_us": {"type": "number"},
                "note": {"type": "string"},
            },
            "required": ["pillar", "title", "summary", "problem", "sha", "verdict"],
        },
        "handler": tool_parhelion_kb_record,
    },
    {
        "name": "parhelion_kb_feedback",
        "description": "Record win/fail/anti-pattern evidence on an existing KB entry to drive Bayesian promotion/demotion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["win", "fail", "anti-pattern"]},
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "delta_raw": {"type": "number"},
                "delta_us": {"type": "number"},
                "note": {"type": "string"},
                "promote_snippet_code": {"type": "string"},
            },
            "required": ["entry_id", "verdict", "problem", "sha"],
        },
        "handler": tool_parhelion_kb_feedback,
    },
    {
        "name": "parhelion_test_plan",
        "description": "Preview 3-Phase WSPT NPU test plan (--build-only / --fast / full) or execute `opsb test`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "mode": {"type": "string", "enum": ["build-only", "fast", "full"]},
                "execute": {"type": "boolean"},
            },
            "required": ["problem"],
        },
        "handler": tool_parhelion_test_plan,
    },
    {
        "name": "parhelion_submit_gate",
        "description": "Evaluate 4-Stage Submission Gate (G1-G4 + ESCAPE_LOCK) and optionally enqueue validated commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "note": {"type": "string"},
                "enqueue": {"type": "boolean"},
                "force": {"type": "boolean"},
            },
            "required": ["problem"],
        },
        "handler": tool_parhelion_submit_gate,
    },
]

TOOL_HANDLERS: dict[str, ToolHandler] = {t["name"]: t["handler"] for t in TOOL_DEFINITIONS}


def list_tool_schemas() -> list[dict[str, Any]]:
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in TOOL_DEFINITIONS
    ]


def call_tool(name: str, arguments: dict[str, Any] | None, workspace: Path) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise KeyError(f"Unknown tool: {name}")
    if arguments is not None and not isinstance(arguments, dict):
        raise ValueError("Tool 'arguments' must be a JSON object")
    return handler(arguments or {}, workspace.expanduser().resolve())
