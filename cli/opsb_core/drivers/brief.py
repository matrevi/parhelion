"""Session worktree preparation and prompt-cache-safe `< 3 KB` brief composition."""
from __future__ import annotations

import importlib
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..services.breakthrough_service import load_catalog
from ..services.calibration_service import build_problem_calibration
from ..services.contest_service import index_submission_history, load_slim_boards, resolve_workspace_meta
from .base import resolve_parhelion_home

MAX_BRIEF_BYTES = 2950


def resolve_problem_repo(workspace: Path, problem: str) -> tuple[str, Path, dict[str, Any] | None]:
    ws = workspace.expanduser().resolve()
    meta = resolve_workspace_meta(ws)
    probs = meta.get("problems") or []
    matched = next((p for p in probs if str(p.get("title", "")).lower() == problem.lower()), None)
    if matched:
        title = str(matched["title"])
        rel = str(matched.get("path") or title)
        return title, (ws / rel).resolve(), matched
    for cand in (ws / problem, ws / "bench" / problem):
        if cand.is_dir():
            return cand.name, cand.resolve(), None
    return problem, (ws / problem).resolve(), None


def prepare_session_worktree(
    workspace: Path,
    problem: str,
    track_name: str = "",
    use_main: bool = False,
) -> Path:
    """Create or reuse `<problem_repo>/.worktrees/<track>` safely (no-op if `use_main=True`)."""
    ws = workspace.expanduser().resolve()
    title, repo_dir, _ = resolve_problem_repo(ws, problem)
    if use_main or not repo_dir.is_dir() or not (repo_dir / ".git").exists():
        return repo_dir

    head_chk = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", "HEAD"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5.0,
    )
    if head_chk.returncode != 0:
        return repo_dir

    raw_track = track_name.strip() if track_name else f"tune-{_cfg.utc_now().strftime('%Y%m%d-%H%M%S')}"
    safe_track = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_track).strip("-.") or "tune"
    wt_dir = repo_dir / ".worktrees" / safe_track
    if wt_dir.exists() and (wt_dir / ".git").exists():
        return wt_dir.resolve()

    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    branch_chk = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", f"refs/heads/{safe_track}"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5.0,
    )
    cmd = (
        ["git", "-C", str(repo_dir), "worktree", "add", str(wt_dir), safe_track]
        if branch_chk.returncode == 0
        else ["git", "-C", str(repo_dir), "worktree", "add", "-b", safe_track, str(wt_dir), "HEAD"]
    )
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15.0)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to prepare worktree for {title} at {wt_dir}: {(proc.stderr or proc.stdout).strip()}")
    return wt_dir.resolve()


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    suffix = "\n...(truncated)"
    budget = max(64, max_bytes - len(suffix.encode("utf-8")))
    return raw[:budget].decode("utf-8", errors="ignore").rstrip() + suffix


def _collect_fast_feedback(ws: Path, title: str) -> tuple[list[str], list[str]]:
    detected_tags: list[str] = []
    pmu_section: list[str] = []
    try:
        ff_mod = importlib.import_module("opsb_core.services.fast_feedback_service")
        if callable(pmu_fn := getattr(ff_mod, "build_pmu_torch_card", None)):
            pmu = pmu_fn(ws, title)
            if isinstance(pmu, dict):
                dom = pmu.get("dominant_bottleneck") or pmu.get("bottleneck")
                if dom and dom != "unknown":
                    detected_tags.append(str(dom))
                if md_card := str(pmu.get("markdown_card") or "").strip():
                    pmu_section.append(md_card)
        if callable(elf_fn := getattr(ff_mod, "evaluate_elf_guard", None)):
            elf = elf_fn(ws, title)
            if isinstance(elf, dict) and elf.get("status") in ("warn", "redline"):
                pmu_section.append(f"- ELF Guard `{elf['status']}`: {len(elf.get('alerts') or [])} alert(s)")
                detected_tags.append("icache-bloat")
        if callable(cold_fn := getattr(ff_mod, "evaluate_cold3_probe", None)):
            c3 = cold_fn(ws, title)
            if isinstance(c3, dict) and (c3.get("bimodal_spike_detected") or c3.get("status") in ("warn", "redline")):
                pmu_section.append(f"- Cold3 Probe `{c3.get('status')}`: cold_tax={c3.get('cold_tax_us')}us")
                detected_tags.append("cold-launch-tax")
    except Exception:
        pass
    return detected_tags, pmu_section


def _collect_kb_digest(ws: Path, title: str, detected_tags: list[str]) -> str:
    try:
        kb_mod = importlib.import_module("opsb_core.services.triad_kb_service")
        if callable(ensure_fn := getattr(kb_mod, "ensure_triad_kb_seeded", None)):
            try:
                ensure_fn(parhelion_home=resolve_parhelion_home())
            except TypeError:
                ensure_fn()
        q_fn = getattr(kb_mod, "query_kb", None)
        fmt_fn = getattr(kb_mod, "format_prompt_cache_digest", None)
        if callable(q_fn):
            qr = q_fn(bottlenecks=detected_tags or None, text=title, include_anti_patterns=True, limit=4)
            if isinstance(qr, dict):
                items = qr.get("results") or qr.get("entries") or []
                anti = qr.get("anti_patterns") or []
                if not items and not anti and detected_tags:
                    qr = q_fn(bottlenecks=detected_tags, include_anti_patterns=True, limit=4)
                if callable(fmt_fn):
                    return str(fmt_fn(qr, max_chars=900)).strip()
    except Exception:
        pass

    try:
        cat = load_catalog(ws)
        bt_entries = [e for e in (cat.get("entries") or []) if isinstance(e, dict)]
        rel = [e for e in bt_entries if str(e.get("problem", "")).lower() == title.lower()] or bt_entries
        if rel:
            return "\n".join([
                "## Relevant Breakthroughs",
                *(f"- `{e.get('id')}` ({e.get('delta_raw', 0):+.1f} pts): {e.get('transferable_rule') or e.get('title')}" for e in rel[:3]),
            ])
    except Exception:
        pass
    return ""


def compose_session_brief(workspace: Path, problem: str, goal: str = "") -> str:
    """Compose a prompt-cache-safe `< 3 KB` first-user-message brief from score, PMU/ELF/Cold3 & Triad KB."""
    ws = workspace.expanduser().resolve()
    title, repo_dir, p_meta = resolve_problem_repo(ws, problem)

    snapshot = _cfg.read_json(ws / "results" / "score-snapshot.json", {})
    snap_row = snapshot.get(title, {}) if isinstance(snapshot, dict) else {}
    cur_score = float(snap_row.get("score") or 0.0)
    status, rank = str(snap_row.get("status") or "no-submission"), snap_row.get("rank")
    ours_pub = [float(x) for x in (snap_row.get("public_times_us") or [])]

    boards = load_slim_boards(ws / "results" / "scoring-evidence" / "board-summary.json")
    b_info = boards.get(title, {}) if isinstance(boards, dict) else {}
    b_rows, tcs_raw = b_info.get("rows") or [], b_info.get("testcases") or []
    leader_score = float((b_rows[0] or {}).get("score") or 0.0) if b_rows else 0.0
    total_teams = (b_info.get("meta") or {}).get("total") or len(b_rows)

    pub_ratios = [
        min(1.0, float(tb) / ours_pub[idx])
        for idx in range(min(5, len(tcs_raw)))
        if (tb := tcs_raw[idx].get("tbest")) and idx < len(ours_pub) and ours_pub[idx] > 0
    ]
    public_pts = round(2.0 * sum(pub_ratios), 2) if pub_ratios else 0.0
    hidden_pts = round(max(0.0, cur_score - public_pts), 2) if cur_score > 0 else 0.0
    implied_r = round(hidden_pts / 90.0, 4) if cur_score > 0 else 0.0

    hist_idx = index_submission_history(ws)
    rev_hist = [dict(h) for h in reversed(hist_idx["history_by_prob"].get(title, []))]
    cal = build_problem_calibration(
        workspace=ws, repo_dir=repo_dir, title=title, cur_score=cur_score,
        public_pts=public_pts, hidden_pts=hidden_pts, implied_hidden_r=implied_r, history=rev_hist, est_by_sha7={},
    )
    strata = sorted(cal.get("strata") or [], key=lambda s: float(s.get("headroom_pts") or 0.0), reverse=True)
    top_stratum = strata[0] if strata else None
    diff = int((p_meta or {}).get("difficulty") or _cfg.DEFAULT_DIFFICULTIES.get(title, 2))
    raw_best_sc, raw_best_sha, _ = hist_idx["raw_best_by_prob"].get(title, (cur_score, "", ""))

    lines: list[str] = [
        f"# Parhelion Tuning Session · {title} (Tier x{diff})",
        f"- Score: **{cur_score:.2f}** (best `{raw_best_sc:.2f}` {raw_best_sha[:7] or '—'}, leader `{leader_score:.2f}`, rank `{f'#{rank}/{total_teams}' if rank else '—'}`, status `{status}`)",
    ]
    if top_stratum:
        lines.append(
            f"- Top Headroom Stratum: `{top_stratum.get('stratum')}` "
            f"(w_hat={top_stratum.get('learned_w')}, r={top_stratum.get('efficiency_r')}, +{top_stratum.get('headroom_pts')} pts)"
        )
    disk_cal = _cfg.read_json(ws / "results" / title / "calibration.json", {})
    esc_locked = bool(cal.get("escape_lock") or (isinstance(disk_cal, dict) and disk_cal.get("escape_lock")))
    esc_sha = cal.get("escape_sha") or (disk_cal.get("escape_sha") if isinstance(disk_cal, dict) else None)
    if esc_locked:
        lines.append(f"- **ESCAPE_LOCK ACTIVE** (bad SHA `{esc_sha}`): add reproducer to `bench/cases_correctness.json` & run `opsb bench verify-escape`.")
    if goal.strip():
        lines.append(f"- Goal: {goal.strip()}")

    detected_tags, pmu_section = _collect_fast_feedback(ws, title)
    if pmu_section:
        lines.append("\n## Fast-Feedback Telemetry")
        lines.extend(pmu_section[:6])
    if kb_digest := _collect_kb_digest(ws, title, detected_tags):
        lines.append("\n" + kb_digest)

    lines.append(
        "\n## Gate & Integrity Rules\n"
        "- Validate via `opsb test` (`--build-only` / `--fast` / full) before `opsb submit` (use MCP tools when attached).\n"
        "- Keep `bench/reference.py` frozen (SHA-256) and `bench/cases_perf.json` append-only.\n"
        "- Record verified wins/anti-patterns with SHA + `profiler`/online evidence via `parhelion_kb_record`."
    )
    return _truncate_utf8("\n".join(lines), MAX_BRIEF_BYTES)
