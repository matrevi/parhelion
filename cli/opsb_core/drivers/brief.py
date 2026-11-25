"""Session worktree preparation and prompt-cache-safe `< 3 KB` brief composition."""
from __future__ import annotations

import fcntl
import importlib
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..device_env import device_text
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
    for cand in (ws / problem, ws / device_text("work_prefix") / problem):
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
    lock_path = (repo_dir / ".git" / ".parhelion-worktree.lock") if (repo_dir / ".git").is_dir() else (wt_dir.parent / ".parhelion-worktree.lock")
    with lock_path.open("a", encoding="utf-8") as lfh:
        fcntl.flock(lfh, fcntl.LOCK_EX)
        try:
            if wt_dir.exists() and (wt_dir / ".git").exists():
                return wt_dir.resolve()
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
        finally:
            fcntl.flock(lfh, fcntl.LOCK_UN)
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



def _template_layout(repo: Path) -> bool:
    """Official judge template: CMake project + run.sh, no legacy build.sh."""
    try:
        return (
            (repo / "CMakeLists.txt").is_file()
            and (repo / "run.sh").is_file()
            and not (repo / "build.sh").is_file()
        )
    except OSError:
        return False


def _clean_session_brief(ws: Path, title: str, meta: dict[str, Any], p_meta: dict[str, Any] | None, goal: str) -> str:
    """Brief for one official-template problem: judge scaffold, remote-only build, push-before-test."""
    diff = int((p_meta or {}).get("difficulty") or 2)
    repo = str((p_meta or {}).get("repo_slug") or "")
    lines = [
        f"# Parhelion Kernel Session · {title} (Tier x{diff})",
        f"- Workspace: `{ws}` | layout=clean-agents-first | repo `{repo}`",
        "",
        "## 官方模板所有权",
        "- 只读：`main.asc` `sample.py` `reference.py` `CMakeLists.txt` `run.sh`（改了判题会拒绝）",
        "- 只改：`kernel.asc`（同目录可新增 `*.asc` / `*.h`，禁止新增 `main()`）",
        "- 输入输出/容差一律以 `sample.py` 的 OUTPUTS 与 `reference.py` 为准",
        "",
        "## 硬约束",
        "- 本机没有 SDK：禁止本地编译/执行；唯一评测入口是 `opsb test " + title + "`（远端 npu，device0.lock 串行）",
        "- 在分配的工作树中提交修改，用 `git push origin HEAD` 发布自己的分支，记录完整 SHA",
        "- 候选就绪后，自行运行 `opsb submit " + title + " <sha> --note '<验证证据>'` 入队。平台负责门禁、额度和实际上传。",
        "",
        "## 迭代循环",
        "1. 读 `main.asc` / `reference.py` 明确形状、dtype、PBC 与排序规则",
        "2. 在自己的分支上实现一个可与基线比较的改动",
        "3. 自行运行 `opsb test " + title + " --sha <sha>` 进入验证队列。平台调度并行编译和设备使用。",
        "4. 验证失败先读逐例报告；用 parhelion_bench_diagnose 查看本题计时与分值的实测记录。小形状消融优先比较同一用例的 judge time。",
        "",
        "## 汇报",
        "- 自行诊断、修复、复测并跟踪回执。用 `parhelion_agent_report` 同步重要结果，只有自己无法判断下一步时才请主控决策。",
        "",
        "## 铁律",
        "- 不改其他题目录、不动 `results/` 与 `_stale_scaffold/`、不改只读模板",
        "- 同一错误重复 3 次必须换方案或上报，不做无意义重试",
    ]
    if goal.strip():
        lines.insert(2, f"- Goal: {goal.strip()}")
    return _truncate_utf8("\n".join(lines), MAX_BRIEF_BYTES)


def _clean_sprint_brief(ws: Path, probs: list[dict[str, Any]], goal: str) -> str:
    """English first turn for a contest orchestrator, including before template sync."""
    ranked = sorted(
        (p for p in probs if p.get("title")),
        key=lambda p: (-int(p.get("difficulty") or 2), str(p.get("title"))),
    )
    brief = """# Parhelion Contest Orchestrator
Read opsb.workspace.json. Use opsb --workspace with the launch root. Refresh team rank, quota and queues. Prioritize high-weight zeros and same-era gains.

On start, call parhelion_sprint_start. Set enable_auto_submit=true when requested. Inspect the returned switch and blocked slots. It starts or reuses algorithm, dataflow and bench-author workers for each problem. They coordinate via parhelion_squad. Monitor strategic events.

Discover each suite with opsb bench cases <op>. Use opsb test --case for diagnosis, opsb bench compare for ablations and opsb bench evolve for receipt-linked profiles. Buckets group case IDs. Preserve measured ICT strata: PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED and X_EXTREME.

Each worker directly queues validation with opsb test <op> --sha <sha> (--build-only, --fast or full). Parhelion schedules CPU and device time. Diagnose, fix and retest independently. Escalate only unresolved decisions.

Workers enqueue their own candidates via opsb submit <op> <sha> --note "<evidence>". Parhelion dispatches every upload. Respect manual holds, in-flight work, pacing and quota. Never use --force, --now or a direct uploader. Per problem, the first three distinct judge submission IDs are opening probes, even after an early AC. Remote compile evidence suffices. Local rejections and HTTP 429 without a judge ID consume no probe. From attempt four, apply the normal safety, gain, circuit and quota gates.

Before changing a kernel, query opsb kb query --pillar bugs --text <problem> and inspect relevant receipts and prevention notes. Read local case counts from the commit catalog and online counts from the current contest's judge. Keep predictions separate from judged scores. Follow each queued SHA through its receipt and final verdict. Reproduce WA/TLE/RE and verify the fix before retrying an escape-locked candidate. Reprioritize after each verdict. Report official rank and weighted score changes, quota used, confirmed breakthroughs, pending and held SHAs, and the next experiment."""
    brief += "\n\nMonitor strategic events. For an escape, ask the owner for a failure review and delegate a reusable bug entry. For a breakthrough, delegate an evidence-backed KB entry. After six paid attempts without a threshold gain, collect the failure summary and give a fresh worker an orthogonal direction. Relay admin directives to affected owners."
    brief += f"\n\nLaunch workspace: {ws}"
    if goal.strip():
        brief += f"\n\nUser goal: {goal.strip()}"
    if ranked:
        roster = ", ".join(f"{p['title']} (x{p.get('difficulty') or 2})" for p in ranked)
        with_roster = f"{brief}\n\nManifest problems: {roster}"
        if len(with_roster.encode("utf-8")) <= MAX_BRIEF_BYTES:
            return with_roster
    return _truncate_utf8(brief, MAX_BRIEF_BYTES)


def compose_bench_author_brief(workspace: Path, problem: str) -> str:
    """Stage-0 task: one agent per problem authors its local bench buckets.

    The judge's own testcases are hidden and the opening submissions happen before any
    calibration exists, so the local benchmark must be authored from the problem
    semantics (reference.py / sample.py) instead of copied from public anchors.
    """
    ws = workspace.expanduser().resolve()
    title, repo_dir, p_meta = resolve_problem_repo(ws, problem)
    diff = int((p_meta or {}).get("difficulty") or 2)
    if (repo_dir/"reference.py").is_file() and (repo_dir/"main.asc").is_file():
        return (f"# Case coverage for {title}\nInspect `opsb bench cases {title}`. Reuse the platform's "
                "reference suite. Define seed or parameter variants in an experimental profile for the "
                "specific coverage gap. Run the baseline and candidate on identical inputs with "
                "parhelion_test_plan using the same case IDs or bucket. Reuse matching evidence. "
                "Reserve suite=all for a submission candidate. Send run IDs, findings and the profile path "
                "directly to the optimizer with parhelion_squad. Escalate unresolved decisions only.")
    spec = _cfg.workspace_bench_specs_dir(ws) / title / "cases_perf.json"
    lines = [
        f"# Bench Bucket Author · {title} (Tier x{diff})",
        f"- Repo: `{repo_dir.name}` | 只读模板，**不要改 kernel.asc**（这个 agent 只写 bench 规格）",
        f"- 产物: `{spec}`（JSON，schema 见 ops-bench 的既有 cases_perf.json）",
        "",
        "## 目标",
        "1. 读 `reference.py` / `sample.py` 的 shapes、dtype、属性，列出该算子的真实形状族：",
        "   - 小/中/大三档 + 边界形状（空张量、单元素、非对齐尾块、极端属性）",
        "   - 每个 case 写明输入 shapes/dtype/attrs 与权重，**只依据语义推导**，不要猜隐藏用例",
        "2. 生成 8–14 个 case（至少覆盖：gate 样例形状、一个接近 UB 上限的大形状、一个尾块不对齐）",
        "3. 按 reference.impl 写可重复的输入和断言，先在本地运行；只有验证日志含该桶的 profiler 时才填实测耗时",
        "4. 若某形状会超出 UB/编译失败，标注 `skip_reason` 而不是删除，方便后续判断 strata",
        "",
        "## 输出",
        "- `cases_perf.json`：problem/version/protocol/hardware_roofline 沿用 ops-bench 格式；",
        "  保留 public_anchors，在 hidden_buckets 数组追加 id、stratum、shapes、dtype、attrs、description；未测耗时留空",
        "- 一段 5 行内说明：覆盖了哪些形状族、哪些没覆盖、预估隐藏集风险",
        "",
        "## 约束",
        "- 不改 kernel.asc / main.asc / sample.py / reference.py；只写 bench-specs 目录",
        "- 不 submit、不消耗判题额度；bench 结果只用于本地排序",
    ]
    return _truncate_utf8("\n".join(lines), MAX_BRIEF_BYTES)


def compose_session_brief(workspace: Path, problem: str, goal: str = "") -> str:
    """Compose a prompt-cache-safe `< 3 KB` first-user-message brief from score, PMU/ELF/Cold3 & Triad KB."""
    ws = workspace.expanduser().resolve()
    title, repo_dir, p_meta = resolve_problem_repo(ws, problem)
    meta = resolve_workspace_meta(ws)
    if _template_layout(repo_dir):
        return _clean_session_brief(ws, title, meta, p_meta, goal)

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
        lines.append(f"- **ESCAPE_LOCK ACTIVE** (bad SHA `{esc_sha}`): reproduce the failure on the bad SHA, run full validation on the fix, then use `opsb bench verify-escape`.")
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


def compose_contest_subagent_brief(workspace: Path, goal: str = "") -> str:
    """Compose a workspace-wide orchestration brief for the active contest layout."""
    ws = workspace.expanduser().resolve()
    meta = resolve_workspace_meta(ws)
    probs = meta.get("problems") or []
    if meta["layout"] == "clean-agents-first" or any(
        _template_layout(ws / str(p.get("path") or p.get("title"))) for p in probs
    ):
        return _clean_sprint_brief(ws, probs, goal)
    snapshot = _cfg.read_json(ws / "results" / "score-snapshot.json", {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    boards = load_slim_boards(ws / "results" / "scoring-evidence" / "board-summary.json")
    hist_idx = index_submission_history(ws)

    rows: list[tuple[float, int, str, float, float, float, str]] = []
    weighted_total = 0.0
    for p in probs:
        title = str(p.get("title") or "").strip()
        if not title:
            continue
        diff = int(p.get("difficulty") or _cfg.DEFAULT_DIFFICULTIES.get(title, 2))
        srow = snapshot.get(title) if isinstance(snapshot.get(title), dict) else {}
        cur = float(srow.get("score") or 0.0)
        weighted_total += diff * cur
        b_rows = (boards.get(title) or {}).get("rows") or [] if isinstance(boards, dict) else []
        leader = float((b_rows[0] or {}).get("score") or 0.0) if b_rows else 0.0
        best_sc = float((hist_idx["raw_best_by_prob"].get(title) or (cur, "", ""))[0] or cur)
        gap = max(0.0, leader - cur) if leader > 0 else max(0.0, 100.0 - cur)
        prio = diff * (gap + max(0.0, best_sc - cur) * 1.5)
        st = str(srow.get("status") or "—")
        rows.append((prio, diff, title, cur, best_sc, leader, st))

    rows.sort(key=lambda x: (-x[0], -x[1], x[2]))
    lines: list[str] = [
        f"# Parhelion {len(rows)}-Operator Subagent Sprint · {ws.name}",
        f"- Workspace: `{ws}` | Weighted Total: **{weighted_total:.2f}** ({len(rows)} ops)",
    ]
    if goal.strip():
        lines.append(f"- Goal: {goal.strip()}")

    lines.extend([
        "",
        "## Live Operator Priorities (Tier x Diff · Cur / Best / Leader)",
        "| Op | Tier | Score | Best | Leader | Status |",
        "|---|---:|---:|---:|---:|---|",
    ])
    for _prio, diff, title, cur, best_sc, leader, st in rows[:12]:
        lines.append(f"| `{title}` | x{diff} | {cur:.2f} | {best_sc:.2f} | {leader:.2f} | {st} |")

    lines.extend([
        "",
        "## Subagent Orchestration Protocol",
        "1. On start call parhelion_sprint_start with enable_auto_submit=true when requested. Inspect the switch, blocked slots and worker errors. Two isolated workers cover every manifest problem.",
        "2. **Fast-Feedback First**: Each subagent starts with `parhelion_bench_diagnose` / `parhelion_pmu_card` / `parhelion_elf_cold3_check` and queries the 5-Pillar KB (`parhelion_kb_query` — check `hardware` iron laws & anti-patterns before coding).",
        "3. **Lock-Safe Validation & Gated Submission**: Use `opsb test --build-only` / `--fast` / full (respecting Die 0 lock) and enqueue only gate-approved gains via `parhelion_submit_gate` / `opsb submit`.",
        "4. **Shared Memory**: Record verified silicon findings into `pillar=hardware` and reusable blocks into `pillar=snippets` via `parhelion_kb_record`.",
        "",
        "## Dispatch Policy (admin rules)",
        "5. **Prefer native subagents** of the active orchestrator; dispatch external Harness workers via `parhelion_agent_spawn` only when an admin explicitly requests multi-Harness collaboration.",
        "6. **Never dispatch `claude` workers** unless an admin prompt explicitly allows it (`PARHELION_ALLOW_CLAUDE_WORKERS=1`); the pool hides them by default.",
    ])
    return _truncate_utf8("\n".join(lines), MAX_BRIEF_BYTES)
