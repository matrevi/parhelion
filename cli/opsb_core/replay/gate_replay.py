"""Replay a workspace's judge receipts through the submission gate, one shot at a time (4.3 P21).

The 4.3 gate packages (P23/P24/P25/P29) all ask the same question: *given the last contest's real
receipts, which shots would the gate have blocked?* There was no repeatable ruler — R5's 13/231 was
counted by hand and the research summary's 18/231 replayed raw peaks. This module is that ruler.

Everything happens on a throwaway copy: ``results/`` (where every gate and receipt side effect lands:
``feedback-log.jsonl``, candidate records, ``.case-map.json``, judge-compile records), the problem
manifest, the platform ``bench-specs`` suite, and a read-only object clone of every problem repository
(``git clone --shared --no-checkout`` writes nothing into the source repo). ``PARHELION_HOME`` points
into the copy while the replay runs, so no platform state is touched either. The copy is removed at
the end; the source workspace is never opened for writing.

Each shot is judged as of its own ``submitted_at``, against the board as it stood just before it:
``latest`` keeps the previous shot's result, ``best`` keeps the historical peak. This is a **per-shot
judgement, not a counterfactual simulation** — the recorded history continues unchanged after a shot
the replay blocks, so the blocked counts are an upper bound on what a live gate would have cost.

Problems replay in parallel (one worker per problem per switch group): the estimator's Simplex-MAP
calibration is the only expensive step and it is per (problem, history length). The statistics live in
``gate_metrics``; this module is only "prepare the copy, judge each shot".
"""
from __future__ import annotations

import datetime
import json
import multiprocessing
import os
import pathlib
import shutil
import sys
import tempfile
import time
from typing import Any

#: 4.2.0 behaviour (docs/v4.3/00-PLAN.md §1): every switch the 4.2.1 gate added, flipped back.
GROUP_420: dict[str, Any] = {
    "board_mode_from_rules": False, "tle_arms_escape_lock": True, "circuit_blocks": True,
    "noise_floor_from_judge_sigma": False, "approve_low_gain_positive": False,
}
#: 4.3 P29: the constant same-source jitter the estimator used before sigma was measured.
GROUP_SIGMA: dict[str, Any] = {"jitter_sigma_from_judge_noise": False}
#: 4.3 P29: the two-tier cross-generation prediction on top of the measured sigma.
GROUP_RANKING: dict[str, Any] = {"ranking_v2": True}
GROUPS: dict[str, dict[str, Any]] = {"default": {}, "420": GROUP_420, "sigma": GROUP_SIGMA,
                                     "ranking": GROUP_RANKING}
DEFAULT_JOBS = 6


def _ts(row: dict[str, Any]) -> Any:
    from scheduler.estimator import _parse_iso
    return _parse_iso(row.get("submitted_at") or row.get("finished_at") or row.get("queued_at"))


def _bound(text: str | None) -> Any:
    """A ``--since``/``--until`` timestamp; a value without an offset is UTC."""
    from scheduler.estimator import _parse_iso
    value = _parse_iso(text) if text else None
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def _manifest(workspace: pathlib.Path, manifest: pathlib.Path | None) -> tuple[list[dict[str, Any]], str]:
    """The problem roster, read by the scheduler's own rule, plus where it came from."""
    from scheduler.parhelion_paths import load_problem_manifest, read_json_safe
    raw, ok = read_json_safe(workspace / "opsb.workspace.json")
    if ok and isinstance(raw, dict) and raw.get("problems"):
        return list(raw["problems"]), "workspace"
    if manifest is not None:  # a contest whose roster lives elsewhere (ops-bench: <prefix>/problems.json)
        doc = json.loads(pathlib.Path(manifest).read_text(encoding="utf-8"))
        problems = doc.get("problems") if isinstance(doc, dict) else doc
        return [p for p in problems or [] if isinstance(p, dict) and p.get("title")], "arg"
    found = load_problem_manifest(workspace)
    return found, "generated" if found else "absent"


def _clone_repos(workspace: pathlib.Path, copy: pathlib.Path, problems: list[dict[str, Any]]) -> list[str]:
    """Read-only object clones so S1 keeps its git checks; a missing source is reported, not fatal."""
    import subprocess
    done = []
    for item in problems:
        rel = str(item.get("path") or item.get("title") or "")
        if not rel or rel.startswith("/") or ".." in pathlib.Path(rel).parts:
            continue
        src, dst = workspace / rel, copy / rel
        if not (src / ".git").exists() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["git", "clone", "--shared", "--no-checkout", "--quiet", str(src), str(dst)],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            done.append(rel)
    return done


def _isolate_home(tmp: pathlib.Path, workspace: pathlib.Path, copy: pathlib.Path) -> dict[str, Any]:
    """Point ``PARHELION_HOME`` at the copy, keeping the real device settings and the suite S1 reads."""
    from scheduler import parhelion_paths
    live_home = parhelion_paths.get_parhelion_home()
    live_specs = parhelion_paths.workspace_bench_specs_dir(workspace)
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    device = (parhelion_paths.read_json_safe(live_home / "config.json")[0] or {}).get("device")
    if isinstance(device, dict) and device:
        (home / "config.json").write_text(json.dumps({"device": device}, ensure_ascii=False), encoding="utf-8")
    os.environ["PARHELION_HOME"] = os.environ["OPSB_HOME"] = str(home)
    specs = parhelion_paths.workspace_bench_specs_dir(copy)
    if live_specs.is_dir():
        shutil.copytree(live_specs, specs, dirs_exist_ok=True)
    return {"home": str(home), "bench_specs": specs.is_dir()}


def _prepare(workspace: pathlib.Path, manifest: pathlib.Path | None, tmp: pathlib.Path) -> dict[str, Any]:
    """Copy everything the gate reads into ``tmp`` and isolate the platform state there."""
    copy = tmp / "workspace"
    copy.mkdir()
    started = time.time()
    shutil.copytree(workspace / "results", copy / "results")
    problems, source = _manifest(workspace, manifest)
    if (workspace / "opsb.workspace.json").is_file() and manifest is None:
        # Copy the manifest itself: the suite reads `bench_strict_fp32` from it, so a generated
        # roster would silently change the reference-suite evidence S1 checks.
        shutil.copy2(workspace / "opsb.workspace.json", copy / "opsb.workspace.json")
    else:
        (copy / "opsb.workspace.json").write_text(
            json.dumps({"schema_version": 1, "layout": "clean-agents-first", "problems": problems},
                       ensure_ascii=False), encoding="utf-8")
    repos = _clone_repos(workspace, copy, problems)
    info = {"copy": str(copy), "manifest": source, "problems": len(problems), "repos": len(repos),
            "copy_sec": round(time.time() - started, 2)}
    info.update(_isolate_home(tmp, workspace, copy))
    return info


def _load_shots(copy: pathlib.Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Deduplicated judged receipts ordered by ``submitted_at``; unplaceable rows are counted out."""
    from scheduler import receipts
    rows = receipts.canonical(receipts.read_rows(copy / "results" / "submit-queue-outcome.jsonl"))
    kept: list[dict[str, Any]] = []
    skipped = {"not_judged": 0, "no_sha": 0, "no_time": 0, "used_fallback_time": 0}
    for idx, row in enumerate(rows):
        when = _ts(row)
        if not receipts.judged(row):
            skipped["not_judged"] += 1
        elif not str(row.get("sha") or "").strip():
            skipped["no_sha"] += 1
        elif when is None:
            skipped["no_time"] += 1
        else:
            skipped["used_fallback_time"] += 0 if row.get("submitted_at") else 1
            kept.append({"at": when, "idx": idx, "score": receipts.official_score(row),
                         "sha": str(row["sha"]).strip().lower(), "title": str(row.get("title") or ""),
                         "status": str(row.get("status") or "")})
    kept.sort(key=lambda s: (s["at"], s["idx"]))
    return kept, {"canonical_rows": len(rows), **skipped}


def _gate_config(name: str, switches: list[str] | None, board_mode: str | None) -> Any:
    from scheduler.gate import GATE_SWITCHES, GateConfig
    config = GateConfig()
    for key, value in GROUPS[name].items():
        setattr(config, key, value)
    for text in switches or []:
        config.set_switch(text)  # replay-scoped only; nothing is saved to config.json or the bridge
    if board_mode == "latest":
        config.keep_best_board = False
    elif board_mode == "best":
        config.board_mode_from_rules = False
    return config


class _Board:
    """The board in front of each shot, rebuilt from the recorded receipts only.

    ``latest`` shows the previous shot's result, ``best`` the historical peak; neither looks at a
    replayed verdict, so every shot is judged against the history that really happened.
    """

    def __init__(self, keeps_best: bool) -> None:
        self.keeps_best = keeps_best
        self.peak = 0.0
        self.prev: dict[str, Any] | None = None

    def before(self) -> tuple[float, str, str]:
        """``(score, status, source)`` of the board in front of one shot."""
        if self.keeps_best:
            return self.peak, ("Pass" if self.peak > 0 else "no-submission"), "peak"
        if self.prev is None:
            return 0.0, "no-submission", "none"
        if self.prev["score"] is None:  # a Pass the ranking row never scored: keep the last known board
            return self.prev["board_score"], self.prev["board_status"], "unknown"
        return float(self.prev["score"]), self.prev["status"], "previous"

    def after(self, shot: dict[str, Any], board: tuple[float, str, str]) -> tuple[float, str, str]:
        """``(score, status, source)`` of the board after the shot's *recorded* outcome."""
        score = shot["score"]
        if shot["status"] == "Pass" and (score or 0.0) > self.peak:
            self.peak = float(score)
            after = (self.peak, "Pass", "peak")
        elif self.keeps_best:
            after = board
        else:
            after = board if score is None else (float(score), shot["status"], "previous")
        self.prev = {"score": score, "status": shot["status"],
                     "board_score": after[0], "board_status": after[1]}
        return after


def _primary(verdict: Any) -> tuple[str, str]:
    """The stage that actually blocked the shot, in the gate's own precedence S1 -> S3 -> S2."""
    by_stage = {s.stage: s for s in verdict.stages}
    for stage in ("SAFETY", "CIRCUIT_BREAKER", "NOISE_FLOOR"):
        result = by_stage.get(stage)
        if result is not None and not result.passed:
            return stage, result.code
    return "", ""


def _replay_title(task: tuple[str, str, tuple[str, ...], str | None, str,
                             list[dict[str, Any]], list[dict[str, Any]], bool]) -> dict[str, Any]:
    """Worker: replay one problem's shots for one switch group; the board advances on real outcomes."""
    copy, name, switches, board_mode, title, prior, shots, intents = task
    from scheduler.estimator import ScoreEstimator
    from scheduler.gate import SubmissionGate

    from . import intent_replay

    gate = SubmissionGate(pathlib.Path(copy), estimator=ScoreEstimator(pathlib.Path(copy)),
                          config=_gate_config(name, list(switches), board_mode))
    keeps_best = gate._keeps_best(title)
    board = _Board(keeps_best)
    for shot in prior:  # shots outside --since/--until still move the board, they are just not judged
        board.after(shot, board.before())
    passed: set[str] = set()
    probe_log: list[float] = []
    last_status = ""
    out = []
    for shot in shots:
        before = board.before()
        verdict = gate._evaluate_impl(title, shot["sha"], as_of=shot["at"],
                                      override_board_score=float(before[0]),
                                      override_board_status=before[1] or None)
        stage, code = _primary(verdict)
        steps = [str(s.metrics["next_step"]) for s in verdict.stages
                 if isinstance(s.metrics, dict) and s.metrics.get("next_step")]
        record = {
            "title": title, "sha": shot["sha"], "short_sha": shot["sha"][:7],
            "submitted_at": shot["at"].isoformat(), "ts": shot["at"].timestamp(),
            "status": shot["status"], "score": shot["score"],
            "board_score": round(before[0], 2), "board_status": before[1], "board_source": before[2],
            "board_after": 0.0, "approved": bool(verdict.approved),
            "status_tag": verdict.status_tag, "stage": stage, "code": code,
            "reason": verdict.gate_reason, "next_step": steps[0] if steps else "",
            "failed": [f"{s.stage}:{s.code}" for s in verdict.stages if not s.passed],
            "board_mode": "best" if keeps_best else "latest",
            "paired_cases": int(getattr(verdict.estimate, "matched_cases", 0) or 0),
            "method": str(getattr(verdict.estimate, "estimation_method", "")),
            # 4.3 P29: what the ranking metrics need -- the gate's priority, what it predicted, and
            # the tier that prediction came from (`scheduler.ranking.adjust`).
            "priority": round(float(verdict.priority_score), 4),
            "predicted_gain": round(float(verdict.estimate.delta_expected), 4),
            "predicted_score": round(float(verdict.estimate.estimated_score), 4),
            "sigma": round(float(verdict.estimate.confidence_sigma), 4),
            "tier": (getattr(verdict.estimate, "ranking", None) or {}).get("tier", ""),
            "ranking": getattr(verdict.estimate, "ranking", None) or {},
        }
        if intents:
            extra = intent_replay.intents(gate, title, record, shot["at"], before, verdict, passed,
                                          probe_log, last_status)
            if extra:
                record["intents"] = extra
        after = board.after(shot, before)
        record["board_after"] = round(after[0], 2)
        out.append(record)
        if shot["status"] == "Pass" and (shot["score"] or 0.0) > 0:
            passed.add(shot["sha"])
        last_status = shot["status"]
    return {"group": name, "title": title, "board_mode": "best" if keeps_best else "latest", "shots": out}


def run(workspace: pathlib.Path | str, manifest: pathlib.Path | None = None,
        board_mode: str | None = None, gate_switches: list[str] | None = None,
        since: str | None = None, until: str | None = None, groups: list[str] | None = None,
        jobs: int = DEFAULT_JOBS, keep_copy: bool = False, detail: bool = False,
        intents: bool = False, ranking: bool = False) -> dict[str, Any]:
    """Replay every judged receipt of ``workspace`` through the gate, once per switch group."""
    from ..config import ensure_scheduler_path
    from . import gate_metrics
    # `cli/opsb` only puts cli/ on sys.path; every helper below imports `scheduler.*`, so the
    # repository root must be added before the first one runs (forked workers inherit it).
    ensure_scheduler_path()
    from scheduler import ranking_metrics

    started = time.time()
    source = pathlib.Path(workspace).expanduser().resolve()
    if not (source / "results" / "submit-queue-outcome.jsonl").is_file():
        raise ValueError(f"{source} has no results/submit-queue-outcome.jsonl to replay")
    names = [g.strip() for g in (groups or ["default", "420"]) if g.strip()]
    if not names or any(g not in GROUPS for g in names):
        raise ValueError(f"unknown group(s) {[g for g in names if g not in GROUPS]}; use {', '.join(GROUPS)}")
    if board_mode not in (None, "best", "latest"):
        raise ValueError("--board-mode takes best or latest")
    low, high = _bound(since), _bound(until)
    if (since and low is None) or (until and high is None):
        raise ValueError("--since/--until must be ISO-8601 timestamps")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="opsb-gate-replay-"))
    saved = {k: os.environ.get(k) for k in ("PARHELION_HOME", "OPSB_HOME")}
    try:
        info = _prepare(source, manifest, tmp)
        loaded, skipped = _load_shots(tmp / "workspace")
        window = [s for s in loaded
                  if (low is None or s["at"] >= low) and (high is None or s["at"] <= high)]
        by_title: dict[str, list[dict[str, Any]]] = {}
        before_window: dict[str, list[dict[str, Any]]] = {}
        window_ids = {id(s) for s in window}
        for shot in loaded:
            target = by_title if id(shot) in window_ids else before_window
            target.setdefault(shot["title"], []).append(shot)
        seed = gate_metrics.seed([s for shots in before_window.values() for s in shots])
        tasks = [(str(tmp / "workspace"), name, tuple(gate_switches or ()), board_mode, title,
                  before_window.get(title, []), shots, bool(intents and name == "default"))
                 for name in names for title, shots in sorted(by_title.items())]
        results: list[dict[str, Any]] = []
        if tasks:
            workers = max(1, min(int(jobs), len(tasks)))
            if workers == 1:
                results = [_replay_title(task) for task in tasks]
            else:
                context = multiprocessing.get_context(
                    "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn")
                with context.Pool(processes=workers) as pool:
                    results = pool.map(_replay_title, tasks)
        result: dict[str, Any] = {
            "ok": True, "workspace": str(source), "copy": info,
            "window": {"since": since, "until": until},
            "shots": {**skipped, "judged": len(loaded), "replayed": len(window)},
            "board_mode": {"requested": board_mode}, "groups": {},
        }
        from scheduler.gate import GATE_SWITCHES
        for name in names:
            display = _gate_config(name, gate_switches, board_mode)
            shots = sorted([s for r in results if r["group"] == name for s in r["shots"]],
                           key=lambda s: (s["ts"], s["sha"]))
            if not shots:
                continue
            modes = {s["title"]: s["board_mode"] for s in shots}
            result["groups"][name] = {
                "switches": {k: getattr(display, k) for k in sorted(GATE_SWITCHES)},
                "board_mode": "mixed" if len(set(modes.values())) > 1 else next(iter(modes.values())),
                "per_problem": modes,
                "gate": {**gate_metrics.blocked(shots), **gate_metrics.events(shots, seed)},
                "outcome": gate_metrics.outcome(shots, seed),
                "credibility": gate_metrics.credibility(shots),
                "shots_detail": shots if detail else [],
            }
            if intents and name == "default":
                # 4.3 P23 acceptance block: G4, the hypothetical probe series and the opening window.
                result["groups"][name]["intents"] = gate_metrics.intents(shots)
            if ranking:
                # 4.3 P29 acceptance block: Spearman, top-1 hit rate and the gain error.
                result["groups"][name]["ranking"] = ranking_metrics.evaluate(shots)
        result["elapsed_sec"] = round(time.time() - started, 1)
        return result
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if keep_copy:
            print(f"copy kept at {tmp}", file=sys.stderr)  # stdout stays parseable under --json
        else:
            shutil.rmtree(tmp, ignore_errors=True)
