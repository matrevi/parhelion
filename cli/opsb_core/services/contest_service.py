"""Contest metadata, slim board-summary cache, and 50-case operator telemetry."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import CONFIG_DIR, DEFAULT_DIFFICULTIES, parse_iso, read_json, read_jsonl, utc_now
from .git_fast import read_head_fast


def resolve_workspace_meta(workspace: Path) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    clean_manifest = read_json(workspace / "opsb.workspace.json")
    if isinstance(clean_manifest, dict):
        cid = clean_manifest.get("contest_id", "")
        owner = clean_manifest.get("git_owner", "winbeau")
        return {
            "workspace": str(workspace),
            "layout": clean_manifest.get("layout", "clean-agents-first"),
            "contest_id": cid,
            "title": clean_manifest.get("title", workspace.name),
            "team_name": "",
            "git_owner": owner,
            "repo_prefix": clean_manifest.get("repo_prefix", ""),
            "npu_ssh_host": clean_manifest.get("npu_ssh_host", "npu"),
            "contest_url": f"https://judge.invalid/contests/{cid}" if cid else "https://judge.invalid",
            "gitcode_url": f"https://gitcode.com/{owner}",
            "problems": clean_manifest.get("problems", []),
        }

    contest_detail = read_json(workspace / "results" / "scoring-evidence" / "contest-detail.json", {})
    problems_all = read_json(workspace / "results" / "scoring-evidence" / "problems-all.json", {})
    legacy_problems = read_json(workspace / "bench" / "problems.json", [])

    normalized = []
    for p in legacy_problems:
        title = p["title"]
        slug = p.get("slug", title.lower())
        p_meta = problems_all.get(title, {}) if isinstance(problems_all, dict) else {}
        diff = int(p_meta.get("difficulty") or DEFAULT_DIFFICULTIES.get(title, 2))
        normalized.append(
            {
                "title": title,
                "slug": slug,
                "repo_slug": f"bench-{slug}",
                "problem_id": p.get("problem_id", ""),
                "numeric_id": p.get("ID"),
                "difficulty": diff,
                "sdk_version": p.get("sdk_version", "9.1.0"),
                "path": p.get("path", f"bench/{title}"),
            }
        )

    cid = contest_detail.get("id") or contest_detail.get("_id") or "6a9e2d04bf41025d60f21804"
    return {
        "workspace": str(workspace),
        "layout": "legacy-multi-repo" if legacy_problems else "uninitialized",
        "contest_id": cid,
        "title": contest_detail.get("title") or "第十一届operator-challengeICT大赛｜kernel-trackAI算子开发赛道（决赛）",
        "team_name": (contest_detail.get("signup_status") or {}).get("team_name") or "",
        "git_owner": "winbeau",
        "repo_prefix": "bench",
        "npu_ssh_host": "npu",
        "contest_url": f"https://judge.invalid/contests/{cid}",
        "gitcode_url": "https://gitcode.com/winbeau",
        "problems": normalized,
    }


def load_slim_boards(board_path: Path) -> dict[str, Any]:
    """Cache only top-1 row + 50 testcase tbest entries keyed by file mtime (<0.5ms hit)."""
    if not board_path.is_file():
        return {}
    cache_path = CONFIG_DIR / ".board_slim_cache.json"
    try:
        mtime = board_path.stat().st_mtime_ns
        if cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("mtime") == mtime and isinstance(cached.get("boards"), dict):
                return cached["boards"]
    except Exception:
        mtime = 0

    raw = read_json(board_path, {})
    boards_in = raw.get("boards", {}) if isinstance(raw, dict) else {}
    slim: dict[str, Any] = {}
    for title, b in boards_in.items():
        rows = b.get("rows") or []
        slim[title] = {
            "meta": b.get("meta") or {"total": len(rows)},
            "testcases": b.get("testcases") or [],
            "rows": [rows[0]] if rows else [],
        }
    try:
        CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"mtime": mtime, "boards": slim}, separators=(",", ":")), encoding="utf-8")
    except Exception:
        pass
    return slim


def index_submission_history(workspace: Path) -> dict[str, Any]:
    """Aggregate problem timelines, best SHAs, 24h usage, and 12h circuit-breaker state."""
    results_dir = workspace / "results"
    score_table = read_json(results_dir / "scoring-evidence" / "my-score-table.json", [])
    outcome_rows = read_jsonl(results_dir / "submit-queue-outcome.jsonl")

    now = utc_now()
    cutoff_24h = now.timestamp() - 86400
    cutoff_12h = now.timestamp() - 43200

    history_by_prob: dict[str, list[dict[str, Any]]] = {}
    sha_pass_scores: dict[tuple[str, str], list[float]] = {}
    seen_keys: set[tuple[str, str]] = set()

    for row in score_table:
        t, sha, at = row.get("title", ""), row.get("sha", ""), row.get("at", "")
        sc = float(row.get("score") or 0.0)
        st = row.get("status", "Pass")
        seen_keys.add((t, f"{sha[:7]}|{at[:16]}"))
        history_by_prob.setdefault(t, []).append(
            {
                "at": at,
                "sha": sha,
                "status": st,
                "score": sc,
                "public_pts": round(float(row.get("public_pts") or 0.0), 2),
                "hidden_pts": round(float(row.get("hidden_pts") or 0.0), 2),
                "note": "",
                "submission_id": None,
            }
        )
        if st == "Pass" and sc > 0:
            sha_pass_scores.setdefault((t, sha), []).append(sc)

    last_sub_dt = None
    used_24h = 0
    pressure_12h: dict[str, int] = {}
    consec_fail_12h: dict[str, int] = {}
    best_12h: dict[str, float] = {}

    chron = sorted(outcome_rows, key=lambda r: r.get("submitted_at") or r.get("finished_at") or r.get("queued_at") or "")
    for out in chron:
        t, sha, st = out.get("title", ""), out.get("sha", ""), out.get("status", "")
        sc = out.get("score")
        sub_at = out.get("submitted_at") or out.get("finished_at")
        dt = parse_iso(sub_at)

        if st not in ("rejected", "skipped", "duplicate") and out.get("submission_id") and dt:
            ts = dt.timestamp()
            if last_sub_dt is None or dt > last_sub_dt:
                last_sub_dt = dt
            if ts >= cutoff_24h:
                used_24h += 1
            if ts >= cutoff_12h:
                val = float(sc or 0.0)
                if st == "Pass" and val > best_12h.get(t, -1.0):
                    best_12h[t] = val
                    pressure_12h[t] = 0
                    consec_fail_12h[t] = 0
                else:
                    pressure_12h[t] = pressure_12h.get(t, 0) + 1
                    consec_fail_12h[t] = 0 if st == "Pass" else consec_fail_12h.get(t, 0) + 1

        if st == "Pass" and sc is not None and float(sc) > 0:
            sha_pass_scores.setdefault((t, sha), []).append(float(sc))

        dkey = (t, f"{sha[:7]}|{(sub_at or '')[:16]}")
        if dkey not in seen_keys and st != "skipped":
            seen_keys.add(dkey)
            history_by_prob.setdefault(t, []).append(
                {
                    "at": sub_at or out.get("queued_at", ""),
                    "sha": sha,
                    "status": st,
                    "score": float(sc) if sc is not None else None,
                    "public_pts": None,
                    "hidden_pts": None,
                    "note": out.get("note", ""),
                    "submission_id": out.get("submission_id"),
                }
            )
        else:
            for h in history_by_prob.get(t, []):
                if h["sha"].startswith(sha[:7]) and h["at"][:16] == (sub_at or "")[:16]:
                    h["note"] = h["note"] or out.get("note", "")
                    h["submission_id"] = h["submission_id"] or out.get("submission_id")

    best_by_prob: dict[str, tuple[float, str]] = {}
    for t, h_list in history_by_prob.items():
        h_list.sort(key=lambda x: x.get("at") or "")
        prev_pass = 0.0
        for h in h_list:
            sc = h.get("score")
            if sc is not None:
                h["delta"] = round(float(sc) - prev_pass, 2)
                if h.get("status") == "Pass" and float(sc) > 0:
                    prev_pass = float(sc)
                    if float(sc) >= best_by_prob.get(t, (0.0, ""))[0]:
                        best_by_prob[t] = (float(sc), h["sha"])
            else:
                h["delta"] = None

    return {
        "outcome_rows": outcome_rows,
        "history_by_prob": history_by_prob,
        "sha_pass_scores": sha_pass_scores,
        "best_by_prob": best_by_prob,
        "last_submitted_dt": last_sub_dt,
        "used_24h": used_24h,
        "pressure_12h": pressure_12h,
        "consec_fail_12h": consec_fail_12h,
    }


def build_problem_models(
    workspace: Path,
    meta: dict[str, Any],
    hist_idx: dict[str, Any],
    noise_floor: float,
    cb_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    results_dir = workspace / "results"
    snapshot = read_json(results_dir / "score-snapshot.json", {})
    boards = load_slim_boards(results_dir / "scoring-evidence" / "board-summary.json")

    problems_out: list[dict[str, Any]] = []
    raw_total = 0.0
    weighted_total = 0.0
    max_weighted = 0
    passing_count = 0

    for p in meta["problems"]:
        title = p["title"]
        diff = int(p.get("difficulty", DEFAULT_DIFFICULTIES.get(title, 2)))
        max_weighted += diff * 100

        snap_row = snapshot.get(title, {})
        cur_score = float(snap_row.get("score") or 0.0)
        status = snap_row.get("status") or "no-submission"
        rank = snap_row.get("rank")
        ours_pub = [float(x) for x in (snap_row.get("public_times_us") or [])]

        if status == "Pass" and cur_score > 0:
            passing_count += 1
        raw_total += cur_score
        weighted_total += diff * cur_score

        hist_best, hist_best_sha = hist_idx["best_by_prob"].get(title, (0.0, ""))
        best_score = max(cur_score, hist_best)

        b_info = boards.get(title, {})
        tcs_raw = b_info.get("testcases") or []
        b_rows = b_info.get("rows") or []
        total_teams = (b_info.get("meta") or {}).get("total") or len(b_rows)
        ldr = b_rows[0] if b_rows else {}
        leader_score = float(ldr.get("score") or 0.0)
        leader_nick = ldr.get("nickname") or ""
        leader_times = [float(x) for x in (ldr.get("times") or []) if x is not None]

        pub_tbest, pub_ratios = [], []
        for idx in range(min(5, len(tcs_raw))):
            tb = tcs_raw[idx].get("tbest")
            if tb is not None:
                pub_tbest.append(float(tb))
                if idx < len(ours_pub) and ours_pub[idx] > 0:
                    pub_ratios.append(min(1.0, float(tb) / ours_pub[idx]))

        public_pts = round(2.0 * sum(pub_ratios), 2) if pub_ratios else 0.0
        hidden_pts = round(max(0.0, cur_score - public_pts), 2) if cur_score > 0 else 0.0
        implied_r = round(hidden_pts / 90.0, 4) if cur_score > 0 else 0.0

        all_cases = []
        for i in range(max(50, len(tcs_raw)) if tcs_raw else 50):
            tc = tcs_raw[i] if i < len(tcs_raw) else {}
            is_hid = bool(tc.get("hidden")) if tc else (i >= 5)
            tb = float(tc["tbest"]) if tc.get("tbest") is not None else None
            ot = ours_pub[i] if i < len(ours_pub) else None
            lt = leader_times[i] if i < len(leader_times) else None
            r = round(min(1.0, tb / ot), 4) if (tb is not None and ot and ot > 0) else None
            all_cases.append(
                {
                    "index": i + 1,
                    "case_id": tc.get("id") or f"case-{i+1:02d}",
                    "numeric_id": tc.get("ID"),
                    "is_hidden": is_hid,
                    "tbest_us": round(tb, 4) if tb is not None else None,
                    "ours_us": round(ot, 4) if ot is not None else None,
                    "leader_us": round(lt, 4) if lt is not None else None,
                    "ratio": r,
                    "points": round(2.0 * r, 3) if r is not None else None,
                }
            )

        restore_gap = max(0.0, best_score - cur_score)
        leader_gap = max(0.0, (leader_score if leader_score > 0 else 70.0) - cur_score)
        exp_step = restore_gap if (cur_score == 0 and best_score > 0) else max(restore_gap, min(12.0, leader_gap * 0.25))
        wspt = round(diff * exp_step, 2)

        chev = "^" * min(3, max(1, diff))
        if cur_score == 0 and best_score > 0:
            tag = "CRITICAL RESTORE"
        elif cur_score == 0:
            tag = f"FIRST PASS {chev}"
        elif restore_gap >= noise_floor:
            tag = f"RESTORE +{restore_gap:.1f}"
        elif diff == 3 and leader_gap >= 25:
            tag = "HIGH ROI ^^^"
        elif leader_gap >= 25:
            tag = f"HEADROOM {chev}"
        else:
            tag = f"STEADY {chev}"

        head_sha, head_age_min, head_subject = read_head_fast(workspace / p["path"])
        cb_trips = hist_idx["pressure_12h"].get(title, 0)
        cb_open = cb_trips >= cb_limit or hist_idx["consec_fail_12h"].get(title, 0) >= 2

        problems_out.append(
            {
                "title": title,
                "slug": p["slug"],
                "repo_slug": p["repo_slug"],
                "problem_id": p["problem_id"],
                "numeric_id": p.get("numeric_id"),
                "difficulty": diff,
                "sdk_version": p.get("sdk_version", "9.1.0"),
                "path": p["path"],
                "status": status,
                "rank": rank,
                "total_teams": total_teams,
                "current_score": round(cur_score, 2),
                "weighted_score": round(diff * cur_score, 2),
                "best_score": round(best_score, 2),
                "best_sha": hist_best_sha,
                "leader_score": round(leader_score, 2),
                "leader_nickname": leader_nick,
                "public_mean_us": round(sum(ours_pub) / len(ours_pub), 2) if ours_pub else None,
                "tbest_mean_us": round(sum(pub_tbest) / len(pub_tbest), 2) if pub_tbest else None,
                "public_pts": public_pts,
                "hidden_pts": hidden_pts,
                "implied_hidden_ratio": implied_r,
                "public_times_us": [round(x, 3) for x in ours_pub],
                "public_tbest_us": [round(x, 3) for x in pub_tbest],
                "leader_times_us": [round(x, 3) for x in leader_times[:5]],
                "public_ratios": [round(x, 4) for x in pub_ratios],
                "wspt_priority": wspt,
                "wspt_tag": tag,
                "circuit_open": cb_open,
                "pressure_12h": cb_trips,
                "head_sha": head_sha,
                "head_subject": head_subject,
                "head_age_min": head_age_min,
                "testcases": all_cases,
                "history": list(reversed(hist_idx["history_by_prob"].get(title, []))),
            }
        )

    closing_pot = sum(p["difficulty"] * p["best_score"] for p in problems_out)
    contest_summary = {
        **meta,
        "problem_count": len(problems_out),
        "passing_count": passing_count,
        "raw_score": round(raw_total, 2),
        "max_raw_score": len(problems_out) * 100,
        "weighted_score": round(weighted_total, 2),
        "max_weighted_score": max_weighted,
        "closing_potential_weighted": round(closing_pot, 2),
    }
    return problems_out, contest_summary
