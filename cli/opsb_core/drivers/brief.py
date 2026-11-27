"""Session worktree preparation and lean (worker <= 1.5 KB + state card) brief composition."""
from __future__ import annotations

import fcntl
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..device_env import device_text
from ..services.contest_service import index_submission_history, load_slim_boards, resolve_workspace_meta

MAX_BRIEF_BYTES = 1500  # worker briefs; the appended state card is not counted
MAX_ORCHESTRATOR_BRIEF_BYTES = 2950


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


def _board_line(ws: Path, title: str) -> str:
    snapshot = _cfg.read_json(ws / "results" / "score-snapshot.json", {})
    row = snapshot.get(title) if isinstance(snapshot, dict) else None
    if not isinstance(row, dict) or row.get("score") is None:
        return ""
    rank = f", rank #{row['rank']}" if row.get("rank") else ""
    return f"Board: score {float(row.get('score') or 0.0):.2f} (status {row.get('status') or '-'}{rank})."


def _escape_line(ws: Path, title: str) -> str:
    cal = _cfg.read_json(ws / "results" / title / "calibration.json", {})
    if not (isinstance(cal, dict) and cal.get("escape_lock")):
        return ""
    step = (cal.get("escape_repro") or {}).get("next_step") if isinstance(cal.get("escape_repro"), dict) else ""
    if step:
        return f"ESCAPE_LOCK on {str(cal.get('escape_sha') or '?')[:12]}: {step}."
    return (f"ESCAPE_LOCK on {str(cal.get('escape_sha') or '?')[:12]}: reproduce the failure, run level=final on "
            "the fix, then `opsb bench verify-escape`.")


def compose_session_brief(
    workspace: Path,
    problem: str,
    goal: str = "",
    *,
    worktree: Path | None = None,
    branch: str = "",
    role: str = "worker",
    agent_id: str | None = None,
    include_state_card: bool = True,
    include_startup_pack: bool = False,
    driver: str = "",
    startup_pack_tokens: int | None = None,
    handover: str = "",
) -> str:
    """Lean worker brief (<= MAX_BRIEF_BYTES) followed by the platform state card.

    The state card is appended after the budgeted part and does not count toward it.
    ``include_startup_pack`` appends the 4.2 G start-up pack (``startup_pack.build``) instead: its
    section 1 is this same contract under the 1.5 KB cap, and its section 5 already carries the
    per-case evidence, so the small state card is not repeated.
    """
    ws = workspace.expanduser().resolve()
    title, repo_dir, p_meta = resolve_problem_repo(ws, problem)
    diff = int((p_meta or {}).get("difficulty") or _cfg.DEFAULT_DIFFICULTIES.get(title, 2))
    wt = Path(worktree) if worktree else repo_dir
    lines = [
        f"# {title} · {role or 'worker'} (weight x{diff})",
        f"Goal: {' '.join(goal.split()) or 'Raise this problem’s official score with measured, gated candidates.'}",
        f"Worktree `{wt}`" + (f" on branch `{branch}`" if branch else "")
        + "; commit there, `git push origin HEAD`, hand over full SHAs.",
    ]
    if _template_layout(repo_dir):
        lines.append("Edit only `kernel.asc` (new *.asc/*.h allowed, no new main()); `main.asc` `sample.py` "
                     "`reference.py` `CMakeLists.txt` `run.sh` are read-only.")
    else:
        lines.append("Keep `bench/reference.py` frozen and `bench/cases_perf.json` append-only.")
    lines.extend(x for x in (_board_line(ws, title), _escape_line(ws, title)) if x)
    # The rules come first: the body is cut at MAX_BRIEF_BYTES, and a long worktree path plus an
    # escape-lock line must never push the data-channel rule out of the brief (4.3.0 fix).
    lines.extend([
        "Loop: parhelion_test (level, sha) -> parhelion_test_wait -> compare with the baseline digest -> "
        "parhelion_submit_gate (preview, then enqueue=true); verdicts: parhelion_agent_inbox; report every result, "
        "negative too, via parhelion_agent_report.",
        "No ssh or device access. Board, tbest, receipts, env: parhelion_report views only, never ~/.parhelion "
        "files or opsb_core internals.",
        "intent=probe: reversible speculative shot; intent=restore: roll back. Levels: smoke self-check, "
        "standard correctness + warm timing, final once per candidate.",
        "parhelion_squad to an agent_id, role or all: claim <=280 chars, refs, evidence{class, ref}; class "
        "online_receipt = judge receipt, paired = same-batch per-case A/B, local = one local/profiler run. "
        "Hardware laws need the first two.",
        f"parhelion_kb_query when needed. CLI fallback: `opsb test {title} --sha <sha>`, "
        f"`opsb submit {title} <sha> --note \"<evidence>\"`.",
    ])
    if len("\n".join(lines).encode("utf-8")) > MAX_BRIEF_BYTES:
        lines[-1] = "parhelion_kb_query when needed."  # the CLI fallback is the first thing to give way
    body = _truncate_utf8("\n".join(lines), MAX_BRIEF_BYTES)
    if include_startup_pack:
        from . import startup_pack
        pack = startup_pack.build(
            ws, title, contract=body, worktree=wt, branch=branch, role=role, agent_id=agent_id,
            driver=driver, tokens=startup_pack_tokens, handover=handover,
        )
        # P03's meter records the pack size so `opsb replay metrics` counts it (never raises).
        startup_pack.record_size(ws, pack, agent_id=agent_id, role=role)
        return pack["text"]
    if not include_state_card:
        return body
    from ..services import state_card
    return body + "\n\n" + state_card.build(ws, title, agent_id)


DATA_CHANNEL = ("Platform data (board, tbest, receipts, env) comes from parhelion_report views "
                "board/history/per_case/env, not from ~/.parhelion files, opsb_core imports or platform internals.")
#: Seat roles with their own contract; ``race``/``recon``/``integrator``/``knowledge`` ship in 4.3 (P26).
SEAT_CONTRACTS = ("race", "recon", "integrator", "knowledge", "scout")


def compose_seat_contract(workspace: Path, role: str, *, problem: str = "", worktree: Path | str | None = None,
                          branch: str = "", goal: str = "") -> str:
    """One role contract under the 1.5 KB cap; every contract names the five required fields.

    The five fields are the one of research R2 §1.2: an exclusive directory and branch, the commands,
    the deliverable, the recipient, and the condition that makes the seat stop. The data-channel line
    stays last so a shorter contract never drops where platform facts come from.
    """
    ws = workspace.expanduser().resolve()
    role = str(role or "").strip().lower()
    if role not in SEAT_CONTRACTS:
        raise ValueError(f"Unknown seat contract {role!r}: choose one of {', '.join(SEAT_CONTRACTS)}")
    if role == "knowledge":
        title = problem or ws.name
        # A knowledge seat may be unbound (no problem): it then works in the shared workspace and has no branch.
        wt = Path(worktree).expanduser().resolve() if worktree else ws
        where = (f"Worktree `{wt}`" + (f" on branch `{branch}`" if branch else "") +
                 "; write the documents you name there and commit only there.")
        commands = ("parhelion_kb_query / parhelion_kb_record / parhelion_kb_feedback; evidence reads through "
                    "parhelion_report views history/per_case/board. You cannot run parhelion_test and cannot enqueue "
                    "a submission.")
        deliverable = ("reusable KB entries (mechanism, trigger conditions, cost, applicable problems, evidence) "
                       "plus the document paths you wrote. Your deliverable is not a score.")
        recipient = "the problem owners via parhelion_squad (recipient all) and the orchestrator via parhelion_agent_report."
        stop = ("the entries are recorded with their evidence, or the evidence is too thin to promote: report that "
                "as a follow-up instead of guessing.")
    else:
        title, repo_dir, _ = resolve_problem_repo(ws, problem or ws.name)
        wt = Path(worktree).expanduser().resolve() if worktree else repo_dir
        where = (f"Worktree `{wt}`" + (f" on branch `{branch}`" if branch else "") +
                 "; commit there and hand over full SHAs.")
        if role == "race":
            commands = ("parhelion_test (level smoke|standard|final, explicit sha) -> parhelion_test_wait "
                        "request_id=... -> compare the digest with its baseline -> parhelion_submit_gate (preview, "
                        "then enqueue=true). Levels: smoke self-check, standard correctness+warm timing, final once "
                        f"per candidate. CLI fallback: `opsb test {title} --sha <sha>`.")
            deliverable = ("a measured candidate SHA through the gate, plus the mechanism and its numbers with "
                           "parhelion_note. You are one of N tracks on this goal and a rival seat works the same "
                           "objective; there is no separate winner rule: the gate and the platform candidate "
                           "archive (top-3) keep the best track.")
            recipient = "the orchestrator (parhelion_agent_report kind=result) and your rivals via parhelion_squad."
            stop = ("three measured rounds on one mechanism show no gain, the gate holds the candidate, or the "
                    "orchestrator sends a handoff directive. No ssh, flock or direct device access.")
        elif role == "scout":
            from ..commands.docs_cmd import SAVE_HINT, status as _mirror
            mirror_doc = _mirror()
            mirror = (f"the local docs mirror `{mirror_doc['docs_dir']}` first (read and grep it), the web only "
                      "for pages it lacks" if mirror_doc["files"]
                      else "the official documentation on the web (the local mirror is still empty)")
            mirror += "; " + SAVE_HINT.format(dir=mirror_doc["docs_dir"])
            commands = (f"read {mirror}; parhelion_report views per_case/history/board and parhelion_kb_query for "
                        "context; the dossier .parhelion/dossier/ in your worktree states the gap. You cannot run "
                        "tests or enqueue a submission, and you do not edit the kernel.")
            deliverable = ("at most three findings, each one parhelion_note type=insight: claim (what the docs "
                           "say that applies here), why (document and section or URL), actionable_next (one "
                           "experiment: the single change, expected effect, how to judge it), cases.")
            recipient = ("the problem's seats via parhelion_squad (recipient all); the platform turns your first "
                         "finding into a guide card for the stuck seat.")
            stop = ("the findings are recorded, or the docs say nothing that applies: record one insight that names "
                    "what you searched, then stop.")
        elif role == "recon":
            commands = ("parhelion_report views board/per_case/bottleneck/history, parhelion_kb_query and "
                        "parhelion_bench_diagnose. You cannot run parhelion_test; parhelion_submit_gate is preview-only "
                        "for you.")
            deliverable = ("one blueprint: the measured bottleneck split, 2-3 candidate mechanisms with the "
                           "evidence that names them, and the implementation seat each one belongs to. Record it with "
                           "parhelion_note and name the document path.")
            recipient = ("the implementation seats (parhelion_squad recipient all, or impl-1..K) and the integrator; "
                         "the orchestrator gets it via parhelion_agent_report.")
            stop = ("the blueprint is delivered, the evidence you need does not exist locally (name the missing "
                    "receipt), or the orchestrator stops the seat.")
        else:  # integrator
            commands = ("merge the implementation branches (`sprint-impl-*`) into your branch, parhelion_test "
                        "level=final on the merged SHA, then parhelion_submit_gate (preview, then enqueue=true). "
                        "Never use --force or --now.")
            deliverable = ("one merged candidate SHA with its digest and merge order; report it with "
                           "parhelion_note and parhelion_agent_report.")
            recipient = ("the orchestrator (result) and the implementation seats (squad: which branch was kept and "
                         "why).")
            stop = ("the merged candidate is submitted and judged, the gate holds it, or a conflict needs an owner "
                    "decision: ask the owning implementation seat, then the orchestrator.")
    lines = [f"# {title} · {role}", f"Goal: {' '.join(goal.split()) or 'Raise the measured official score.'}", where]
    lines.extend(["Commands: " + commands, "Deliverable: " + deliverable, "Recipient: " + recipient,
                  "Stop when: " + stop, DATA_CHANNEL])
    return _truncate_utf8("\n".join(lines), MAX_BRIEF_BYTES)


def _clean_sprint_brief(ws: Path, probs: list[dict[str, Any]], goal: str) -> str:
    """English first turn for a contest orchestrator, including before template sync."""
    ranked = sorted(
        (p for p in probs if p.get("title")),
        key=lambda p: (-int(p.get("difficulty") or 2), str(p.get("title"))),
    )
    brief = """# Parhelion Contest Orchestrator
Read opsb.workspace.json. Use opsb --workspace with the launch root. Refresh team rank, quota and queues. Prioritize high-weight zeros and same-era gains.

On start, call parhelion_sprint_start. Set enable_auto_submit=true when requested. Inspect the returned switch and blocked slots. By default it starts or reuses one solo worker per problem; mode=dual adds algorithm and dataflow seats, mode=full also a bench-author. Workers address parhelion_squad by agent_id, role name or all. Monitor strategic events.

Each problem has one platform-versioned suite (opsb bench cases <op>); suite changes go through opsb bench suite add-variant. Buckets group case IDs. Preserve measured ICT strata: PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED and X_EXTREME.

Each worker directly queues validation with parhelion_test (level smoke, standard or final) and parhelion_test_wait; finished results also reach its inbox. Parhelion schedules CPU and device time; nobody uses ssh or flock. Diagnose, fix and retest independently. Escalate only unresolved decisions.

Workers enqueue their own candidates via opsb submit <op> <sha> --note "<evidence>". Parhelion dispatches every upload. Respect manual holds and in-flight work. Quota is not a gate: S4 only decides dispatch timing, so a missing slot delays an approved candidate. Never use --force, --now or a direct uploader. Per problem, distinct judge submission IDs are opening probes until the first scored Pass, at most 8, even after an early AC. Remote compile evidence suffices. Local rejections and HTTP 429 without a judge ID consume no probe. After that, apply the safety, gain and circuit gates; quota never blocks them. A speculative but reversible shot uses intent=probe (per-problem budget, needs a rollback); restoring a proven SHA uses intent=restore.

Before changing a kernel, query opsb kb query --pillar bugs --text <problem> and inspect relevant receipts and prevention notes. Keep predictions separate from judged scores. Follow each queued SHA through its receipt and final verdict. Reproduce WA/TLE/RE and verify the fix before retrying an escape-locked candidate. Reprioritize after each verdict. Report official rank and weighted score changes, quota used, confirmed breakthroughs, pending and held SHAs, and the next experiment."""
    brief += ("\n\nMonitor strategic events. For an escape, ask the owner for a failure review and delegate a reusable "
              "bug entry. For a breakthrough, delegate an evidence-backed KB entry. After six paid attempts without a "
              "threshold gain, collect the failure summary and give a fresh worker an orthogonal direction. Relay "
              "admin directives to affected owners. A provider fault suspends its driver; after the provider "
              "recovers, rerun parhelion_sprint_start.")
    brief += f"\n\nLaunch workspace: {ws}"
    if goal.strip():
        brief += f"\n\nUser goal: {goal.strip()}"
    if ranked:
        roster = ", ".join(f"{p['title']} (x{p.get('difficulty') or 2})" for p in ranked)
        with_roster = f"{brief}\n\nManifest problems: {roster}"
        if len(with_roster.encode("utf-8")) <= MAX_ORCHESTRATOR_BRIEF_BYTES:
            return with_roster
    return _truncate_utf8(brief, MAX_ORCHESTRATOR_BRIEF_BYTES)


def compose_bench_author_brief(workspace: Path, problem: str) -> str:
    """Lean brief for an explicit calibration (bench-author) seat."""
    ws = workspace.expanduser().resolve()
    title, repo_dir, p_meta = resolve_problem_repo(ws, problem)
    diff = int((p_meta or {}).get("difficulty") or 2)
    if (repo_dir/"reference.py").is_file() and (repo_dir/"main.asc").is_file():
        return _truncate_utf8("\n".join([
            f"# {title} · bench-author (weight x{diff})",
            f"Own case coverage for {title}. Inspect `opsb bench cases {title}` and compare it with official "
            "receipts and their matching local reports.",
            "Calibrate with parhelion_test level=final, then parhelion_test_wait; compare the digest with the "
            "receipt on identical inputs. Reuse matching evidence instead of re-running.",
            f"The suite is platform-owned: propose variants to the orchestrator (parhelion_agent_report "
            f"kind=question), which runs `opsb bench suite add-variant {title} --json '<variant>'`.",
            "Share run IDs and findings with parhelion_squad (agent_id, role name or all).",
            "Do not edit kernels, submit, or touch the device directly (no ssh/flock).",
        ]), MAX_BRIEF_BYTES)
    spec = _cfg.workspace_bench_specs_dir(ws) / title / "cases_perf.json"
    lines = [
        f"# Bench Bucket Author · {title} (Tier x{diff})",
        f"- 产物: `{spec}`（只写 bench-specs 目录，**不要改 kernel.asc**）",
        "1. 依据 `reference.py` / `sample.py` 的语义列出形状族：小/中/大、空张量、单元素、非对齐尾块、极端属性；不猜隐藏用例",
        "2. 写 8–14 个 case，含 gate 样例形状、接近 UB 上限的大形状和一个尾块不对齐形状",
        "3. 超出 UB 或编译失败的形状标注 `skip_reason`；未测耗时留空",
        "4. 校准用 parhelion_test level=final 和 parhelion_test_wait；不 submit、不直连设备",
        "- 最后用 5 行说明覆盖范围与隐藏集风险",
    ]
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
        "1. On start call parhelion_sprint_start with enable_auto_submit=true when requested. Inspect the switch, blocked slots and worker errors. One solo worker covers each manifest problem by default (mode=dual or mode=full adds seats).",
        "2. **Fast-Feedback First**: Each subagent starts with `parhelion_bench_diagnose` / `parhelion_pmu_card` / `parhelion_elf_cold3_check` and queries the 5-Pillar KB (`parhelion_kb_query` — check `hardware` iron laws & anti-patterns before coding).",
        "3. **Platform-Owned Validation & Gated Submission**: Use `parhelion_test` (level smoke/standard/final) and `parhelion_test_wait`; never ssh or flock. Enqueue only gate-approved gains via `parhelion_submit_gate` / `opsb submit`.",
        "4. **Shared Memory**: Record verified silicon findings into `pillar=hardware` and reusable blocks into `pillar=snippets` via `parhelion_kb_record`.",
        "",
        "## Dispatch Policy (admin rules)",
        "5. **Prefer native subagents** of the active orchestrator; dispatch external Harness workers via `parhelion_agent_spawn` only when an admin explicitly requests multi-Harness collaboration.",
        "6. **Never dispatch `claude` workers** unless an admin prompt explicitly allows it (`PARHELION_ALLOW_CLAUDE_WORKERS=1`); the pool hides them by default.",
    ])
    return _truncate_utf8("\n".join(lines), MAX_ORCHESTRATOR_BRIEF_BYTES)
