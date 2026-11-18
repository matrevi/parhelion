"""Unified Active CLI & JSON IPC Bridge (`scheduler.cli_bridge`).

Directly embedded by `OpsSupervisor.app` and invoked by the `opsb` CLI:
    python3 -m scheduler.cli_bridge status --workspace /Users/winbeau/Competitions/ops-bench
    python3 -m scheduler.cli_bridge evaluate --title <Title> --sha <SHA> [--speedup <k>]
    python3 -m scheduler.cli_bridge submit --title <Title> [--sha <SHA>] [--note <note>] [--force]
    python3 -m scheduler.cli_bridge drain [--once] [--dry-run]
    python3 -m scheduler.cli_bridge override --title <Title> --sha <SHA> --action <approve|skip|rollback>
    python3 -m scheduler.cli_bridge config [--noise-floor <pts>] [--reserve <n>]
    python3 -m scheduler.cli_bridge schedule-test --title <Title> [--fast] [--dry-run]
    python3 -m scheduler.cli_bridge retrospect
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import math
import pathlib
import subprocess
import sys
import time
from typing import Any

from .calibration import BenchmarkCalibrator
from .estimator import ScoreEstimator, _parse_iso, resolve_sha
from .gate import GateConfig, SubmissionGate
from .npu_scheduler import NpuTaskScheduler

DEFAULT_WORKSPACE = pathlib.Path("/Users/winbeau/Competitions/ops-bench")
PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_STATE_DIR = PACKAGE_DIR / ".state"


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


def _append_jsonl(path: pathlib.Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class SchedulerEngine:
    """Stateful orchestrator uniting ScoreEstimator, SubmissionGate, and NpuTaskScheduler."""

    def __init__(
        self,
        workspace: pathlib.Path = DEFAULT_WORKSPACE,
        state_dir: pathlib.Path | None = None,
        npu_host: str = "npu",
    ) -> None:
        self.workspace = workspace.resolve()
        self.state_dir = (state_dir or DEFAULT_STATE_DIR).resolve()
        self.state_file = self.state_dir / "scheduler_state.json"
        self.npu_host = npu_host
        self._raw_state = self._load_state()
        self.config = GateConfig.from_dict(self._raw_state.get("config", {}))
        self.overrides: dict[str, dict[str, Any]] = dict(self._raw_state.get("overrides", {}))
        self.held_items: dict[str, dict[str, Any]] = dict(self._raw_state.get("held_items", {}))
        self.verified_escapes: dict[str, dict[str, Any]] = dict(self._raw_state.get("verified_escapes", {}))
        self.refresh_models()

    def refresh_models(self) -> None:
        self.estimator = ScoreEstimator(self.workspace, verified_escapes=self.verified_escapes)
        self.calibrator = BenchmarkCalibrator(
            self.workspace,
            estimator=self.estimator,
            verified_escapes=self.verified_escapes,
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

    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.is_file():
            return {"config": GateConfig().to_dict(), "overrides": {}, "held_items": {}, "verified_escapes": {}}
        try:
            return json.loads(self.state_file.read_text())
        except Exception:
            return {"config": GateConfig().to_dict(), "overrides": {}, "held_items": {}, "verified_escapes": {}}

    def _save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _utc_now(),
            "config": self.config.to_dict(),
            "overrides": self.overrides,
            "held_items": self.held_items,
            "verified_escapes": self.verified_escapes,
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.state_file)

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

    def _discover_unsubmitted_candidates(self) -> list[dict[str, Any]]:
        """Find newest validated green SHA per problem not yet submitted, and gate-evaluate it."""
        tried: set[tuple[str, str]] = {
            (r["title"], r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha")))
            for r in self.estimator.outcomes
            if r.get("status") not in ("rejected",)
        }
        for rec_f in (self.workspace / "results").glob("*/*/submission-receipt.json"):
            try:
                vc = json.loads(rec_f.read_text()).get("validated_commit")
                if vc:
                    tried.add((rec_f.parents[1].name, vc.lower()))
            except Exception:
                continue

        candidates: list[dict[str, Any]] = []
        for title in sorted(self.estimator.problems_meta):
            ledger = self.estimator.get_problem_ledger(title)
            # Case A: Board is 0 / no-submission and a verified historical best SHA exists!
            if ledger.current_score <= 0.0 and ledger.historical_best_sha:
                verdict = self.gate.evaluate(
                    title=title,
                    sha=ledger.historical_best_sha,
                    note="auto-detected CRITICAL_RESTORE for zeroed/missing board row",
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
                    if d.get("exit_code") == 0 and d.get("sha"):
                        runs.append((str(d.get("finished_at", "")), str(d["sha"]).lower()))
                except Exception:
                    continue
            runs.sort(reverse=True)
            if not runs:
                continue
            _, newest_sha = runs[0]
            if (title, newest_sha) in tried:
                continue
            verdict = self.gate.evaluate(title=title, sha=newest_sha, note="unsubmitted latest validation")
            candidates.append(verdict.to_dict())

        candidates.sort(key=lambda c: (c["approved"], c["priority_score"]), reverse=True)
        return candidates

    def build_submit_queue_view(self, limit_history: int = 25) -> list[dict[str, Any]]:
        """Build unified `submit_queue` items with canonical `status_tag` and gate telemetry."""
        queue_path = self.workspace / "results" / "submit-queue.jsonl"
        q_rows = _read_jsonl(queue_path)
        outcomes = self.estimator.outcomes

        # Track pending vs completed queue records
        done_sha = {
            (r["title"], r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha")))
            for r in outcomes
        }
        done_exact = {
            (
                r["title"],
                r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha")),
                r.get("queued_at"),
            )
            for r in outcomes
        }

        items: list[dict[str, Any]] = []

        # 1. Pending items in results/submit-queue.jsonl
        pending_verdicts: list[tuple[dict[str, Any], Any]] = []
        for rec in q_rows:
            t = rec.get("title", "")
            csha = resolve_sha(self.workspace, t, rec.get("sha"))
            q_at = rec.get("queued_at")
            if (t, csha, q_at) in done_exact:
                continue
            if not rec.get("force") and (t, csha) in done_sha:
                continue
            verdict = self.gate.evaluate(
                title=t,
                sha=csha,
                speedup=rec.get("speedup"),
                note=rec.get("note", ""),
                force=bool(rec.get("force", False)),
            )
            pending_verdicts.append((rec, verdict))

        pending_verdicts.sort(key=lambda pv: pv[1].priority_score, reverse=True)
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
                "estimated_score": est.estimated_score,
                "raw_historical_best_score": est.raw_historical_best_score,
                "contemporary_best_score": est.contemporary_best_score,
                "decay_factor_gamma": est.decay_factor_gamma,
                "true_restore_gain": est.true_restore_gain,
                "online_score": None,
                "delta_expected": est.delta_expected,
                "weighted_delta": est.weighted_delta,
                "confidence_sigma": est.confidence_sigma,
                "priority_score": verdict.priority_score,
                "status_tag": verdict.status_tag,
                "gate_reason": verdict.gate_reason,
                "stages": [s.to_dict() for s in verdict.stages],
            })

        # 2. Held items stored in scheduler_state.json (not yet pushed to submit-queue.jsonl)
        for h_key, h_rec in sorted(self.held_items.items(), key=lambda kv: kv[1].get("held_at", ""), reverse=True):
            t = h_rec["title"]
            csha = h_rec["sha"]
            if (t, csha) in done_sha:
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
                "title": verdict.title,
                "sha": verdict.sha,
                "short_sha": verdict.short_sha,
                "note": h_rec.get("note", ""),
                "queued_at": h_rec.get("held_at"),
                "submitted_at": None,
                "current_score": est.current_score,
                "estimated_score": est.estimated_score,
                "raw_historical_best_score": est.raw_historical_best_score,
                "contemporary_best_score": est.contemporary_best_score,
                "decay_factor_gamma": est.decay_factor_gamma,
                "true_restore_gain": est.true_restore_gain,
                "online_score": None,
                "delta_expected": est.delta_expected,
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
            sc = float(rec["score"]) if isinstance(rec.get("score"), (int, float)) else None

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

            if st in ("rejected", "skipped", "duplicate"):
                if st == "skipped":
                    tag = "HOLD_LOW_GAIN"
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
                "estimated_score": est.estimated_score,
                "raw_historical_best_score": est.raw_historical_best_score,
                "contemporary_best_score": est.contemporary_best_score,
                "decay_factor_gamma": est.decay_factor_gamma,
                "true_restore_gain": est.true_restore_gain,
                "online_score": sc,
                "delta_expected": est.delta_expected,
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
        for title in sorted(self.estimator.problems_meta):
            cdoc = self.calibrator.load_or_calibrate(title)
            cal_summaries[title] = {
                "mae_pts": cdoc.get("mae_pts"),
                "rmse_pts": cdoc.get("rmse_pts"),
                "beta_tightness": cdoc.get("beta_tightness"),
                "observations_m": cdoc.get("observations_m"),
                "paired_observations_m": cdoc.get("paired_observations_m"),
                "last_predicted_score": cdoc.get("last_predicted_score"),
                "last_actual_score": cdoc.get("last_actual_score"),
                "last_residual_pts": cdoc.get("last_residual_pts"),
                "escape_lock": bool(cdoc.get("escape_lock", False)),
                "escape_sha": cdoc.get("escape_sha"),
                "unpaired_bucket_count": int(cdoc.get("unpaired_bucket_count") or 0),
                "diagnostics": cdoc.get("diagnostics") or [],
                "strata": cdoc.get("strata") or [],
                "coverage_grid": cdoc.get("coverage_grid") or {},
            }

        for p_row in priorities:
            p_row.calibration = cal_summaries.get(p_row.title)
            if p_row.calibration and p_row.calibration.get("escape_lock") and p_row.current_score <= 0.0:
                p_row.recommended_lane = "ESCAPE_LOCK (VERIFY_ESCAPE)"

        maes = [float(v["mae_pts"]) for v in cal_summaries.values() if isinstance(v.get("mae_pts"), (int, float))]
        mean_mae = round(sum(maes) / len(maes), 2) if maes else None

        return {
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
                "probe_latency_ms": npu_live.get("probe_latency_ms"),
                "offline": npu_live.get("offline"),
                "ssh_error": npu_live.get("ssh_error"),
                "cpu_cores": npu_live.get("cpu_cores"),
                "load_1m": npu_live.get("load_1m"),
                "load_5m": npu_live.get("load_5m"),
                "devices": npu_live.get("devices", []),
                "die_locks": npu_live.get("die_locks", []),
                "die_duty_10m": npu_live.get("die_duty_10m"),
                "starvation_warnings": npu_live.get("starvation_warnings", []),
            },
            "npu_queue": npu_live.get("tasks", []),
            "submit_queue": self.build_submit_queue_view(limit_history=limit_history),
            "unsubmitted_candidates": self._discover_unsubmitted_candidates(),
            "quota": {
                **quota.to_dict(),
                "used": quota.used_today,
                "remaining": quota.remaining_today,
                "reserved": quota.reserved_for_closing,
            },
            "circuit_breakers": cbs,
            "calibrations": cal_summaries,
            "problem_priorities": [p.to_dict() for p in priorities],
            "config": self.config.to_dict(),
        }

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
    ) -> dict[str, Any]:
        """Gate-checked submission enqueue entry point (`opsb submit`)."""
        self.refresh_models()
        verdict = self.gate.evaluate(title=title, sha=sha, speedup=speedup, note=note, force=force)
        h_key = f"{verdict.title}:{verdict.sha}"

        enqueued = False
        if verdict.approved:
            if not dry_run:
                self.held_items.pop(h_key, None)
                self._save_state()
                q_rec: dict[str, Any] = {
                    "title": verdict.title,
                    "sha": verdict.sha,
                    "note": note or f"gated: {verdict.status_tag} (E[d]={verdict.estimate.delta_expected:+.2f})",
                    "queued_at": _utc_now(),
                    "status": "queued",
                    "priority_score": verdict.priority_score,
                    "estimated_score": verdict.estimate.estimated_score,
                    "delta_expected": verdict.estimate.delta_expected,
                }
                if speedup is not None:
                    q_rec["speedup"] = speedup
                if verdict.forced or verdict.is_critical_restore:
                    q_rec["force"] = True
                _append_jsonl(self.workspace / "results" / "submit-queue.jsonl", q_rec)
            enqueued = True
        else:
            if not dry_run and verdict.status_tag in ("HOLD_LOW_GAIN", "CIRCUIT_OPEN"):
                self.held_items[h_key] = {
                    "title": verdict.title,
                    "sha": verdict.sha,
                    "note": note,
                    "speedup": speedup,
                    "held_at": _utc_now(),
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
                self.overrides[ov_key] = {"action": "approve", "reason": reason or "manual rollback", "at": _utc_now()}
                self.held_items.pop(ov_key, None)
                self._save_state()
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
                self.overrides[ov_key] = {"action": "approve", "reason": reason or "operator approve", "at": _utc_now()}
                held = self.held_items.pop(ov_key, {})
                self._save_state()
                _append_jsonl(
                    self.workspace / "results" / "submit-queue.jsonl",
                    {
                        "title": title,
                        "sha": canonical_sha,
                        "note": reason or held.get("note") or "operator approved override",
                        "queued_at": _utc_now(),
                        "status": "queued",
                        "force": True,
                    },
                )
            else:
                self.gate.overrides[ov_key] = {"action": "approve", "reason": reason or "operator approve"}
            verdict = self.gate.evaluate(title=title, sha=canonical_sha, note=reason, force=True)
            return {"action": "approve", "dry_run": dry_run, "enqueued": True, "verdict": verdict.to_dict()}

        # action == "skip"
        if not dry_run:
            self.overrides[ov_key] = {"action": "skip", "reason": reason or "operator skip", "at": _utc_now()}
            self.held_items.pop(ov_key, None)
            self._save_state()
            _append_jsonl(
                self.workspace / "results" / "submit-queue-outcome.jsonl",
                {
                    "title": title,
                    "sha": canonical_sha,
                    "note": reason or "skipped via scheduler override",
                    "queued_at": _utc_now(),
                    "finished_at": _utc_now(),
                    "status": "skipped",
                },
            )
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
        self.refresh_models()
        return {"updated": not dry_run, "dry_run": dry_run, "config": self.config.to_dict()}

    def _execute_platform_submit(self, title: str, sha: str) -> dict[str, Any]:
        """Invoke `tools/submit-validated.py` from a clean detached git worktree."""
        repo = self.workspace / "bench" / title
        worktree = pathlib.Path("/tmp") / f"opsb-submit-{title}"
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], capture_output=True)
        added = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), sha],
            capture_output=True,
            text=True,
        )
        if added.returncode != 0:
            raise RuntimeError(f"worktree add failed for {title} {sha[:7]}: {added.stderr.strip()}")
        try:
            res = subprocess.run(
                [
                    sys.executable,
                    str(self.workspace / "tools" / "submit-validated.py"),
                    title,
                    "--sha",
                    sha,
                    "--project-dir",
                    str(worktree),
                    "--force",
                ],
                capture_output=True,
                text=True,
            )
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                capture_output=True,
            )
        if res.returncode != 0:
            raise RuntimeError(f"submit failed for {title} {sha[:7]}: {(res.stderr or res.stdout).strip()}")
        return json.loads(res.stdout.strip().splitlines()[-1])

    def _wait_and_score(self, title: str, submission_id: str) -> tuple[str, float | None]:
        sys.path.insert(0, str(self.workspace / "skills" / "problem-submit" / "scripts"))
        from ict_cli import ICTClient  # type: ignore

        client = ICTClient()
        if not client.load_session():
            raise RuntimeError("Run tools/cj login first")
        deadline = time.monotonic() + 1800
        status = "Running"
        while time.monotonic() < deadline:
            status = client.get_submission(submission_id).get("status", "Running")
            if status not in ("Running", "Queued", "Pending"):
                break
            time.sleep(20)
        pmeta = self.estimator.problems_meta[title]
        base = f"/api/problems/{pmeta['problem_id']}/ranking"
        score = None
        for page in range(1, 8):
            payload = client.session.get(
                client.BASE_URL + base, params={"page": page, "size": 100}, timeout=30
            ).json()
            row = next((e for e in payload.get("rows", []) if e.get("user_id") == client.user_id), None)
            if row:
                score = float(row["score"])
                break
        return status, score

    def drain(self, once: bool = True, interval_sec: int = 180, dry_run: bool = False) -> dict[str, Any]:
        """Single-consumer WSPT priority drain loop over `results/submit-queue.jsonl`."""
        while True:
            self.refresh_models()
            q_path = self.workspace / "results" / "submit-queue.jsonl"
            out_path = self.workspace / "results" / "submit-queue-outcome.jsonl"
            lock_path = self.workspace / "results" / ".submit-queue.lock"

            outcomes = self.estimator.outcomes
            done_sha = {
                (r["title"], r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha")))
                for r in outcomes
            }
            done_exact = {
                (
                    r["title"],
                    r.get("canonical_sha") or resolve_sha(self.workspace, r["title"], r.get("sha")),
                    r.get("queued_at"),
                )
                for r in outcomes
            }

            raw_pending: list[dict[str, Any]] = []
            for rec in _read_jsonl(q_path):
                t = rec.get("title", "")
                csha = resolve_sha(self.workspace, t, rec.get("sha"))
                q_at = rec.get("queued_at")
                if (t, csha, q_at) in done_exact:
                    continue
                if not rec.get("force") and (t, csha) in done_sha:
                    continue
                raw_pending.append({**rec, "canonical_sha": csha})

            approved_plan: list[tuple[dict[str, Any], Any]] = []
            held_plan: list[dict[str, Any]] = []
            for rec in raw_pending:
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

            # Order approved submissions by CRITICAL_RESTORE -> difficulty * delta_expected -> FIFO
            approved_plan.sort(
                key=lambda pv: (pv[1].priority_score, -(time.time())),
                reverse=True,
            )

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

            lock_path.parent.mkdir(parents=True, exist_ok=True)
            submitted_results: list[dict[str, Any]] = []
            with lock_path.open("a") as lk:
                try:
                    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"action": "drain", "locked_by_other": True, "submitted": []}

                for idx, (rec, verdict) in enumerate(approved_plan):
                    self.refresh_models()
                    quota_now = self.gate.get_quota_status()
                    if quota_now.remaining_today <= 0:
                        break
                    if quota_now.rate_wait_sec > 0 or idx > 0:
                        wait_s = quota_now.rate_wait_sec if idx == 0 else self.config.rate_limit_gap_sec
                        if wait_s > 0:
                            time.sleep(wait_s)

                    t, csha = verdict.title, verdict.sha
                    receipt = None
                    err = None
                    for _attempt in range(3):
                        try:
                            receipt = self._execute_platform_submit(t, csha)
                            break
                        except RuntimeError as exc:
                            err = exc
                            if "timed out" not in str(exc) and "ReadTimeout" not in str(exc):
                                break
                            time.sleep(30)

                    if receipt is None:
                        out_rec = {
                            **rec,
                            "sha": csha,
                            "finished_at": _utc_now(),
                            "status": "rejected",
                            "detail": str(err)[:400],
                        }
                        _append_jsonl(out_path, out_rec)
                        submitted_results.append(out_rec)
                        if err and "daily submission limit" in str(err):
                            break
                        continue

                    if receipt.get("existing"):
                        out_rec = {
                            **rec,
                            "sha": csha,
                            "finished_at": _utc_now(),
                            "status": "duplicate",
                            "submission_id": receipt["submission_id"],
                        }
                        _append_jsonl(out_path, out_rec)
                        submitted_results.append(out_rec)
                        continue

                    sub_id = receipt["submission_id"]
                    st_str, sc_val = self._wait_and_score(t, sub_id)
                    out_rec = {
                        **rec,
                        "sha": csha,
                        "finished_at": _utc_now(),
                        "status": st_str,
                        "submission_id": sub_id,
                        "score": sc_val,
                        "submitted_at": receipt.get("submitted_at") or _utc_now(),
                    }
                    _append_jsonl(out_path, out_rec)
                    submitted_results.append(out_rec)
                    # Closed-loop auto-recalibration upon receiving new online score or WA/TLE/RE!
                    try:
                        self.refresh_models()
                        self.calibrator.calibrate_problem(t, persist=True)
                    except Exception:
                        pass

            if once:
                return {
                    "action": "drain",
                    "dry_run": False,
                    "submitted_count": len(submitted_results),
                    "submitted": submitted_results,
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

    def diagnose(self, title: str) -> dict[str, Any]:
        """Return residual diagnostics, phantom-weight adjustments, unpaired buckets, and ROI ranking."""
        self.refresh_models()
        if title not in self.estimator.problems_meta:
            raise ValueError(f"Unknown problem title: {title}")
        return self.calibrator.diagnose_problem(title)

    def verify_escape(self, title: str, bad_sha: str, good_sha: str) -> dict[str, Any]:
        """Verify regression test between escaped `bad_sha` and passing `good_sha`, clearing `ESCAPE_LOCK`."""
        self.refresh_models()
        if title not in self.estimator.problems_meta:
            raise ValueError(f"Unknown problem title: {title}")
        res = self.calibrator.verify_escape(title=title, bad_sha=bad_sha, good_sha=good_sha)
        if res.get("ok"):
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
        abs_errors: list[float] = []
        sq_errors: list[float] = []

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

            sc = float(rec["score"]) if isinstance(rec.get("score"), (int, float)) else None
            is_live_shot = st not in ("rejected", "skipped", "duplicate")
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
            "daily_cap_exhaustion_prevented": slots_saved >= 5,
            "estimator_accuracy": {
                "evaluated_pass_pairs": len(abs_errors),
                "mae_pts": mae,
                "rmse_pts": rmse,
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
        help="Contest workspace root (default: /Users/winbeau/Competitions/ops-bench)",
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
    p_tst.add_argument("--sha", default=None, help="Commit SHA (defaults to repo HEAD)")
    p_tst.add_argument("--fast", action="store_true", help="Route through Q0 fast-lane")
    p_tst.add_argument("--build-only", action="store_true", help="Run Phase A 32-core CPU compile only (0.0s NPU lock)")
    p_tst.add_argument("--dry-run", action="store_true", help="Output 3-phase execution plan without running")

    p_cal = sub.add_parser("calibrate", parents=[common], help="Fit Simplex MAP calibration & write calibration.json")
    p_cal.add_argument("--title", default=None, help="Optional single problem title (defaults to all 10)")

    p_bst = sub.add_parser("bench-status", parents=[common], help="Inspect stratified calibration & 6x5 coverage grid")
    p_bst.add_argument("--title", default=None, help="Optional single problem title")

    p_dia = sub.add_parser("diagnose", parents=[common], help="Inspect residual diagnostics, phantom weights, and bucket ROI")
    p_dia.add_argument("--title", required=True, help="Operator problem title")

    p_ves = sub.add_parser("verify-escape", parents=[common], help="Verify escape regression test & clear ESCAPE_LOCK")
    p_ves.add_argument("--title", required=True, help="Operator problem title")
    p_ves.add_argument("--bad-sha", required=True, help="Commit SHA that escaped local validation and failed online")
    p_ves.add_argument("--good-sha", required=True, help="Fixed commit SHA with regression reproducer passing Q1")

    sub.add_parser("retrospect", parents=[common], help="Replay historical outcomes against 4-stage gate")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
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
            payload = engine.npu_scheduler.schedule_test(
                title=args.title,
                sha=args.sha,
                fast=args.fast,
                build_only=args.build_only,
                dry_run=args.dry_run,
                stream_stdout=not args.dry_run,
            )
            if not args.dry_run:
                res = payload.get("result") or {}
                print(json.dumps(res, ensure_ascii=False))
                return int(res.get("exit_code", 0))
        elif args.command == "calibrate":
            payload = engine.calibrate(title=args.title)
        elif args.command == "bench-status":
            payload = engine.bench_status(title=args.title)
        elif args.command == "diagnose":
            payload = engine.diagnose(title=args.title)
        elif args.command == "verify-escape":
            payload = engine.verify_escape(title=args.title, bad_sha=args.bad_sha, good_sha=args.good_sha)
            if not payload.get("ok", True):
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 1
        elif args.command == "retrospect":
            payload = engine.retrospect()
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    except Exception as exc:
        err_payload = {"ok": False, "command": args.command, "error": str(exc)}
        print(json.dumps(err_payload, indent=2, ensure_ascii=False))
        return 1

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
