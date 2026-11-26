"""Unified Active CLI & JSON IPC Bridge (`scheduler.cli_bridge`).

Directly embedded by `OpsSupervisor.app` and invoked by the `opsb` CLI:
    python3 -m scheduler.cli_bridge status --workspace /path/to/ops-bench
    python3 -m scheduler.cli_bridge evaluate --title <Title> --sha <SHA> [--speedup <k>]
    python3 -m scheduler.cli_bridge submit --title <Title> [--sha <SHA>] [--note <note>] [--force]
    python3 -m scheduler.cli_bridge drain [--once] [--dry-run]
    python3 -m scheduler.cli_bridge override --title <Title> --sha <SHA> --action <approve|skip|rollback>
    python3 -m scheduler.cli_bridge config [--noise-floor <pts>] [--reserve <n>]
    python3 -m scheduler.cli_bridge schedule-test --title <Title> [--level smoke|standard|final] [--fast] [--dry-run]
    python3 -m scheduler.cli_bridge suite <show|migrate|add-variant> --title <Title> [--json <variant>]
    python3 -m scheduler.cli_bridge retrospect
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import math
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

from .backfill import BackfillScheduler, offline_npu_sentinel
from .bench_evolution import compute_spearman
from .calibration import BenchmarkCalibrator
from .estimator import ScoreEstimator, _parse_iso, resolve_sha
from .gate import GateConfig, SubmissionGate
from . import judge_align_cmd, receipts
from .npu_scheduler import NpuTaskScheduler
from .parhelion_paths import (
    atomic_write_json,
    ensure_workspace_data_dirs,
    get_parhelion_home,
    read_json_safe,
    read_parhelion_scheduler_config,
    sanitize_json_value,
    workspace_bench_specs_dir,
    workspace_state_dir,
    write_parhelion_scheduler_config,
)


def _dev(key: str, default: str = "") -> str:
    """Deployment setting from `~/.parhelion/config.json` ("device").

    `scheduler` may be imported without the CLI package on `sys.path`, so the
    accessor is imported lazily and a missing CLI falls back to the neutral default.
    """
    try:
        from cli.opsb_core.device_env import device_text
        return device_text(key) or default
    except Exception:
        return default


def _detect_default_workspace() -> pathlib.Path:
    env_ws = os.environ.get("OPSB_WORKSPACE")
    if env_ws:
        return pathlib.Path(env_ws).expanduser().resolve()
    cwd = pathlib.Path.cwd().resolve()
    legacy_manifest = pathlib.Path(_dev("work_prefix")) / "problems.json"
    for anc in [cwd, *cwd.parents]:
        if (anc / "opsb.workspace.json").is_file() or (anc / legacy_manifest).is_file():
            return anc
    candidate = pathlib.Path(__file__).resolve().parents[2]
    if (candidate / legacy_manifest).is_file():
        return candidate
    return cwd


DEFAULT_WORKSPACE = _detect_default_workspace()
PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_STATE_DIR = workspace_state_dir(DEFAULT_WORKSPACE)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _append_jsonl(path: pathlib.Path, record: dict[str, Any]) -> bool:
    if path.name == "submit-queue.jsonl":
        from cli.opsb_core.services.queue_service import enqueue_once
        return enqueue_once(path.parent.parent, record)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitize_json_value(record), ensure_ascii=False, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return True


def _opt_float(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    return None


def _opt_int(val: Any) -> int | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float) and math.isfinite(val):
        return int(val)
    return None


def _valid_coverage_grid(raw: Any) -> dict[str, Any] | None:
    """Return a Swift-decodable `BenchCoverageGrid` dict, or `None` if empty/incomplete."""
    if not isinstance(raw, dict):
        return None
    rows = raw.get("rows")
    cols = raw.get("cols")
    cells = raw.get("cells")
    if not (isinstance(rows, list) and isinstance(cols, list) and isinstance(cells, list)):
        return None
    return raw


class SchedulerEngine:
    """Stateful orchestrator uniting ScoreEstimator, SubmissionGate, and NpuTaskScheduler."""

    def __init__(
        self,
        workspace: pathlib.Path = DEFAULT_WORKSPACE,
        state_dir: pathlib.Path | None = None,
        npu_host: str = "npu",
    ) -> None:
        self.workspace = workspace.resolve()
        if state_dir is not None:
            self.state_dir = pathlib.Path(state_dir).resolve()
        else:
            ensure_workspace_data_dirs(self.workspace)
            self.state_dir = workspace_state_dir(self.workspace).resolve()
        self.state_file = self.state_dir / "scheduler_state.json"
        self.npu_host = npu_host
        self._state_corrupt: bool = False
        self._raw_state = self._load_state()
        cfg_dict = dict(self._raw_state.get("config", {}))
        ph_sched = read_parhelion_scheduler_config()
        if ph_sched:
            if ph_sched.get("noise_floor_delta") is not None:
                cfg_dict["noise_floor_pts"] = float(ph_sched["noise_floor_delta"])
            if ph_sched.get("closing_reserve") is not None:
                cfg_dict["min_closing_reserve"] = int(ph_sched["closing_reserve"])
            if ph_sched.get("daily_quota_limit") is not None:
                cfg_dict["daily_limit"] = int(ph_sched["daily_quota_limit"])
            if ph_sched.get("circuit_breaker_limit") is not None:
                cfg_dict["circuit_limit_shots"] = int(ph_sched["circuit_breaker_limit"])
        if ph_sched.get("rate_limit_gap_sec") is not None:
            cfg_dict["rate_limit_gap_sec"] = int(ph_sched["rate_limit_gap_sec"])
        cfg_dict.pop("probe_submissions_per_problem", None)  # Previously persisted default was two.
        self.config = GateConfig.from_dict(cfg_dict)
        self.overrides: dict[str, dict[str, Any]] = dict(self._raw_state.get("overrides", {}))
        self.held_items: dict[str, dict[str, Any]] = dict(self._raw_state.get("held_items", {}))
        self.verified_escapes: dict[str, dict[str, Any]] = dict(self._raw_state.get("verified_escapes", {}))
        self.refresh_models()

    def refresh_models(self) -> None:
        self.estimator = ScoreEstimator(self.workspace, verified_escapes=self.verified_escapes)
        self.calibrator = BenchmarkCalibrator(
            self.workspace,
            estimator=self.estimator,
            specs_root=workspace_bench_specs_dir(self.workspace),
            verified_escapes=self.verified_escapes,
            state_dir=self.state_dir,
        )
        self.estimator._calibrator = self.calibrator
        self.gate = SubmissionGate(
            self.workspace,
            estimator=self.estimator,
            config=self.config,
            overrides=self.overrides,
        )
        self.npu_scheduler = NpuTaskScheduler(
            self.workspace,
            estimator=self.estimator,
            gate=self.gate,
            config=self.config,
            npu_host=self.npu_host,
        )
        # B0 lowest-priority backfill lane (shares the calibrator so no double fit)
        self.backfill_lane = BackfillScheduler(
            self.workspace,
            state_dir=self.state_dir,
            estimator=self.estimator,
            calibrator=self.calibrator,
            npu_scheduler=self.npu_scheduler,
            npu_host=self.npu_host,
        )
        self.calibrator.backfill = self.backfill_lane

    def _load_state(self) -> dict[str, Any]:
        default_state = {"config": GateConfig().to_dict(), "overrides": {}, "held_items": {}, "verified_escapes": {}}
        data, ok = read_json_safe(self.state_file)
        if not ok:
            # File exists on disk but is empty/half-written/corrupt: do NOT treat as absent
            # and refuse subsequent `_save_state()` overwrites so user state is never wiped.
            self._state_corrupt = True
            return default_state
        if data is None:
            self._state_corrupt = False
            return default_state
        if not isinstance(data, dict):
            self._state_corrupt = True
            return default_state
        self._state_corrupt = False
        return data

    def _save_state(self) -> None:
        if self._state_corrupt:
            raise RuntimeError(
                f"Refusing to overwrite corrupt or unreadable scheduler state file: {self.state_file}"
            )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _utc_now(),
            "config": self.config.to_dict(),
            "overrides": self.overrides,
            "held_items": self.held_items,
            "verified_escapes": self.verified_escapes,
        }
        lock_file = self.state_dir / ".scheduler_state.lock"
        with lock_file.open("a", encoding="utf-8") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                latest, ok = read_json_safe(self.state_file)
                if not ok or (latest is not None and not isinstance(latest, dict)):
                    raise RuntimeError(f"Unreadable scheduler state: {self.state_file}")
                for field in ("overrides", "held_items", "verified_escapes", "config"):
                    original = self._raw_state.get(field, {})
                    current = payload[field]
                    merged = dict((latest or {}).get(field, {}))
                    for key in original.keys() - current.keys():
                        merged.pop(key, None)
                    merged.update({key: value for key, value in current.items() if original.get(key) != value})
                    payload[field] = merged
                atomic_write_json(self.state_file, payload, indent=2, ensure_ascii=False)
                self.overrides = dict(payload["overrides"])
                self.held_items = dict(payload["held_items"])
                self.verified_escapes = dict(payload["verified_escapes"])
                self._raw_state = payload
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)

    def inspect_daemon(self) -> dict[str, Any]:
        """Read-only check of running supervision / consumer daemons and alert files."""
        daemons: list[dict[str, Any]] = []
        try:
            out = subprocess.check_output(["ps", "-eo", "pid,etime,args"], text=True, timeout=2.0)
            for line in out.splitlines():
                if "grep" in line or "uv run" in line:
                    continue
                if any(k in line for k in ("tools/supervise.py", "tools/submit-queue.py", "cli_bridge drain")):
                    parts = line.strip().split(None, 2)
                    if len(parts) >= 3 and parts[0].isdigit():
                        daemons.append({"pid": int(parts[0]), "etime": parts[1], "cmd": parts[2][:120]})
        except Exception:
            pass
        daemons.sort(key=lambda d: (int(d["pid"]), str(d["cmd"])))

        alert_file = self.workspace / "results" / "SUPERVISION-ALERT.md"
        alerts: list[str] = []
        if alert_file.is_file():
            for line in alert_file.read_text(errors="replace").splitlines():
                if line.strip().startswith("- "):
                    alerts.append(line.strip()[2:])

        live_log = self.workspace / "results" / "supervision-live.log"
        last_tick = None
        if live_log.is_file():
            try:
                with live_log.open("rb") as fh:
                    fh.seek(0, 2)
                    sz = fh.tell()
                    fh.seek(max(0, sz - 4096))
                    tail_lines = fh.read().decode("utf-8", errors="replace").splitlines()
                for ln in reversed(tail_lines):
                    if "tick " in ln and ln.startswith("["):
                        last_tick = ln.strip()
                        break
            except Exception:
                pass

        return {
            "running": bool(daemons),
            "processes": daemons,
            "last_tick": last_tick,
            "alerts": alerts,
        }

    def _skipped_short_map(self) -> dict[tuple[str, str], str]:
        """Map `(title, sha[:7])` -> latest skip timestamp across outcomes and active skip overrides."""
        out: dict[tuple[str, str], str] = {}
        for r in self.estimator.outcomes:
            if r.get("status") != "skipped":
                continue
            t_name = str(r.get("title") or "")
            csha = str(r.get("canonical_sha") or resolve_sha(self.workspace, t_name, r.get("sha")) or r.get("sha") or "").lower()
            if t_name and len(csha) >= 7:
                ts = str(r.get("finished_at") or r.get("queued_at") or "")
                k = (t_name, csha[:7])
                if k not in out or ts >= out[k]:
                    out[k] = ts
        for ov_key, ov in self.overrides.items():
            if isinstance(ov, dict) and ov.get("action") == "skip" and ":" in ov_key:
                t_name, o_sha = ov_key.split(":", 1)
                csha = resolve_sha(self.workspace, t_name, o_sha).lower()
                s7 = (csha or o_sha.lower())[:7]
                if t_name and len(s7) >= 7:
                    ts = str(ov.get("at") or "")
                    k = (t_name, s7)
                    if k not in out or ts >= out[k]:
                        out[k] = ts
        return out

    def _discover_unsubmitted_candidates(self) -> list[dict[str, Any]]:
        """Find newest validated green SHA per problem not yet submitted, and gate-evaluate it."""
        tried: set[tuple[str, str]] = set()
        tried_short: set[tuple[str, str]] = set()

        def _mark_tried(t_name: str, raw_sha: str | None) -> None:
            if not t_name or not raw_sha:
                return
            s_low = str(raw_sha).strip().lower()
            if not s_low:
                return
            tried.add((t_name, s_low))
            if len(s_low) >= 7:
                tried_short.add((t_name, s_low[:7]))

        for r in self.estimator.outcomes:
            if r.get("status") not in ("rejected",) or bool(r.get("submission_id")) or r.get("status") == "skipped":
                t_name = str(r.get("title") or "")
                csha = r.get("canonical_sha") or resolve_sha(self.workspace, t_name, r.get("sha"))
                _mark_tried(t_name, r.get("sha"))
                _mark_tried(t_name, csha)
        for rec_f in (self.workspace / "results").glob("*/*/submission-receipt.json"):
            try:
                vc = json.loads(rec_f.read_text()).get("validated_commit")
                if vc:
                    _mark_tried(rec_f.parents[1].name, vc)
            except Exception:
                continue
        for ov_key, ov in self.overrides.items():
            if isinstance(ov, dict) and ov.get("action") == "skip" and ":" in ov_key:
                t_name, o_sha = ov_key.split(":", 1)
                csha = resolve_sha(self.workspace, t_name, o_sha)
                _mark_tried(t_name, o_sha)
                _mark_tried(t_name, csha)

        candidates: list[dict[str, Any]] = []
        for title in sorted(self.estimator.problems_meta):
            ledger = self.estimator.get_problem_ledger(title)
            if ledger.confirmed_board_zero and ledger.historical_best_sha:
                verdict = self.gate.evaluate(
                    title=title,
                    sha=ledger.historical_best_sha,
                    note="auto-detected CRITICAL_RESTORE for confirmed zero board score",
                    force=True,
                )
                candidates.append(verdict.to_dict())
                continue

            # Case B: Newest passing validation in results/<Title>/*
            res_dir = self.workspace / "results" / title
            if not res_dir.is_dir():
                continue
            runs: list[tuple[str, str]] = []
            for rj in res_dir.glob("*/result.json"):
                try:
                    d = json.loads(rj.read_text())
                    if d.get("sha") and (d.get("exit_code") == 0 or self.gate.probe_allowance(title, d["sha"])["allowed"]):
                        runs.append((str(d.get("finished_at", "")), str(d["sha"]).lower()))
                except Exception:
                    continue
            runs.sort(reverse=True)
            if not runs:
                continue
            _, newest_sha = runs[0]
            if (title, newest_sha) in tried or (len(newest_sha) >= 7 and (title, newest_sha[:7]) in tried_short):
                continue
            verdict = self.gate.evaluate(title=title, sha=newest_sha, note="unsubmitted latest validation")
            candidates.append(verdict.to_dict())

        candidates.sort(
            key=lambda c: (
                0 if c["approved"] else 1,
                -float(c["priority_score"]),
                str(c["title"]),
                str(c["sha"]),
            )
        )
        return candidates

    def build_submit_queue_view(self, limit_history: int = 25) -> list[dict[str, Any]]:
        """Build unified `submit_queue` items with canonical `status_tag` and gate telemetry."""
        queue_path = self.workspace / "results" / "submit-queue.jsonl"
        q_rows = _read_jsonl(queue_path)
        outcomes = self.estimator.outcomes
        skipped_short = self._skipped_short_map()

        # Track pending vs completed queue records
        done_sha = {
            (r["title"], (r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha"))).lower())
            for r in outcomes
        }
        done_exact = {
            (
                r["title"],
                (r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha"))).lower(),
                r.get("queued_at"),
            )
            for r in outcomes
        }

        items: list[dict[str, Any]] = []

        # 1. Pending items in results/submit-queue.jsonl (deduped by (title, canonical_sha))
        pending_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for rec in q_rows:
            t = rec.get("title", "")
            csha = resolve_sha(self.workspace, t, rec.get("sha")).lower()
            q_at = rec.get("queued_at")
            if (t, csha, q_at) in done_exact:
                continue
            skip_ts = skipped_short.get((t, csha[:7])) if len(csha) >= 7 else None
            if skip_ts is not None and (not rec.get("force") or skip_ts >= str(q_at or "")):
                continue
            if not rec.get("force") and (t, csha) in done_sha:
                continue
            pk = (t, csha)
            prev_rec = pending_by_key.get(pk)
            if prev_rec is None:
                pending_by_key[pk] = rec
            else:
                prev_rank = (1 if prev_rec.get("force") else 0, str(prev_rec.get("queued_at") or ""))
                cur_rank = (1 if rec.get("force") else 0, str(q_at or ""))
                if cur_rank >= prev_rank:
                    pending_by_key[pk] = rec

        pending_verdicts: list[tuple[dict[str, Any], Any]] = []
        for (t, csha), rec in sorted(pending_by_key.items()):
            verdict = self.gate.evaluate(
                title=t,
                sha=csha,
                speedup=rec.get("speedup"),
                note=rec.get("note", ""),
                force=bool(rec.get("force", False)),
            )
            pending_verdicts.append((rec, verdict))

        pending_verdicts.sort(
            key=lambda pv: (
                -float(pv[1].priority_score),
                str(pv[1].title),
                str(pv[1].sha),
                str(pv[0].get("queued_at") or ""),
            )
        )
        pending_shas = {(v.title, v.sha.lower()) for _, v in pending_verdicts}
        pending_short = {(v.title, v.sha.lower()[:7]) for _, v in pending_verdicts if len(v.sha) >= 7}
        for rec, verdict in pending_verdicts:
            est = verdict.estimate
            items.append({
                "queue_state": "PENDING",
                "title": verdict.title,
                "sha": verdict.sha,
                "short_sha": verdict.short_sha,
                "note": rec.get("note", ""),
                "queued_at": rec.get("queued_at"),
                "submitted_at": None,
                "current_score": est.current_score,
                "estimated_score": None if "no perf telemetry" in est.detail.lower() else est.estimated_score,
                "raw_historical_best_score": est.raw_historical_best_score,
                "contemporary_best_score": est.contemporary_best_score,
                "decay_factor_gamma": est.decay_factor_gamma,
                "true_restore_gain": est.true_restore_gain,
                "online_score": None,
                "delta_expected": est.delta_expected,
                "ucb_delta": est.ucb_delta,
                "calibration_trust": est.calibration_trust,
                "explore_eligible": est.explore_eligible,
                "weighted_delta": est.weighted_delta,
                "confidence_sigma": est.confidence_sigma,
                "priority_score": verdict.priority_score,
                "status_tag": verdict.status_tag,
                "gate_reason": verdict.gate_reason,
                "stages": [s.to_dict() for s in verdict.stages],
            })

        # 2. Held items stored in scheduler_state.json (not yet pushed to submit-queue.jsonl)
        # Stable deterministic order: held_at DESC, then title ASC, sha ASC.
        held_sorted = sorted(
            self.held_items.items(),
            key=lambda kv: (str(kv[1].get("title") or ""), str(kv[1].get("sha") or "").lower(), kv[0]),
        )
        held_sorted.sort(key=lambda kv: str(kv[1].get("held_at") or ""), reverse=True)
        for h_key, h_rec in held_sorted:
            t = h_rec["title"]
            csha = str(h_rec["sha"]).lower()
            if (
                (t, csha) in done_sha
                or (t, csha) in pending_shas
                or (len(csha) >= 7 and ((t, csha[:7]) in skipped_short or (t, csha[:7]) in pending_short))
            ):
                continue
            verdict = self.gate.evaluate(
                title=t,
                sha=csha,
                speedup=h_rec.get("speedup"),
                note=h_rec.get("note", ""),
            )
            est = verdict.estimate
            items.append({
                "queue_state": "HELD",
                "agent_id": h_rec.get("agent_id", ""),
                "speedup": h_rec.get("speedup"),
                "title": verdict.title,
                "sha": verdict.sha,
                "short_sha": verdict.short_sha,
                "note": h_rec.get("note", ""),
                "queued_at": h_rec.get("held_at"),
                "submitted_at": None,
                "current_score": est.current_score,
                "estimated_score": None if "no perf telemetry" in est.detail.lower() else est.estimated_score,
                "raw_historical_best_score": est.raw_historical_best_score,
                "contemporary_best_score": est.contemporary_best_score,
                "decay_factor_gamma": est.decay_factor_gamma,
                "true_restore_gain": est.true_restore_gain,
                "online_score": None,
                "delta_expected": est.delta_expected,
                "ucb_delta": est.ucb_delta,
                "calibration_trust": est.calibration_trust,
                "explore_eligible": est.explore_eligible,
                "weighted_delta": est.weighted_delta,
                "confidence_sigma": est.confidence_sigma,
                "priority_score": verdict.priority_score,
                "status_tag": verdict.status_tag,
                "gate_reason": verdict.gate_reason,
                "stages": [s.to_dict() for s in verdict.stages],
            })

        # 3. Historical outcomes (chronologically tracked to label PASS_UP vs PASS_DOWN vs ZEROED_ERR)
        board_tracker: dict[str, float] = {}
        hist_items: list[dict[str, Any]] = []
        total_out = len(outcomes)
        start_hist_idx = max(0, total_out - limit_history) if limit_history > 0 else 0
        for idx_o, rec in enumerate(outcomes):
            t = rec.get("title", "")
            csha = rec.get("canonical_sha") or resolve_sha(self.workspace, t, rec.get("sha"))
            st = str(rec.get("status", ""))
            prev_b = board_tracker.get(t, 0.0)
            sc = receipts.official_score(rec)

            if idx_o < start_hist_idx:
                if st not in ("rejected", "skipped", "duplicate"):
                    board_tracker[t] = sc if (st == "Pass" and sc is not None and sc > 0.0) else 0.0
                continue

            ts = _parse_iso(rec.get("submitted_at") or rec.get("finished_at") or rec.get("queued_at"))
            est = self.estimator.estimate(
                title=t,
                sha=csha,
                note=rec.get("note", ""),
                as_of=ts,
                override_board_score=prev_b,
                override_board_status="Pass" if prev_b > 0 else "no-submission",
                fast_cached_cal=True,
            )

            raw_detail = str(rec.get("detail") or "")
            if st == "sync_error" or (st == "rejected" and (raw_detail.lower().startswith("poll error:") or bool(rec.get("submission_id")))):
                tag = "SYNC_ERROR"
                reason = f"Submitted (sid={str(rec.get('submission_id') or '')[:8]}) — score sync pending/interrupted"
            elif st in ("rejected", "skipped", "duplicate"):
                if st == "skipped":
                    tag = "SKIPPED"
                    reason = f"Skipped pre-submit: {rec.get('note', '')[:120]}"
                else:
                    tag = "REJECTED_GATE"
                    reason = str(rec.get("detail") or rec.get("note") or st)[:160]
            elif st != "Pass" or sc is None or sc <= 0.0:
                tag = "ZEROED_ERR"
                reason = f"Online {st} — board score zeroed (was {prev_b:.2f})"
                board_tracker[t] = 0.0
            else:
                actual_delta = round(sc - prev_b, 2)
                if sc >= prev_b:
                    tag = "PASS_UP"
                    reason = f"Online Pass {sc:.2f} ({actual_delta:+.2f} vs prior board {prev_b:.2f})"
                else:
                    tag = "PASS_DOWN"
                    reason = f"Online Pass {sc:.2f} ({actual_delta:+.2f} regression/jitter vs prior {prev_b:.2f})"
                board_tracker[t] = sc

            hist_items.append({
                "queue_state": "COMPLETED",
                "title": t,
                "sha": csha,
                "short_sha": csha[:7] if csha else "",
                "note": rec.get("note", ""),
                "queued_at": rec.get("queued_at"),
                "submitted_at": rec.get("submitted_at") or rec.get("finished_at"),
                "current_score": prev_b,
                "estimated_score": None if "no perf telemetry" in est.detail.lower() else est.estimated_score,
                "raw_historical_best_score": est.raw_historical_best_score,
                "contemporary_best_score": est.contemporary_best_score,
                "decay_factor_gamma": est.decay_factor_gamma,
                "true_restore_gain": est.true_restore_gain,
                "online_score": sc,
                "delta_expected": est.delta_expected,
                "ucb_delta": est.ucb_delta,
                "calibration_trust": est.calibration_trust,
                "explore_eligible": est.explore_eligible,
                "weighted_delta": est.weighted_delta,
                "confidence_sigma": est.confidence_sigma,
                "priority_score": round(est.weighted_delta, 2),
                "status_tag": tag,
                "gate_reason": reason,
            })

        if limit_history > 0:
            hist_items = list(reversed(hist_items[-limit_history:]))
        else:
            hist_items = list(reversed(hist_items))

        return items + hist_items

    def status(self, offline: bool = False, limit_history: int = 25) -> dict[str, Any]:
        """Full unified JSON state for `OpsSupervisor.app` and `opsb status`."""
        self.refresh_models()
        npu_live = self.npu_scheduler.inspect_live_queue(offline=offline)
        priorities = self.npu_scheduler.rank_problem_priorities(live_tasks=npu_live.get("tasks", []))
        quota = self.gate.get_quota_status()
        cbs = {
            title: self.gate.get_circuit_breaker(title).to_dict()
            for title in sorted(self.estimator.problems_meta)
        }
        ledgers = [
            self.estimator.get_problem_ledger(title).to_dict()
            for title in sorted(self.estimator.problems_meta)
        ]
        weighted_total = round(sum(x["weighted_current_score"] for x in ledgers), 2)
        unweighted_total = round(sum(x["current_score"] for x in ledgers), 2)

        cal_summaries: dict[str, dict[str, Any]] = {}
        cal_docs: dict[str, dict[str, Any]] = {}
        for title in sorted(self.estimator.problems_meta):
            cdoc = self.calibrator.load_or_calibrate(title)
            cal_docs[title] = cdoc
            cal_summaries[title] = {
                "model_version": cdoc.get("model_version"),
                "mae_pts": _opt_float(cdoc.get("mae_pts")),
                "rmse_pts": _opt_float(cdoc.get("rmse_pts")),
                "loocv_mae_pts": _opt_float(cdoc.get("loocv_mae_pts")),
                "loocv_spearman": _opt_float(cdoc.get("loocv_spearman")),
                "loocv_dir_acc": _opt_float(cdoc.get("loocv_dir_acc")),
                "calibration_trust": _opt_float(cdoc.get("calibration_trust")),
                "transfer_gamma": _opt_float(cdoc.get("transfer_gamma")),
                "beta_tightness": _opt_float(cdoc.get("beta_tightness")),
                "observations_m": _opt_int(cdoc.get("observations_m")),
                "paired_observations_m": _opt_int(cdoc.get("paired_observations_m")),
                "last_predicted_score": _opt_float(cdoc.get("last_predicted_score")),
                "last_actual_score": _opt_float(cdoc.get("last_actual_score")),
                "last_residual_pts": _opt_float(cdoc.get("last_residual_pts")),
                "escape_lock": bool(cdoc.get("escape_lock", False)),
                "escape_sha": str(cdoc["escape_sha"]) if cdoc.get("escape_sha") is not None else None,
                "escape_repro": cdoc.get("escape_repro") if isinstance(cdoc.get("escape_repro"), dict) else None,
                "unpaired_bucket_count": _opt_int(cdoc.get("unpaired_bucket_count")) or 0,
                "extra_discovered_count": len(cdoc.get("extra_discovered_buckets") or []),
                "high_residual_count": len(cdoc.get("high_residual_events") or []),
                "diagnostics": cdoc.get("diagnostics") or [],
                "strata": cdoc.get("strata") or [],
                "coverage_grid": _valid_coverage_grid(cdoc.get("coverage_grid")),
            }

        for p_row in priorities:
            p_row.calibration = cal_summaries.get(p_row.title)
            if p_row.calibration and p_row.calibration.get("escape_lock"):
                p_row.recommended_lane = "ESCAPE_LOCK (VERIFY_ESCAPE)"
                p_row.recommended_action = ((p_row.calibration.get("escape_repro") or {}).get("next_step")
                                            or "Verify the escaped submission before advancing a new candidate")

        maes = [float(v["mae_pts"]) for v in cal_summaries.values() if isinstance(v.get("mae_pts"), (int, float))]
        mean_mae = round(sum(maes) / len(maes), 2) if maes else None

        return sanitize_json_value({
            "generated_at": _utc_now(),
            "workspace": str(self.workspace),
            "contest_totals": {
                "weighted_score": weighted_total,
                "weighted_max": 2300.0,
                "unweighted_score": unweighted_total,
                "unweighted_max": 1000.0,
                "model_mae_pts": mean_mae,
                "zeroed_problems": [x["title"] for x in ledgers if x["current_score"] <= 0.0],
            },
            "daemon": self.inspect_daemon(),
            "npu_hardware": {
                "sampled_at": npu_live.get("sampled_at"),
                "probe_latency_ms": _opt_float(npu_live.get("probe_latency_ms")),
                "offline": bool(npu_live.get("offline", False)),
                "ssh_error": str(npu_live["ssh_error"]) if npu_live.get("ssh_error") is not None else None,
                "cpu_cores": _opt_int(npu_live.get("cpu_cores")) or 0,
                "load_1m": _opt_float(npu_live.get("load_1m")),
                "load_5m": _opt_float(npu_live.get("load_5m")),
                "devices": npu_live.get("devices", []),
                "die_locks": npu_live.get("die_locks", []),
                "die_duty_10m": npu_live.get("die_duty_10m"),
                "starvation_warnings": npu_live.get("starvation_warnings", []),
            },
            "npu_queue": npu_live.get("tasks", []),
            "submit_queue": self.build_submit_queue_view(limit_history=limit_history),
            "unsubmitted_candidates": self._auto_request(self._discover_unsubmitted_candidates()),
            "quota": {
                **quota.to_dict(),
                "used": int(quota.used_today),
                "remaining": int(quota.remaining_today),
                "reserved": int(quota.reserved_for_closing),
            },
            "circuit_breakers": cbs,
            "calibrations": cal_summaries,
            # B0 lane: additive field (client banner reads `unpaired_total` / `running`)
            "backfill": self.backfill_lane.summary(cal_docs),
            "problem_priorities": [p.to_dict() for p in priorities],
            "config": self.config.to_dict(),
        })

    def _auto_request(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Request approved candidates that no worker requested, so they do not wait forever.

        A worker normally hands its SHA to the platform; when it does not (it may be waiting on
        the orchestrator), an approved candidate sat READY indefinitely. With automatic
        submission on, a candidate that passed all four stages and whose validation finished at
        least ``auto_request_grace_sec`` ago is held as a platform request; the daemon's existing
        promotion then re-gates and submits it. A problem with a queued or judging submission, or
        a receipt younger than a minute, is left alone so the board can catch up first.
        """
        sched = read_parhelion_scheduler_config() or {}
        if not sched.get("auto_consumer_enabled") or sched.get("auto_request_candidates") is False:
            return candidates
        grace = float(sched.get("auto_request_grace_sec") or 120)
        now = datetime.datetime.now(datetime.timezone.utc)
        results = self.workspace / "results"
        outcomes = _read_jsonl(results / "submit-queue-outcome.jsonl")
        finished_claims = {(o.get("title"), o.get("sha"), o.get("queued_at")) for o in outcomes}
        busy = {q.get("title") for q in _read_jsonl(results / "submit-queue.jsonl")
                if (q.get("title"), q.get("sha"), q.get("queued_at")) not in finished_claims}
        for o in outcomes:
            done = _parse_iso(o.get("finished_at"))
            if o.get("status") in ("Running", "Queued", "Pending", "judging") or (
                    done is not None and (now - done).total_seconds() < 60):
                busy.add(o.get("title"))
        changed = False
        for cand in candidates:
            title, sha = cand.get("title"), str(cand.get("sha") or "").lower()
            key = f"{title}:{sha}"
            if not cand.get("approved") or cand.get("is_critical_restore") or title in busy or key in self.held_items:
                continue
            try:
                validated = _parse_iso(json.loads((results / title / sha / "result.json").read_text()).get("finished_at"))
            except (OSError, ValueError, TypeError):
                continue
            if validated is None or (now - validated).total_seconds() < grace:
                continue
            est = cand.get("estimate") or {}
            self.held_items[key] = {
                "title": title, "sha": sha, "agent_id": "", "speedup": None, "held_at": _utc_now(),
                "note": (f"platform request: approved candidate unrequested for {int(grace)}s "
                         f"(E[improvement]={est.get('expected_improvement')}, {est.get('estimation_method')})"),
                "status_tag": cand.get("status_tag"), "gate_reason": cand.get("gate_reason"),
                "requested_by": "platform",
            }
            busy.add(title)
            changed = True
        if changed:
            self._save_state()
        return candidates

    def evaluate(
        self,
        title: str,
        sha: str | None = None,
        speedup: float | None = None,
        note: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        self.refresh_models()
        verdict = self.gate.evaluate(title=title, sha=sha, speedup=speedup, note=note, force=force)
        return verdict.to_dict()

    def submit(
        self,
        title: str,
        sha: str | None = None,
        note: str = "",
        speedup: float | None = None,
        force: bool = False,
        dry_run: bool = False,
        agent_id: str = "",
    ) -> dict[str, Any]:
        """Gate-checked submission enqueue entry point (`opsb submit`)."""
        self.refresh_models()
        verdict = self.gate.evaluate(title=title, sha=sha, speedup=speedup, note=note, force=force)
        h_key = f"{verdict.title}:{verdict.sha}"

        enqueued = False
        if verdict.approved:
            if not dry_run:
                # Write-before-remove: append to submit-queue.jsonl BEFORE removing from held_items
                # so a concurrent status reader never observes the item transiently vanish.
                q_rec: dict[str, Any] = {
                    "title": verdict.title,
                    "sha": verdict.sha,
                    "note": note or f"gated: {verdict.status_tag} (E[d]={verdict.estimate.delta_expected:+.2f})",
                    "queued_at": _utc_now(),
                    "status": "queued",
                    "priority_score": verdict.priority_score,
                    "estimated_score": None if "no perf telemetry" in verdict.estimate.detail.lower() else verdict.estimate.estimated_score,
                    "delta_expected": verdict.estimate.delta_expected,
                    "estimate_method": verdict.estimate.estimation_method,
                    "sigma": verdict.estimate.confidence_sigma,
                    "expected_improvement": verdict.estimate.expected_improvement,
                    "calibration_trust": verdict.estimate.calibration_trust,
                }
                if speedup is not None:
                    q_rec["speedup"] = speedup
                if agent_id:
                    q_rec["agent_id"] = agent_id
                if verdict.forced or verdict.is_critical_restore:
                    q_rec["force"] = True
                enqueued = _append_jsonl(self.workspace / "results" / "submit-queue.jsonl", q_rec)
                if h_key in self.held_items:
                    self.held_items.pop(h_key, None)
                    self._save_state()
            else:
                enqueued = True
        else:
            if not dry_run and verdict.status_tag != "SKIPPED":
                self.held_items[h_key] = {
                    "title": verdict.title,
                    "sha": verdict.sha,
                    "note": note,
                    "speedup": speedup,
                    "held_at": _utc_now(),
                    "agent_id": agent_id,
                    "status_tag": verdict.status_tag,
                    "gate_reason": verdict.gate_reason,
                }
                self._save_state()

        return {
            "action": "submit",
            "enqueued": enqueued,
            "dry_run": dry_run,
            "verdict": verdict.to_dict(),
        }

    def override(
        self,
        title: str,
        action: str,
        sha: str | None = None,
        reason: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Operator/UI override (`approve`, `skip`, or `rollback`)."""
        self.refresh_models()
        if action not in ("approve", "skip", "rollback"):
            raise ValueError(f"Unsupported override action: {action}")

        if action == "rollback":
            ledger = self.estimator.get_problem_ledger(title)
            target_sha = resolve_sha(self.workspace, title, sha) if sha else (ledger.historical_best_sha or "")
            if not target_sha:
                raise RuntimeError(f"No verified historical best SHA found for {title} rollback")
            s1 = self.gate.evaluate_safety(title, target_sha)
            if not s1.passed:
                raise RuntimeError(f"Rollback target {title} {target_sha[:7]} failed Stage 1 SAFETY: {s1.reason}")
            ov_key = f"{title}:{target_sha}"
            if not dry_run:
                # Append to submit-queue.jsonl BEFORE clearing held_items (no disappear window).
                _append_jsonl(
                    self.workspace / "results" / "submit-queue.jsonl",
                    {
                        "title": title,
                        "sha": target_sha,
                        "note": reason or f"rollback to verified best {target_sha[:7]} ({ledger.historical_best_score:.2f})",
                        "queued_at": _utc_now(),
                        "status": "queued",
                        "force": True,
                    },
                )
                self.overrides[ov_key] = {"action": "approve", "reason": reason or "manual rollback", "at": _utc_now()}
                self.held_items.pop(ov_key, None)
                self._save_state()
            self.refresh_models()
            verdict = self.gate.evaluate(title=title, sha=target_sha, note=reason or "rollback", force=True)
            return {"action": "rollback", "dry_run": dry_run, "enqueued": True, "verdict": verdict.to_dict()}

        canonical_sha = resolve_sha(self.workspace, title, sha)
        ov_key = f"{title}:{canonical_sha}"

        if action == "approve":
            # Stage 1 SAFETY is never bypassed even on approve!
            s1 = self.gate.evaluate_safety(title, canonical_sha)
            if not s1.passed:
                return {
                    "action": "approve",
                    "dry_run": dry_run,
                    "enqueued": False,
                    "error": f"Cannot approve unsafe commit — Stage 1 SAFETY failed: {s1.reason}",
                    "safety": s1.to_dict(),
                }
            if not dry_run:
                held = dict(self.held_items.get(ov_key) or {})
                # Append to submit-queue.jsonl BEFORE updating scheduler_state.json (no disappear window).
                _append_jsonl(
                    self.workspace / "results" / "submit-queue.jsonl",
                    {
                        "title": title,
                        "sha": canonical_sha,
                        "note": reason or held.get("note") or "operator approved override",
                        "agent_id": held.get("agent_id", ""),
                        "queued_at": _utc_now(),
                        "status": "queued",
                        "force": True,
                    },
                )
                for k_old in [k for k in self.overrides if k.startswith(f"{title}:{canonical_sha[:7]}")]:
                    self.overrides.pop(k_old, None)
                self.overrides[ov_key] = {"action": "approve", "reason": reason or "operator approve", "at": _utc_now()}
                self.held_items.pop(ov_key, None)
                self._save_state()
            else:
                self.gate.overrides[ov_key] = {"action": "approve", "reason": reason or "operator approve"}
            verdict = self.gate.evaluate(title=title, sha=canonical_sha, note=reason, force=True)
            return {"action": "approve", "dry_run": dry_run, "enqueued": True, "verdict": verdict.to_dict()}

        # action == "skip"
        if not dry_run:
            now_iso = _utc_now()
            # Append terminal skipped outcome BEFORE writing skip override / removing held_items
            # so a concurrent status poll sees an atomic PENDING/HELD -> COMPLETED(SKIPPED) step.
            _append_jsonl(
                self.workspace / "results" / "submit-queue-outcome.jsonl",
                {
                    "title": title,
                    "sha": canonical_sha,
                    "note": reason or "skipped via scheduler override",
                    "queued_at": now_iso,
                    "finished_at": now_iso,
                    "status": "skipped",
                },
            )
            self.overrides[ov_key] = {"action": "skip", "reason": reason or "operator skip", "at": now_iso}
            self.held_items.pop(ov_key, None)
            for k_old in [k for k in self.held_items if k.startswith(f"{title}:{canonical_sha[:7]}")]:
                self.held_items.pop(k_old, None)
            self._save_state()
        return {
            "action": "skip",
            "dry_run": dry_run,
            "title": title,
            "sha": canonical_sha,
            "status": "skipped",
            "reason": reason or "operator skip",
        }

    def update_config(
        self,
        noise_floor: float | None = None,
        reserve: int | None = None,
        circuit_shots: int | None = None,
        circuit_hours: float | None = None,
        q0_quantum: int | None = None,
        aging_tau: float | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if noise_floor is not None:
            self.config.noise_floor_pts = float(noise_floor)
        if reserve is not None:
            self.config.min_closing_reserve = max(0, int(reserve))
        if circuit_shots is not None:
            self.config.circuit_limit_shots = max(1, int(circuit_shots))
        if circuit_hours is not None:
            self.config.circuit_window_hours = max(1.0, float(circuit_hours))
        if q0_quantum is not None:
            self.config.q0_quantum_sec = max(30, int(q0_quantum))
        if aging_tau is not None:
            self.config.aging_tau_sec = max(60.0, float(aging_tau))
        if not dry_run:
            self._save_state()
            ph_updates: dict[str, Any] = {}
            if noise_floor is not None:
                ph_updates["noise_floor_delta"] = round(self.config.noise_floor_pts, 2)
            if reserve is not None:
                ph_updates["closing_reserve"] = self.config.min_closing_reserve
            if circuit_shots is not None:
                ph_updates["circuit_breaker_limit"] = self.config.circuit_limit_shots
            if ph_updates:
                write_parhelion_scheduler_config(ph_updates)
        self.refresh_models()
        return {"updated": not dry_run, "dry_run": dry_run, "config": self.config.to_dict()}

    def drain(self, once: bool = True, interval_sec: int = 180, dry_run: bool = False) -> dict[str, Any]:
        """Single-consumer WSPT priority drain loop over `results/submit-queue.jsonl`."""
        while True:
            self.refresh_models()
            q_path = self.workspace / "results" / "submit-queue.jsonl"

            outcomes = self.estimator.outcomes
            skipped_short = self._skipped_short_map()
            done_sha = {
                (r["title"], (r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha"))).lower())
                for r in outcomes
            }
            done_exact = {
                (
                    r["title"],
                    (r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha"))).lower(),
                    r.get("queued_at"),
                )
                for r in outcomes
            }

            raw_pending: list[dict[str, Any]] = []
            for rec in _read_jsonl(q_path):
                t = rec.get("title", "")
                csha = resolve_sha(self.workspace, t, rec.get("sha")).lower()
                q_at = rec.get("queued_at")
                if (t, csha, q_at) in done_exact:
                    continue
                skip_ts = skipped_short.get((t, csha[:7])) if len(csha) >= 7 else None
                if skip_ts is not None and (not rec.get("force") or skip_ts >= str(q_at or "")):
                    continue
                if not rec.get("force") and (t, csha) in done_sha:
                    continue
                raw_pending.append({**rec, "canonical_sha": csha})

            # Per-operator inflight mutex: do not drain another commit for an operator already inflight
            inflight_titles: set[str] = set()
            terminal_sids = {
                str(r.get("submission_id"))
                for r in outcomes
                if r.get("submission_id") and r.get("status") not in ("Running", "Queued", "Pending")
            }
            now_ep = time.time()
            for r_file in (self.workspace / "results").glob("*/*/submission-receipt.json"):
                try:
                    rd = json.loads(r_file.read_text())
                    sid = str(rd.get("submission_id") or "")
                    dt = _parse_iso(rd.get("submitted_at"))
                    if sid and sid not in terminal_sids and dt and (now_ep - dt.timestamp()) < 2100:
                        inflight_titles.add(r_file.parents[1].name)
                except Exception:
                    continue
            for inf_f in get_parhelion_home().glob("inflight*.json"):
                try:
                    inf_d = json.loads(inf_f.read_text())
                    pid = int(inf_d.get("pid") or 0)
                    if pid > 0 and inf_d.get("title"):
                        os.kill(pid, 0)
                        inflight_titles.add(str(inf_d["title"]))
                except Exception:
                    continue

            approved_plan: list[tuple[dict[str, Any], Any]] = []
            held_plan: list[dict[str, Any]] = []
            for rec in raw_pending:
                if rec["title"] in inflight_titles:
                    held_plan.append({
                        "title": rec["title"],
                        "sha": rec["canonical_sha"],
                        "status_tag": "INFLIGHT_MUTEX",
                        "gate_reason": f"{rec['title']} already has an active inflight submission; keeping queued",
                    })
                    continue
                verdict = self.gate.evaluate(
                    title=rec["title"],
                    sha=rec["canonical_sha"],
                    speedup=rec.get("speedup"),
                    note=rec.get("note", ""),
                    force=bool(rec.get("force", False)),
                )
                if verdict.approved:
                    approved_plan.append((rec, verdict))
                else:
                    held_plan.append({
                        "title": verdict.title,
                        "sha": verdict.sha,
                        "status_tag": verdict.status_tag,
                        "gate_reason": verdict.gate_reason,
                    })

            # Order approved submissions deterministically: priority_score DESC -> FIFO (queued_at ASC) -> (title, sha) ASC
            approved_plan.sort(
                key=lambda pv: (
                    str(pv[0].get("queued_at") or ""),
                    str(pv[1].title),
                    str(pv[1].sha),
                )
            )
            approved_plan.sort(key=lambda pv: float(pv[1].priority_score), reverse=True)
            # Enforce at most one queued commit per operator per drain cycle
            deduped_plan: list[tuple[dict[str, Any], Any]] = []
            seen_drain_titles: set[str] = set()
            for rec, verdict in approved_plan:
                if verdict.title in seen_drain_titles:
                    held_plan.append({
                        "title": verdict.title,
                        "sha": verdict.sha,
                        "status_tag": "INFLIGHT_MUTEX",
                        "gate_reason": f"{verdict.title} already queued in current drain batch; keeping queued",
                    })
                    continue
                seen_drain_titles.add(verdict.title)
                deduped_plan.append((rec, verdict))
            approved_plan = deduped_plan
            held_plan.sort(key=lambda h: (str(h["title"]), str(h["sha"]), str(h["status_tag"])))

            if dry_run:
                return {
                    "action": "drain",
                    "dry_run": True,
                    "pending_count": len(raw_pending),
                    "approved_order": [
                        {
                            "rank": idx,
                            "title": v.title,
                            "sha": v.sha,
                            "short_sha": v.short_sha,
                            "status_tag": v.status_tag,
                            "priority_score": v.priority_score,
                            "delta_expected": v.estimate.delta_expected,
                            "weighted_delta": v.estimate.weighted_delta,
                        }
                        for idx, (_, v) in enumerate(approved_plan, 1)
                    ],
                    "held_or_rejected": held_plan,
                    "quota": self.gate.get_quota_status().to_dict(),
                }

            dispatched = []
            if approved_plan:
                rec, verdict = approved_plan[0]
                proc = subprocess.run(
                    [sys.executable, str(PACKAGE_DIR.parent / "cli" / "opsb"), "queue", "submit-now",
                     verdict.title, verdict.sha, "--workspace", str(self.workspace), "--note", rec.get("note") or ""],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                )
                if not proc.stdout.strip():
                    raise RuntimeError(proc.stderr.strip() or "submit dispatcher returned no result")
                dispatched.append(json.loads(proc.stdout))
            if once:
                return {
                    "action": "drain",
                    "dry_run": False,
                    "submitted_count": sum(bool(d.get("ok")) for d in dispatched),
                    "submitted": dispatched,
                    "held_or_rejected": held_plan,
                }
            time.sleep(max(30, interval_sec))

    def calibrate(self, title: str | None = None) -> dict[str, Any]:
        """Fit Simplex MAP calibration across one or all 10 problems and persist `calibration.json`."""
        self.refresh_models()
        targets = [title] if title else sorted(self.estimator.problems_meta)
        results: dict[str, Any] = {}
        maes: list[float] = []
        for t in targets:
            if t not in self.estimator.problems_meta:
                raise ValueError(f"Unknown problem title: {t}")
            doc = self.calibrator.calibrate_problem(t, persist=True)
            from .case_bench import has_reference
            repo = self.workspace/self.estimator.problems_meta[t]["path"]
            if has_reference(repo):
                doc["case_experiment"] = self.calibrator.evolve_bench(t)
            results[t] = doc
            if isinstance(doc.get("mae_pts"), (int, float)):
                maes.append(float(doc["mae_pts"]))
        return {
            "action": "calibrate",
            "updated_at": _utc_now(),
            "calibrated_count": len(results),
            "mean_mae_pts": round(sum(maes) / len(maes), 2) if maes else None,
            "problems": results,
        }

    def bench_status(self, title: str | None = None) -> dict[str, Any]:
        """Return stratified benchmark calibration & 6x5 coverage grid for one or all problems."""
        self.refresh_models()
        targets = [title] if title else sorted(self.estimator.problems_meta)
        out: dict[str, Any] = {}
        for t in targets:
            if t not in self.estimator.problems_meta:
                raise ValueError(f"Unknown problem title: {t}")
            out[t] = self.calibrator.load_or_calibrate(t)
        return {
            "action": "bench-status",
            "generated_at": _utc_now(),
            "problems": out[title] if title else out,
        }

    def diagnose(self, title: str | None = None, sha: str | None = None) -> dict[str, Any]:
        """Return residual diagnostics, bucket correlations, blind spots, and ROI ranking for 1 or all 10 ops."""
        self.refresh_models()
        if title:
            if title not in self.estimator.problems_meta:
                raise ValueError(f"Unknown problem title: {title}")
            return self.calibrator.diagnose_problem(title, sha=sha)
        problems_out: dict[str, Any] = {}
        for t in sorted(self.estimator.problems_meta):
            problems_out[t] = self.calibrator.diagnose_problem(t)
        return {
            "action": "diagnose",
            "generated_at": _utc_now(),
            "problem_count": len(problems_out),
            "problems": problems_out,
        }

    def evolve(
        self,
        title: str | None = None,
        dry_run: bool = False,
        min_pairs: int = 2,
        min_corr: float = 0.15,
    ) -> dict[str, Any]:
        """Online-feedback-driven append-only bench evolution (`opsb bench evolve [Title]`)."""
        self.refresh_models()
        apply = not dry_run
        if title:
            if title not in self.estimator.problems_meta:
                raise ValueError(f"Unknown problem title: {title}")
            return self.calibrator.evolve_bench(title, apply=apply, min_pairs=min_pairs, min_corr=min_corr)
        results: dict[str, Any] = {}
        total_appended = 0
        for t in sorted(self.estimator.problems_meta):
            res = self.calibrator.evolve_bench(t, apply=apply, min_pairs=min_pairs, min_corr=min_corr)
            results[t] = res
            total_appended += int(res.get("appended_count") or 0)
        return {
            "action": "evolve",
            "applied": apply,
            "generated_at": _utc_now(),
            "problem_count": len(results),
            "total_appended_buckets": total_appended,
            "problems": results,
        }

    # -- B0 backfill lane (`opsb bench backfill` button + automatic tick) ---------
    def backfill(
        self,
        title: str | None = None,
        sha: str | None = None,
        force: bool = True,
        dry_run: bool = False,
        wait_sec: float | None = None,
        execute: bool = True,
        offline: bool = False,
    ) -> dict[str, Any]:
        """One B0 lane tick.

        `force=True` (the “立即补测” button) bypasses the AUTO throttle only — the die is
        still never taken while an operator ticket (`tier >= 1`) is waiting or a die lock
        is held, and `--dry-run` performs the full admission proof without touching the die.
        """
        self.refresh_models()
        if title and title not in self.estimator.problems_meta:
            raise ValueError(f"Unknown problem title: {title}")
        if wait_sec is not None:
            self.backfill_lane.save_config(force_max_wait_sec=float(wait_sec))

        sha_note = None
        if sha:
            pinned = self.estimator.get_problem_ledger(title or "").active_board_sha if title else None
            sha_note = {
                "requested": sha[:12],
                "pinned_baseline": (pinned or "")[:12] or None,
                "matches_pinned": bool(pinned and pinned.lower().startswith(sha.lower()[:7])),
            }

        npu: dict[str, Any] | None = None
        if offline:
            npu = offline_npu_sentinel()
        else:
            try:
                npu = self.npu_scheduler.inspect_live_queue()
            except Exception as exc:  # fail closed, keep the plan printable
                npu = offline_npu_sentinel(str(exc)[:160])

        before = self.backfill_lane.scan_unpaired()
        if title:
            before = [r for r in before if r["title"] == title]
        out = self.backfill_lane.dispatch(
            force=force,
            dry_run=bool(dry_run or not execute),
            title=title,
            npu=npu,
        )
        after = self.backfill_lane.scan_unpaired()
        if title:
            after = [r for r in after if r["title"] == title]
        queued_after = self.backfill_lane.plan()
        if title:
            queued_after = [j for j in queued_after if j["title"] == title]

        measured = out.get("measured") or {}
        return {
            "ok": True,
            "action": "backfill",
            "lane": "B0",
            "tier": 0,
            "title": title,
            "force": bool(force),
            "dry_run": bool(dry_run or not execute),
            "sha_request": sha_note,
            "unpaired_before": len(before),
            "unpaired_after": len(after),
            "paired_now": max(0, len(before) - len(after)),
            "dispatched": bool(out.get("dispatched")),
            "result": out.get("result"),
            "reason": out.get("reason"),
            "blocked_by": out.get("blocked_by") or [],
            "measured": measured,
            "measured_count": len(measured),
            "session": out.get("session") or out.get("plan"),
            "admission": out.get("admission"),
            "remote": out.get("remote"),
            "queued_remaining": len(queued_after),
            "queued_remaining_buckets": sum(int(j["bucket_count"]) for j in queued_after),
            "eta_sec": round(sum(float(j["est_sec"]) for j in queued_after), 1),
            "ledger_sha": self.backfill_lane.ledger.sha256(),
            "generated_at": _utc_now(),
        }

    def backfill_auto(self) -> dict[str, Any] | None:
        """Event-driven AUTO tick: called right after an operator session releases the die.

        Never raises (a backfill must never break `opsb test`), never waits on the lock
        (`auto_max_wait_sec = 0` ⇒ single non-blocking attempt), and costs one local scan
        plus at most one read-only SSH probe when there is actually something to pair.
        """
        try:
            cfg = self.backfill_lane.config()
            if not (cfg.enabled and cfg.auto_dispatch):
                return None
            if not self.backfill_lane.plan():
                return None
            return self.backfill_lane.dispatch(force=False, dry_run=False)
        except Exception:
            return None

    def verify_escape(self, title: str, bad_sha: str, good_sha: str, review: str = "") -> dict[str, Any]:
        """Verify regression test between escaped `bad_sha` and passing `good_sha`, clearing `ESCAPE_LOCK`."""
        self.refresh_models()
        if title not in self.estimator.problems_meta:
            raise ValueError(f"Unknown problem title: {title}")
        res = self.calibrator.verify_escape(title=title, bad_sha=bad_sha, good_sha=good_sha, review=review)
        if res.get("ok") and not res.get("review_trial"):
            self.verified_escapes.update(self.calibrator.verified_escapes)
            self._save_state()
        return res

    def retrospect(self) -> dict[str, Any]:
        """Strictly causal chronological replay over `results/submit-queue-outcome.jsonl`."""
        self.refresh_models()
        outcomes = self.estimator.outcomes
        board_tracker: dict[str, float] = {}
        board_status_tracker: dict[str, str] = {}

        rows_report: list[dict[str, Any]] = []
        live_submissions = 0
        actual_zeroings = 0
        prevented_zeroings = 0
        actual_regressions = 0
        prevented_regressions = 0
        slots_saved = 0
        saved_by_stage = {"SAFETY": 0, "NOISE_FLOOR": 0, "CIRCUIT_BREAKER": 0, "QUOTA_RESERVE": 0}
        kept_positive_gains = 0
        missed_large_gains = 0
        explore_approved = 0
        explore_gains_saved = 0
        abs_errors: list[float] = []
        sq_errors: list[float] = []
        act_deltas: list[float] = []
        pred_deltas: list[float] = []

        for idx, rec in enumerate(outcomes, 1):
            t = rec.get("title", "")
            raw_sha = str(rec.get("sha", ""))
            csha = rec.get("canonical_sha") or resolve_sha(self.workspace, t, raw_sha)
            st = str(rec.get("status", ""))
            note = str(rec.get("note", ""))
            forced = bool(rec.get("force", False))

            # Causal timestamp strictly before this record took effect
            ts = _parse_iso(rec.get("submitted_at") or rec.get("finished_at") or rec.get("queued_at"))
            prev_board = board_tracker.get(t, 0.0)
            prev_b_status = board_status_tracker.get(t, "no-submission")

            # If original submission passed raw short SHA (e.g. row #1 `c43309c` rejected), preserve exact check
            eval_sha = raw_sha if (st == "rejected" and len(raw_sha) < 40) else csha
            verdict = self.gate.evaluate(
                title=t,
                sha=eval_sha,
                note=note,
                force=forced,
                as_of=ts,
                override_board_score=prev_board,
                override_board_status=prev_b_status,
            )

            sc = receipts.official_score(rec)
            is_live_shot = st not in ("rejected", "skipped", "duplicate", "sync_error")
            actual_delta = round(sc - prev_board, 2) if (is_live_shot and sc is not None) else None

            blocking_stage = next((s.stage for s in verdict.stages if not s.passed), None)
            impact = "HISTORICAL_NON_SUBMIT"

            if is_live_shot:
                live_submissions += 1
                is_zeroing = st != "Pass" or sc is None or sc <= 0.0
                is_regression = (st == "Pass" and sc is not None and prev_board > 0 and sc < prev_board)

                if is_zeroing:
                    actual_zeroings += 1
                if is_regression:
                    actual_regressions += 1

                if st == "Pass" and sc is not None and prev_board > 0:
                    err = verdict.estimate.estimated_score - sc
                    abs_errors.append(abs(err))
                    sq_errors.append(err * err)
                    act_deltas.append(float(actual_delta))
                    pred_deltas.append(float(verdict.estimate.delta_expected))

                is_explore = verdict.status_tag == "EXPLORE" or any(
                    s.code == "EXPLORE_HIGH_UPSIDE" for s in verdict.stages
                )
                if verdict.approved and is_explore:
                    explore_approved += 1
                    if actual_delta is not None and actual_delta >= self.config.noise_floor_pts:
                        explore_gains_saved += 1

                if not verdict.approved:
                    slots_saved += 1
                    if blocking_stage in saved_by_stage:
                        saved_by_stage[blocking_stage] += 1
                    if is_zeroing:
                        prevented_zeroings += 1
                        impact = f"PREVENTED_ZEROING ({st} -> saved {prev_board:.2f} pts)"
                    elif is_regression:
                        prevented_regressions += 1
                        impact = f"PREVENTED_REGRESSION ({actual_delta:+.2f} pts avoided)"
                    elif actual_delta is not None and actual_delta < self.config.noise_floor_pts:
                        impact = f"SAVED_NOISE_SLOT (actual {actual_delta:+.2f} < +{self.config.noise_floor_pts:.1f} floor)"
                    else:
                        missed_large_gains += 1
                        impact = f"FALSE_HOLD (actual {actual_delta:+.2f} pts)"
                else:
                    if actual_delta is not None and actual_delta > 0:
                        kept_positive_gains += 1
                        impact = f"APPROVED_GAIN ({actual_delta:+.2f} pts)"
                    elif is_zeroing:
                        impact = f"UNAVOIDED_PLATFORM_ZERO ({st})"
                    else:
                        impact = f"APPROVED_JITTER ({actual_delta:+.2f} pts)"

                # Advance actual board state
                board_status_tracker[t] = st
                board_tracker[t] = sc if (st == "Pass" and sc is not None) else 0.0
            else:
                if st == "rejected" and ("login" in str(rec.get("detail", "")) or "check_response" in str(rec.get("detail", ""))):
                    impact = f"WOULD_HAVE_QUOTA ({slots_saved} slots saved earlier prevented 50/day cap!)"

            rows_report.append({
                "index": idx,
                "title": t,
                "short_sha": csha[:7] if csha else raw_sha[:7],
                "actual_status": st,
                "prev_board_score": prev_board,
                "actual_score": sc,
                "actual_delta": actual_delta,
                "gate_approved": verdict.approved,
                "gate_status_tag": verdict.status_tag,
                "blocking_stage": blocking_stage,
                "estimated_score": verdict.estimate.estimated_score,
                "delta_expected": verdict.estimate.delta_expected,
                "gate_reason": verdict.gate_reason,
                "impact": impact,
            })

        mae = round(sum(abs_errors) / len(abs_errors), 2) if abs_errors else None
        rmse = round(math.sqrt(sum(sq_errors) / len(sq_errors)), 2) if sq_errors else None
        sp_rho = compute_spearman(act_deltas, pred_deltas)
        dir_all = (
            round(
                sum(
                    1
                    for a, p in zip(act_deltas, pred_deltas)
                    if (a > 0 and p > 0) or (a < 0 and p < 0) or (a == 0 and abs(p) < 0.5)
                )
                / len(act_deltas),
                3,
            )
            if act_deltas
            else None
        )
        sig_pairs = [(a, p) for a, p in zip(act_deltas, pred_deltas) if abs(a) >= 2.0]
        dir_sig = (
            round(sum(1 for a, p in sig_pairs if (a > 0 and p > 0) or (a < 0 and p < 0)) / len(sig_pairs), 3)
            if sig_pairs
            else None
        )

        return {
            "total_records": len(outcomes),
            "live_platform_submissions": live_submissions,
            "slots_saved": slots_saved,
            "slots_saved_pct": round(100.0 * slots_saved / max(1, live_submissions), 1),
            "saved_by_stage": saved_by_stage,
            "actual_zeroings": actual_zeroings,
            "prevented_zeroings": prevented_zeroings,
            "actual_regressions": actual_regressions,
            "prevented_regressions": prevented_regressions,
            "kept_positive_gains": kept_positive_gains,
            "missed_gains_above_floor": missed_large_gains,
            "explore_approved_count": explore_approved,
            "explore_gains_saved": explore_gains_saved,
            "daily_cap_exhaustion_prevented": slots_saved >= 5,
            "estimator_accuracy": {
                "evaluated_pass_pairs": len(abs_errors),
                "mae_pts": mae,
                "rmse_pts": rmse,
                "spearman_rho": round(sp_rho, 3) if sp_rho is not None else None,
                "direction_hit_rate": dir_all,
                "direction_hit_rate_sig2": dir_sig,
                "platform_intrinsic_jitter_sigma_pts": 2.70,
            },
            "records": rows_report,
        }


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--workspace",
        type=pathlib.Path,
        default=DEFAULT_WORKSPACE,
        help="Contest workspace root (auto-detected from OPSB_WORKSPACE or cwd)",
    )
    common.add_argument(
        "--state-dir",
        type=pathlib.Path,
        default=None,
        help="Optional scheduler state directory override",
    )
    common.add_argument("--npu-host", default="npu", help="SSH host alias for Accel NPU server")

    parser = argparse.ArgumentParser(
        prog="python3 -m scheduler.cli_bridge",
        description="Unified Task Sequence Scheduler & 4-Stage Submission Gate CLI Bridge",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_st = sub.add_parser("status", parents=[common], help="Output full unified JSON state")
    p_st.add_argument("--offline", action="store_true", help="Skip live SSH probe of NPU server")
    p_st.add_argument("--limit-history", type=int, default=25, help="Max historical outcomes to include")

    p_ev = sub.add_parser("evaluate", parents=[common], help="Evaluate 4-stage gate & estimator for a SHA")
    p_ev.add_argument("--title", required=True, help="Operator problem title")
    p_ev.add_argument("--sha", default=None, help="Commit SHA (defaults to repo HEAD)")
    p_ev.add_argument("--speedup", type=float, default=None, help="Optional explicit speedup ratio k")
    p_ev.add_argument("--note", default="", help="Optional commit/performance note")
    p_ev.add_argument("--force", action="store_true", help="Evaluate as forced/closing restore")

    p_sub = sub.add_parser("submit", parents=[common], help="Gate-evaluate and enqueue a validated commit")
    p_sub.add_argument("--title", required=True, help="Operator problem title")
    p_sub.add_argument("--sha", default=None, help="Commit SHA (defaults to repo HEAD)")
    p_sub.add_argument("--note", default="", help="Measured performance summary")
    p_sub.add_argument("--speedup", type=float, default=None, help="Optional explicit speedup ratio k")
    p_sub.add_argument("--force", action="store_true", help="Force restore / bypass noise floor")
    p_sub.add_argument("--dry-run", action="store_true", help="Evaluate without mutating queue files")
    p_sub.add_argument("--agent-id", default="", help="Worker inbox for the official result")

    p_dr = sub.add_parser("drain", parents=[common], help="Run WSPT single-consumer submission drain")
    p_dr.add_argument("--once", action="store_true", default=True, help="Drain current pending queue once")
    p_dr.add_argument("--watch", action="store_true", help="Loop continuously instead of exiting after one pass")
    p_dr.add_argument("--interval", type=int, default=180, help="Seconds between watch iterations")
    p_dr.add_argument("--dry-run", action="store_true", help="Preview WSPT drain plan without submitting")

    p_ov = sub.add_parser("override", parents=[common], help="Approve, skip, or rollback a submission")
    p_ov.add_argument("--title", required=True, help="Operator problem title")
    p_ov.add_argument("--sha", default=None, help="Commit SHA (optional for --action rollback)")
    p_ov.add_argument("--action", required=True, choices=["approve", "skip", "rollback"])
    p_ov.add_argument("--reason", default="", help="Audit note for override")
    p_ov.add_argument("--dry-run", action="store_true", help="Validate override without writing queue files")

    p_cfg = sub.add_parser("config", parents=[common], help="Inspect or update gate/scheduler thresholds")
    p_cfg.add_argument("--noise-floor", type=float, default=None, help="Stage 2 noise floor in points (default 2.5)")
    p_cfg.add_argument("--reserve", type=int, default=None, help="Minimum closing restore slot reserve (default 5)")
    p_cfg.add_argument("--circuit-shots", type=int, default=None, help="Non-improving shots to trip breaker (default 6)")
    p_cfg.add_argument("--circuit-hours", type=float, default=None, help="Rolling circuit window in hours (default 12)")
    p_cfg.add_argument("--q0-quantum", type=int, default=None, help="MLFQ Q0 fast-lane quantum in seconds (default 180)")
    p_cfg.add_argument("--aging-tau", type=float, default=None, help="Aging time constant tau in seconds (default 600)")
    p_cfg.add_argument("--dry-run", action="store_true", help="Preview config without saving")

    p_tst = sub.add_parser("schedule-test", parents=[common], help="3-Phase MLFQ (Q0/Q1) NPU test runner")
    p_tst.add_argument("--title", required=True, help="Operator problem title")
    p_tst.add_argument("--case", dest="case_ids", action="append")
    p_tst.add_argument("--bucket")
    p_tst.add_argument("--profile")
    p_tst.add_argument("--experiment")
    p_tst.add_argument("--remeasure", action="store_true", default=None)
    p_tst.add_argument("--suite", choices=["adaptive", "all"], help="Deprecated: adaptive=--level smoke, all=--level standard")
    p_tst.add_argument("--level", choices=["smoke", "standard", "final"], default=None,
                       help="Suite level (default standard)")
    p_tst.add_argument("--runner")
    p_tst.add_argument("--official-only",action="store_true",default=None)
    p_tst.add_argument("--timing", choices=["off","perf","all"])
    p_tst.add_argument("--repeats", type=int)
    p_tst.add_argument("--sha", default=None, help="Commit SHA (defaults to repo HEAD)")
    p_tst.add_argument("--fast", action="store_true", help="Route through Q0 fast-lane")
    p_tst.add_argument("--build-only", action="store_true", help="Run Phase A 32-core CPU compile only (0.0s NPU lock)")
    p_tst.add_argument("--pmu", action="store_true", help="Enable L1 PipeUtilization AI-Core PMU counters")
    p_tst.add_argument("--dry-run", action="store_true", help="Output 3-phase execution plan without running")

    p_cal = sub.add_parser("calibrate", parents=[common], help="Fit Simplex MAP calibration & write calibration.json")
    p_cal.add_argument("--title", default=None, help="Optional single problem title (defaults to all 10)")

    p_bf = sub.add_parser(
        "backfill",
        parents=[common],
        help="B0 lowest-priority baseline perf-bucket backfill lane (auto-detect, dedup, idempotent)",
    )
    p_bf.add_argument("--title", default=None, help="Operator problem title (default: whole B0 queue)")
    p_bf.add_argument("--sha", default=None, help="Optional baseline SHA hint (validated against the pinned board)")
    p_bf.add_argument("--auto", action="store_true", help="Automatic mode: honour throttle, never wait for the die")
    p_bf.add_argument("--force", action="store_true", default=True, help="Bypass AUTO throttle only (manual one-click)")
    p_bf.add_argument("--dry-run", action="store_true", help="Admission proof + session plan, no die acquisition")
    p_bf.add_argument("--wait-sec", type=float, default=None, help="Max seconds to wait for an idle die")
    p_bf.add_argument("--offline", action="store_true", help="Skip the read-only NPU telemetry probe")

    p_bst = sub.add_parser("bench-status", parents=[common], help="Inspect stratified calibration & 6x5 coverage grid")
    p_bst.add_argument("--title", default=None, help="Optional single problem title")

    p_dia = sub.add_parser("diagnose", parents=[common], help="Inspect residual diagnostics, bucket correlations, and blind spots")
    p_dia.add_argument("--title", default=None, help="Optional operator problem title (defaults to all 10)")
    p_dia.add_argument("--sha", default=None, help="Optional commit SHA for commit-scoped local artifact diagnostics")

    p_evo = sub.add_parser("evolve", parents=[common], help="Append-only bench evolution from online residual feedback")
    p_evo.add_argument("--title", default=None, help="Optional operator problem title (defaults to all 10)")
    p_evo.add_argument("--dry-run", action="store_true", help="Preview bucket promotions without mutating cases_perf.json")
    p_evo.add_argument("--min-pairs", type=int, default=2, help="Min paired transitions required for correlation promotion")
    p_evo.add_argument("--min-corr", type=float, default=0.15, help="Min positive correlation r for promotion")

    p_ves = sub.add_parser("verify-escape", parents=[common], help="Verify escape regression test & clear ESCAPE_LOCK")
    p_ves.add_argument("--title", required=True, help="Operator problem title")
    p_ves.add_argument("--bad-sha", required=True, help="Commit SHA that escaped local validation and failed online")
    p_ves.add_argument("--good-sha", required=True, help="Fixed commit SHA with regression reproducer passing Q1")
    p_ves.add_argument("--review", default="", help="Repair rationale for one exact-SHA online WA review")

    sub.add_parser("retrospect", parents=[common], help="Replay historical outcomes against 4-stage gate")

    p_su = sub.add_parser("suite", parents=[common], help="Show, migrate or extend the platform test suite")
    p_su.add_argument("op", choices=["show", "migrate", "add-variant"])
    p_su.add_argument("--title", required=True, help="Operator problem title")
    p_su.add_argument("--json", dest="variant_json", default=None, help="Variant object for add-variant")
    judge_align_cmd.add_parsers(sub, common)
    return parser


def _worker_suite_violation(args) -> str | None:
    """Workers pick a level; the suite, timing protocol and runner stay platform-owned."""
    from .suite import WORKER_FORBIDDEN, WORKER_MESSAGE, is_worker

    if args.command == "schedule-test" and is_worker() and any(
        getattr(args, name, None) is not None for name in WORKER_FORBIDDEN
    ):
        return WORKER_MESSAGE
    return None


def _suite_command(args) -> int:
    from . import case_bench
    from .suite import manage

    ws = pathlib.Path(args.workspace).resolve()
    try:
        try:
            repo = case_bench.problem_repo(ws, args.title)
            repo = repo if case_bench.has_reference(repo, "HEAD") else None
        except (ValueError, OSError, KeyError, subprocess.CalledProcessError):
            repo = None
        payload = manage(ws, args.title, args.op, args.variant_json, repo)
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "command": "suite", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(sanitize_json_value(payload), indent=2, ensure_ascii=False, allow_nan=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    violation = _worker_suite_violation(args)
    if violation:
        print(violation, file=sys.stderr)
        return 2
    if args.command == "suite":
        return _suite_command(args)
    if args.command in ("case-map", "receipts", "wa-repro"):
        return judge_align_cmd.run(args)
    engine = SchedulerEngine(
        workspace=args.workspace,
        state_dir=args.state_dir,
        npu_host=args.npu_host,
    )

    try:
        if args.command == "status":
            payload = engine.status(offline=args.offline, limit_history=args.limit_history)
        elif args.command == "evaluate":
            payload = engine.evaluate(
                title=args.title,
                sha=args.sha,
                speedup=args.speedup,
                note=args.note,
                force=args.force,
            )
        elif args.command == "submit":
            payload = engine.submit(
                agent_id=args.agent_id,
                title=args.title,
                sha=args.sha,
                note=args.note,
                speedup=args.speedup,
                force=args.force,
                dry_run=args.dry_run,
            )
        elif args.command == "drain":
            payload = engine.drain(
                once=not args.watch,
                interval_sec=args.interval,
                dry_run=args.dry_run,
            )
        elif args.command == "override":
            payload = engine.override(
                title=args.title,
                sha=args.sha,
                action=args.action,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        elif args.command == "config":
            payload = engine.update_config(
                noise_floor=args.noise_floor,
                reserve=args.reserve,
                circuit_shots=args.circuit_shots,
                circuit_hours=args.circuit_hours,
                q0_quantum=args.q0_quantum,
                aging_tau=args.aging_tau,
                dry_run=args.dry_run,
            )
        elif args.command == "schedule-test":
            # The level travels in the `suite` slot, which `case_bench.run` resolves as a level
            # and the extended (non-reference) path already ignores. Build-only and fast runs
            # never reach the case bench, so a level there is moot.
            level = args.suite if (args.fast or args.build_only) else (args.level or args.suite)
            payload = engine.npu_scheduler.schedule_test(
                title=args.title,
                sha=args.sha,
                fast=args.fast,
                build_only=args.build_only,
                pmu=getattr(args, "pmu", False),
                case_options={k:v for k,v in dict(ids=args.case_ids,bucket=args.bucket,
                    profile_path=args.profile,experiment=args.experiment,timing=args.timing,
                    repeats=args.repeats,runner=args.runner,remeasure=args.remeasure,suite=level,official_only=args.official_only).items() if v is not None},
                dry_run=args.dry_run,
                stream_stdout=not args.dry_run,
            )
            if not args.dry_run:
                res = payload.get("result") or {}
                # Event-driven B0 tick: the die was just released and no operator ticket
                # can exist yet -> strictly bounded, silent, never raises.
                if not args.build_only and res.get("formal", True) and not (res.get("reused") or res.get("joined") or res.get("deferred")) and not any((args.case_ids,args.profile,args.experiment,args.bucket,args.timing,args.repeats,args.official_only,args.runner)):
                    engine.backfill_auto()
                from .case_bench import result_line

                print(json.dumps(sanitize_json_value(result_line(res)), ensure_ascii=False, allow_nan=False))
                return int(res.get("exit_code", 0))
        elif args.command == "backfill":
            payload = engine.backfill(
                title=args.title,
                sha=args.sha,
                force=(False if args.auto else bool(args.force)),
                dry_run=args.dry_run,
                wait_sec=args.wait_sec,
                offline=args.offline,
            )
        elif args.command == "calibrate":
            payload = engine.calibrate(title=args.title)
        elif args.command == "bench-status":
            payload = engine.bench_status(title=args.title)
        elif args.command == "diagnose":
            payload = engine.diagnose(title=args.title, sha=getattr(args, "sha", None))
        elif args.command == "evolve":
            payload = engine.evolve(
                title=args.title,
                dry_run=args.dry_run,
                min_pairs=args.min_pairs,
                min_corr=args.min_corr,
            )
        elif args.command == "verify-escape":
            payload = engine.verify_escape(title=args.title, bad_sha=args.bad_sha, good_sha=args.good_sha, review=args.review)
            if not payload.get("ok", True):
                print(json.dumps(sanitize_json_value(payload), indent=2, ensure_ascii=False, allow_nan=False))
                return 1
        elif args.command == "retrospect":
            payload = engine.retrospect()
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    except Exception as exc:
        err_payload = {"ok": False, "command": args.command, "error": str(exc)}
        print(json.dumps(sanitize_json_value(err_payload), indent=2, ensure_ascii=False, allow_nan=False))
        return 1

    print(json.dumps(sanitize_json_value(payload), indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
