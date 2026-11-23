"""Unified Driver Registry, worktree preparation, brief composer, and detached worker."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..services.breakthrough_service import load_catalog
from ..services.calibration_service import build_problem_calibration
from ..services.contest_service import index_submission_history, load_slim_boards, resolve_workspace_meta
from .antigravity_driver import AntigravityDriver
from .base import (
    BaseDriver,
    DriverDiagnostic,
    SessionRecord,
    load_parhelion_config,
    resolve_parhelion_home,
    save_parhelion_config,
)
from .claude_driver import ClaudeDriver
from .codex_driver import CodexDriver
from .ext_drivers import DeepSeekDriver, GeminiDriver
from .pi_driver import PiDriver
from .session_store import update_session

DRIVER_CLASSES: dict[str, type[BaseDriver]] = {
    "antigravity": AntigravityDriver,
    "codex": CodexDriver,
    "claude": ClaudeDriver,
    "pi": PiDriver,
    "gemini": GeminiDriver,
    "deepseek": DeepSeekDriver,
}

VALID_DRIVERS: tuple[str, ...] = tuple(DRIVER_CLASSES.keys())
DEFAULT_ACTIVE_DRIVER = "antigravity"
MAX_BRIEF_BYTES = 2950


def get_active_driver_kind(cfg: dict[str, Any] | None = None) -> str:
    loaded = cfg if cfg is not None else load_parhelion_config()
    raw = str(loaded.get("active_driver") or DEFAULT_ACTIVE_DRIVER).strip().lower()
    return raw if raw in DRIVER_CLASSES else DEFAULT_ACTIVE_DRIVER


def get_driver(kind: str | None = None) -> BaseDriver:
    resolved = (kind or get_active_driver_kind()).strip().lower()
    cls = DRIVER_CLASSES.get(resolved)
    if cls is None:
        raise ValueError(f"Unknown driver '{kind}'. Supported: {', '.join(VALID_DRIVERS)}")
    return cls()


def list_drivers() -> dict[str, Any]:
    active = get_active_driver_kind()
    items = []
    for k, cls in DRIVER_CLASSES.items():
        items.append(
            {
                "kind": cls.kind,
                "display_name": cls.display_name,
                "protocol": cls.protocol,
                "active": k == active,
            }
        )
    return {"active_driver": active, "drivers": items}


def select_active_driver(kind: str) -> dict[str, Any]:
    norm = kind.strip().lower()
    if norm not in DRIVER_CLASSES:
        raise ValueError(f"Unknown driver '{kind}'. Supported: {', '.join(VALID_DRIVERS)}")
    cfg = load_parhelion_config()
    cfg["active_driver"] = norm
    save_parhelion_config(cfg)
    return {"ok": True, "active_driver": norm}


def doctor_drivers(kind: str | None = None) -> dict[str, Any]:
    active = get_active_driver_kind()
    targets = [kind.strip().lower()] if kind else list(VALID_DRIVERS)
    diagnostics: list[dict[str, Any]] = []
    for k in targets:
        drv = get_driver(k)
        diag: DriverDiagnostic = drv.discover()
        row = diag.to_dict()
        row["active"] = (k == active)
        diagnostics.append(row)
    return {"active_driver": active, "diagnostics": diagnostics}


def resolve_problem_repo(workspace: Path, problem: str) -> tuple[str, Path, dict[str, Any] | None]:
    ws = workspace.expanduser().resolve()
    meta = resolve_workspace_meta(ws)
    probs = meta.get("problems") or []
    matched = next((p for p in probs if str(p.get("title", "")).lower() == problem.lower()), None)
    if matched:
        title = str(matched["title"])
        rel = str(matched.get("path") or title)
        repo_dir = (ws / rel).resolve()
        return title, repo_dir, matched

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
    if use_main or not repo_dir.is_dir():
        return repo_dir

    git_marker = repo_dir / ".git"
    if not git_marker.exists():
        return repo_dir

    head_chk = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", "HEAD"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5.0,
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
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    if branch_chk.returncode == 0:
        cmd = ["git", "-C", str(repo_dir), "worktree", "add", str(wt_dir), safe_track]
    else:
        cmd = ["git", "-C", str(repo_dir), "worktree", "add", "-b", safe_track, str(wt_dir), "HEAD"]
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
    cut = raw[:budget].decode("utf-8", errors="ignore").rstrip()
    return cut + suffix


def compose_session_brief(workspace: Path, problem: str, goal: str = "") -> str:
    """Compose a prompt-cache-safe `< 3 KB` first-user-message brief from score, PMU/ELF/Cold3 & Triad KB."""
    ws = workspace.expanduser().resolve()
    title, repo_dir, p_meta = resolve_problem_repo(ws, problem)

    # 1. Live score/rank & calibration headroom (strictly local file reads, zero network/SSH)
    snapshot = _cfg.read_json(ws / "results" / "score-snapshot.json", {})
    snap_row = snapshot.get(title, {}) if isinstance(snapshot, dict) else {}
    cur_score = float(snap_row.get("score") or 0.0)
    status = str(snap_row.get("status") or "no-submission")
    rank = snap_row.get("rank")
    ours_pub = [float(x) for x in (snap_row.get("public_times_us") or [])]

    boards = load_slim_boards(ws / "results" / "scoring-evidence" / "board-summary.json")
    b_info = boards.get(title, {}) if isinstance(boards, dict) else {}
    b_rows = b_info.get("rows") or []
    tcs_raw = b_info.get("testcases") or []
    leader_score = float((b_rows[0] or {}).get("score") or 0.0) if b_rows else 0.0
    total_teams = (b_info.get("meta") or {}).get("total") or len(b_rows)

    pub_ratios = []
    for idx in range(min(5, len(tcs_raw))):
        tb = tcs_raw[idx].get("tbest")
        if tb and idx < len(ours_pub) and ours_pub[idx] > 0:
            pub_ratios.append(min(1.0, float(tb) / ours_pub[idx]))
    public_pts = round(2.0 * sum(pub_ratios), 2) if pub_ratios else 0.0
    hidden_pts = round(max(0.0, cur_score - public_pts), 2) if cur_score > 0 else 0.0
    implied_r = round(hidden_pts / 90.0, 4) if cur_score > 0 else 0.0

    hist_idx = index_submission_history(ws)
    rev_hist = [dict(h) for h in reversed(hist_idx["history_by_prob"].get(title, []))]
    cal = build_problem_calibration(
        workspace=ws,
        repo_dir=repo_dir,
        title=title,
        cur_score=cur_score,
        public_pts=public_pts,
        hidden_pts=hidden_pts,
        implied_hidden_r=implied_r,
        history=rev_hist,
        est_by_sha7={},
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
        lines.append(
            f"- **ESCAPE_LOCK ACTIVE** (bad SHA `{esc_sha}`): add reproducer to `bench/cases_correctness.json` & run `opsb bench verify-escape`."
        )
    if goal.strip():
        lines.append(f"- Goal: {goal.strip()}")

    # 2. Lazily load Track 1 fast-feedback bottleneck card (`fast_feedback_service`)
    detected_tags: list[str] = []
    pmu_section: list[str] = []
    try:
        from ..services import fast_feedback_service  # type: ignore

        pmu_fn = getattr(fast_feedback_service, "build_pmu_torch_card", None)
        elf_fn = getattr(fast_feedback_service, "evaluate_elf_guard", None)
        cold_fn = getattr(fast_feedback_service, "evaluate_cold3_probe", None)

        if callable(pmu_fn):
            pmu = pmu_fn(ws, title)
            if isinstance(pmu, dict):
                dom = pmu.get("dominant_bottleneck") or pmu.get("bottleneck")
                if dom and dom != "unknown":
                    detected_tags.append(str(dom))
                md_card = str(pmu.get("markdown_card") or "").strip()
                if md_card:
                    pmu_section.append(md_card)
        if callable(elf_fn):
            elf = elf_fn(ws, title)
            if isinstance(elf, dict) and elf.get("status") in ("warn", "redline"):
                pmu_section.append(f"- ELF Guard `{elf['status']}`: {len(elf.get('alerts') or [])} alert(s)")
                detected_tags.append("icache-bloat")
        if callable(cold_fn):
            c3 = cold_fn(ws, title)
            if isinstance(c3, dict) and (c3.get("bimodal_spike_detected") or c3.get("status") in ("warn", "redline")):
                pmu_section.append(f"- Cold3 Probe `{c3.get('status')}`: cold_tax={c3.get('cold_tax_us')}us")
                detected_tags.append("cold-launch-tax")
    except Exception:
        pass

    if pmu_section:
        lines.append("\n## Fast-Feedback Telemetry")
        lines.extend(pmu_section[:6])

    # 3. Lazily load Track 2 Triad KB prompt-cache-safe digest (`triad_kb_service`)
    kb_digest = ""
    try:
        from ..services import triad_kb_service  # type: ignore

        ensure_fn = getattr(triad_kb_service, "ensure_triad_kb_seeded", None)
        if callable(ensure_fn):
            try:
                ensure_fn(parhelion_home=resolve_parhelion_home())
            except TypeError:
                ensure_fn()
        q_fn = getattr(triad_kb_service, "query_kb", None)
        fmt_fn = getattr(triad_kb_service, "format_prompt_cache_digest", None)
        if callable(q_fn):
            qr = q_fn(
                bottlenecks=detected_tags or None,
                text=title,
                include_anti_patterns=True,
                limit=4,
            )
            if callable(fmt_fn):
                kb_digest = str(fmt_fn(qr, max_chars=900)).strip()
    except Exception:
        kb_digest = ""

    if not kb_digest:
        try:
            cat = load_catalog(ws)
            bt_entries = [e for e in (cat.get("entries") or []) if isinstance(e, dict)]
            rel = [e for e in bt_entries if str(e.get("problem", "")).lower() == title.lower()] or bt_entries
            if rel:
                kb_lines = ["## Relevant Breakthroughs"]
                for e in rel[:3]:
                    kb_lines.append(f"- `{e.get('id')}` ({e.get('delta_raw', 0):+.1f} pts): {e.get('transferable_rule') or e.get('title')}")
                kb_digest = "\n".join(kb_lines)
        except Exception:
            kb_digest = ""

    if kb_digest:
        lines.append("\n" + kb_digest)

    # 4. Integrity & workflow guardrails
    lines.append(
        "\n## Gate & Integrity Rules\n"
        "- Validate via `opsb test` (`--build-only` / `--fast` / full) before `opsb submit` (use MCP tools when attached).\n"
        "- Keep `bench/reference.py` frozen (SHA-256) and `bench/cases_perf.json` append-only.\n"
        "- Record verified wins/anti-patterns with SHA + `profiler`/online evidence via `parhelion_kb_record`."
    )
    return _truncate_utf8("\n".join(lines), MAX_BRIEF_BYTES)


def launch_problem_session(
    workspace: Path,
    problem: str,
    *,
    driver_kind: str | None = None,
    mode: str = "background",
    track_name: str = "",
    goal: str = "",
    resume_id: str | None = None,
    use_main: bool = False,
) -> dict[str, Any]:
    ws = workspace.expanduser().resolve()
    drv = get_driver(driver_kind)
    title, _, _ = resolve_problem_repo(ws, problem)
    worktree = prepare_session_worktree(ws, title, track_name=track_name, use_main=use_main)
    brief = compose_session_brief(ws, title, goal=goal)
    record: SessionRecord = drv.launch_session(
        workspace=ws,
        problem=title,
        worktree=worktree,
        brief=brief,
        mode=mode,
        resume_id=resume_id,
    )
    meta = dict(drv.last_launch_meta)
    return {
        "ok": True,
        "session": record.to_dict(),
        "mode": meta.get("mode", mode),
        "command": meta.get("command", []),
        "env_overrides": meta.get("env", {}),
        "mcp_config_path": meta.get("mcp_config_path"),
        "log_path": meta.get("log_path"),
        "worktree": str(worktree),
        "brief": brief,
    }


def _run_worker_job(job_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    drv = get_driver(job["driver"])
    ws = Path(job["workspace"])
    wt = Path(job["worktree"])
    session_id = str(job["session_id"])
    native_id = str(job["native_session_id"])
    resume_id = job.get("resume_id")
    cmd = [str(x) for x in job["cmd"]]
    env = {str(k): str(v) for k, v in (job.get("env") or {}).items()}
    log_path = Path(job["log_path"])

    rc = 1
    try:
        rc = drv._execute_headless_turn(
            workspace=ws,
            worktree=wt,
            brief=str(job["brief"]),
            session_id=session_id,
            native_session_id=native_id,
            resume_id=resume_id,
            cmd=cmd,
            env=env,
            log_path=log_path,
        )
    except Exception as exc:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps({"event": "worker_error", "error": str(exc)}) + "\n")
        update_session(ws, session_id, status="failed", clear_pid=True)
        return 1

    update_session(ws, session_id, status=("completed" if rc == 0 else "failed"), clear_pid=True)
    return int(rc)


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsb_core.drivers.registry")
    p.add_argument("--exec-job", required=True, help="Path to detached driver job spec JSON")
    args = p.parse_args(argv)
    return _run_worker_job(Path(args.exec_job))


if __name__ == "__main__":
    raise SystemExit(_main())
