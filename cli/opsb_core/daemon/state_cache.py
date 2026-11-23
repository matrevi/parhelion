"""Incremental cold/hot workspace state cache for `opsb daemon`.

- Cold Fingerprint (`cold_etag`): SHA-256 over `(mtime_ns, size)` of contest/queue/git/calibration/auth files (< 0.5ms).
  Recomputes full `SupervisorSnapshot` in a background thread ONLY when fingerprint changes (or invalidated).
- Hot Frame (< 2ms, ~3KB): merges in-memory NPU telemetry stream with cached/live pipeline & scheduler state.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..config import CONFIG_DIR, DEFAULT_CONFIG, SESSIONS_DIR, ConfigRepository, _resolve_parhelion_home, parse_iso, read_json, read_jsonl, utc_now
from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
from ..services.auth_service import get_auth_status, list_judge_accounts
from ..services.backfill_status_service import probe_backfill_compact
from ..services.board_refresh_service import refresh_if_stale_async
from ..services.contest_service import resolve_workspace_meta
from ..services.inflight_io import inflight_pid, read_inflight, reap_inflight_if_stale
from ..services.judge_sync import (
    TERMINAL_JUDGE_STATUSES,
    TERMINAL_OUTCOME_STATUSES,
    _classify_remote_phase,
    _inspect_local_worker,
    _pid_alive,
    _query_remote_submission_cached,
)
from ..services.queue_service import _probe_queue_lock, compute_workspace_snapshot
from ..services.quota_helpers import compute_effective_closing_reserve
from ..services.skills_service import get_skills_status
from ..services.submit_runner import MIN_RATE_GAP_SEC, inflight_file_for
from ..services.sync_reconciler import collapse_reconciled_outcomes, reconcile_sync_errors_async


def _stat_sig(path: Path) -> tuple[int, int]:
    try:
        st = os.stat(path)
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (0, 0)


def _strip_volatile_for_digest(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a canonical copy of `payload` excluding purely tick-volatile counters for `cold_etag`."""
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k == "sampled_at":
            continue
        if k == "problems" and isinstance(v, list):
            out[k] = [
                {pk: pv for pk, pv in p.items() if pk != "head_age_min"} if isinstance(p, dict) else p
                for p in v
            ]
        elif k == "scheduler" and isinstance(v, dict):
            s_copy: dict[str, Any] = {}
            for sk, sv in v.items():
                if sk == "cooldown_remaining_sec":
                    continue
                if sk == "pipeline" and isinstance(sv, dict):
                    p_copy = {
                        pk: pv
                        for pk, pv in sv.items()
                        if pk not in ("cooldown_remaining_sec", "judge_elapsed_sec")
                    }
                    if isinstance(p_copy.get("active_submissions"), list):
                        p_copy["active_submissions"] = [
                            {ak: av for ak, av in a.items() if ak != "elapsed_sec"} if isinstance(a, dict) else a
                            for a in p_copy["active_submissions"]
                        ]
                    if isinstance(p_copy.get("account_slots"), list):
                        p_copy["account_slots"] = [
                            {ak: av for ak, av in a.items() if ak != "cooldown_remaining_sec"} if isinstance(a, dict) else a
                            for a in p_copy["account_slots"]
                        ]
                    s_copy[sk] = p_copy
                elif sk == "board_freshness" and isinstance(sv, dict):
                    s_copy[sk] = {
                        bk: bv
                        for bk, bv in sv.items()
                        if bk not in ("refresh_age_hours", "hours_since_last_submit")
                    }
                else:
                    s_copy[sk] = sv
            out[k] = s_copy
        elif k == "submission_queue" and isinstance(v, list):
            out[k] = [
                {qk: qv for qk, qv in q.items() if qk != "elapsed_sec"} if isinstance(q, dict) else q
                for q in v
            ]
        else:
            out[k] = v
    return out


class _WorkspaceStateEntry:
    def __init__(self, workspace: Path, on_cold_updated: Callable[[], None] | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self._on_cold_updated = on_cold_updated
        self._lock = threading.Lock()
        self._hot_cache_lock = threading.Lock()

        self.cold_etag: str = ""
        self.cold_fingerprint: str = ""
        self.cold_payload: dict[str, Any] | None = None
        self.cold_json_bytes: bytes | None = None
        self.cold_recompute_ms: float = 0.0
        self.cold_computed_at: float = 0.0
        self.problems_by_title: dict[str, dict[str, Any]] = {}
        self.invalidated_gen: int = 0
        self._recomputing: bool = False
        self._recomputing_count: int = 0
        self._recompute_seq: int = 0
        self._applied_seq: int = 0
        self._last_good_bridge: dict[str, Any] | None = None

        # Cached git HEAD ref paths per problem so fingerprint stat avoids opening .git/HEAD if unchanged
        self._problem_paths: list[str] | None = None

        # Hot incremental caches
        self._outcome_sig: tuple[int, int] | None = None
        self._outcome_rows: list[dict[str, Any]] = []
        self._outcome_sids: set[str] = set()

        self._receipt_dir_sigs: dict[str, int] = {}
        self._receipt_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._cached_receipts: list[dict[str, Any]] = []

        self._remote_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._remote_fetching: set[str] = set()
        self._remote_lock = threading.Lock()

        self._cached_hot_base: dict[str, Any] | None = None
        self._cached_hot_base_ts: float = 0.0
        self._cached_hot_sig: str = ""
        self._last_fp_check: float = 0.0

    def _discover_problem_paths(self) -> list[str]:
        if self._problem_paths is not None:
            return self._problem_paths
        meta = resolve_workspace_meta(self.workspace)
        self._problem_paths = [str(p.get("path") or f"bench/{p['title']}") for p in meta.get("problems", [])]
        return self._problem_paths

    def compute_cold_fingerprint(self) -> str:
        from ..config import breakthrough_dir, workspace_state_dir

        ws = self.workspace
        ws_state = workspace_state_dir(ws)
        bt_root = breakthrough_dir()
        h = hashlib.sha256()

        def _feed(tag: str, p: Path) -> None:
            m, sz = _stat_sig(p)
            h.update(f"{tag}:{m}:{sz};".encode("utf-8"))

        _feed("snap", ws / "results" / "score-snapshot.json")
        _feed("sq", ws / "results" / "submit-queue.jsonl")
        _feed("out", ws / "results" / "submit-queue-outcome.jsonl")
        _feed("tbest", ws / "results" / "scoring-evidence" / "live-board-tbest.json")
        _feed("rank", ws / "results" / "scoring-evidence" / "team-ranking.json")
        _feed("board", ws / "results" / "scoring-evidence" / "board-summary.json")
        _feed("mtable", ws / "results" / "scoring-evidence" / "my-score-table.json")
        _feed("esc", ws / "results" / "verified-escapes.json")
        _feed("esc2", ws / "results" / "escape-verifications.json")
        _feed("esc3", ws_state / "verified_escapes.json")
        _feed("bt", bt_root / "index.json")
        _feed("sstate", ws_state / "scheduler_state.json")
        _feed("bledger", ws_state / "backfill_ledger.jsonl")
        _feed("bcfg", ws_state / "backfill_config.json")
        _feed("cfg", CONFIG_DIR / "config.json")
        _feed("accts", CONFIG_DIR / "cj_accounts.json")
        _feed("psess", SESSIONS_DIR)
        _feed("cj", Path.home() / ".judge" / "session.json")
        _feed("cjs", Path.home() / ".judge" / "sessions")

        # Include live per-account inflight trackers so clicking submit immediately refreshes cold snapshot.
        ph = _resolve_parhelion_home()
        try:
            for inf_p in sorted(ph.glob("inflight*.json")):
                _feed(f"inf:{inf_p.name}", inf_p)
        except OSError:
            pass

        # Include submission-receipt.json stats directly (writing a receipt into an existing <sha> dir
        # does NOT bump `results/<title>` mtime, so scanning <sha> dirs prevents stale snapshots).
        results_dir = ws / "results"
        if results_dir.is_dir():
            rec_sigs: list[str] = []
            try:
                for p_entry in os.scandir(results_dir):
                    if not p_entry.is_dir():
                        continue
                    try:
                        for sha_entry in os.scandir(p_entry.path):
                            if not sha_entry.is_dir():
                                continue
                            rec_p = Path(sha_entry.path) / "submission-receipt.json"
                            rm, rsz = _stat_sig(rec_p)
                            if rm or rsz:
                                rec_sigs.append(f"{p_entry.name}/{sha_entry.name}:{rm}:{rsz}")
                    except OSError:
                        continue
            except OSError:
                pass
            if rec_sigs:
                rec_sigs.sort()
                h.update(("recs:" + ";".join(rec_sigs) + ";").encode("utf-8"))

        for rel_p in self._discover_problem_paths():
            title = Path(rel_p).name
            _feed(f"cal:{title}", ws / "results" / title / "calibration.json")
            git_dir = ws / rel_p / ".git"
            _feed(f"gh:{title}", git_dir / "HEAD")
            _feed(f"gl:{title}", git_dir / "logs" / "HEAD")
            try:
                head_txt = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
                if head_txt.startswith("ref: "):
                    _feed(f"gr:{title}", git_dir / head_txt[5:].strip())
            except OSError:
                pass

        with self._lock:
            gen = self.invalidated_gen
        h.update(f"gen:{gen}".encode("utf-8"))
        return h.hexdigest()[:24]

    def invalidate(self) -> None:
        with self._lock:
            self.invalidated_gen += 1
            self.cold_fingerprint = ""
            self._cached_hot_sig = ""
            self._last_fp_check = 0.0
        self._problem_paths = None

    def ensure_cold_fresh(self, *, synchronous: bool = False, max_age_sec: float = 30.0) -> None:
        now_m = time.monotonic()
        with self._lock:
            if (
                not synchronous
                and self.cold_payload is not None
                and self.cold_fingerprint
                and (now_m - self._last_fp_check) < 0.25
            ):
                return
        fp = self.compute_cold_fingerprint()
        with self._lock:
            self._last_fp_check = now_m
            age = now_m - self.cold_computed_at
            needs = (
                self.cold_payload is None
                or fp != self.cold_fingerprint
                or age >= max_age_sec
            )
            if not needs:
                return
            if synchronous:
                self._recompute_seq += 1
                my_seq = self._recompute_seq
                start_gen = self.invalidated_gen
                self._recomputing_count += 1
                self._recomputing = True
            elif self._recomputing:
                return
            else:
                self._recompute_seq += 1
                my_seq = self._recompute_seq
                start_gen = self.invalidated_gen
                self._recomputing_count += 1
                self._recomputing = True

        if synchronous:
            self._do_recompute_cold(fp, my_seq, start_gen)
        else:
            threading.Thread(
                target=self._do_recompute_cold,
                args=(fp, my_seq, start_gen),
                daemon=True,
                name="opsb-cold-recompute",
            ).start()

    def _do_recompute_cold(self, target_fp: str, seq: int = 0, start_gen: int | None = None) -> None:
        t0 = time.monotonic()
        try:
            cfg = ConfigRepository.load()
            payload = compute_workspace_snapshot(self.workspace, cfg)
            bridge = invoke_bridge(self.workspace, "status", ["--offline"], use_cache=True)
            if isinstance(bridge, dict):
                with self._lock:
                    self._last_good_bridge = bridge
                merge_bridge_enrichment(payload, bridge)
            else:
                # Transient bridge timeout/error must not wipe `candidate` cards from submission_queue.
                with self._lock:
                    fallback_bridge = self._last_good_bridge
                if isinstance(fallback_bridge, dict):
                    merge_bridge_enrichment(payload, fallback_bridge)

            ssh_host = cfg.get("npu_ssh_host") or payload["contest"].get("npu_ssh_host") or "npu"
            payload["npu"] = {"online": False, "ssh_host": ssh_host, "skipped": True, "die_locks": [], "tasks": []}
            payload["scheduler"]["backfill"] = probe_backfill_compact(self.workspace)

            contest_obj = payload.get("contest") or {}
            score_drift: float | None = None
            if contest_obj.get("team_rank_score") is not None:
                try:
                    score_drift = abs(float(contest_obj.get("weighted_score") or 0.0) - float(contest_obj["team_rank_score"]))
                except (TypeError, ValueError):
                    score_drift = None
            try:
                payload["scheduler"]["board_freshness"] = refresh_if_stale_async(
                    self.workspace, score_drift=score_drift, on_refreshed=self.invalidate
                )
            except TypeError:
                payload["scheduler"]["board_freshness"] = refresh_if_stale_async(self.workspace)

            payload["auth"] = get_auth_status(use_cache=True)
            payload["skills"] = get_skills_status()
            payload["workspaces"] = cfg.get("workspaces", [str(self.workspace)])
            payload["language"] = cfg.get("language", "zh")
            reconcile_sync_errors_async(self.workspace, on_reconciled=self.invalidate)

            # Compute a deterministic content digest excluding tick-volatile fields so periodic timer ticks
            # do not spuriously rotate `cold_etag` when nothing meaningful changed.
            digest_obj = _strip_volatile_for_digest(payload)
            digest_bytes = json.dumps(digest_obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            content_hash = hashlib.sha256(digest_bytes).hexdigest()[:16]
            etag = f"{target_fp[:12]}-{content_hash}"
            raw_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            recompute_ms = round((time.monotonic() - t0) * 1000, 2)
            probs_map = {p["title"]: p for p in payload.get("problems", []) if p.get("title")}

            changed = False
            with self._lock:
                # Monotonic commit guard: never let an older/pre-invalidation background recompute roll back
                # a newer `cold_etag` / `cold_payload`.
                superseded_seq = seq > 0 and seq < self._applied_seq
                superseded_gen = start_gen is not None and start_gen != self.invalidated_gen
                if not superseded_seq and (not superseded_gen or self.cold_payload is None):
                    changed = etag != self.cold_etag
                    self.cold_payload = payload
                    self.cold_json_bytes = raw_bytes
                    self.cold_etag = etag
                    self.cold_fingerprint = "" if superseded_gen else target_fp
                    self.cold_recompute_ms = recompute_ms
                    self.cold_computed_at = time.monotonic()
                    self.problems_by_title = probs_map
                    if seq > 0:
                        self._applied_seq = seq

            if changed and self._on_cold_updated is not None:
                try:
                    self._on_cold_updated()
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            with self._lock:
                self._recomputing_count = max(0, self._recomputing_count - 1)
                self._recomputing = self._recomputing_count > 0

    def get_snapshot_response(
        self,
        if_none_match: str | None = None,
        *,
        live_npu: dict[str, Any] | None = None,
    ) -> tuple[int, str, bytes]:
        self.ensure_cold_fresh(synchronous=(self.cold_json_bytes is None))
        with self._lock:
            etag = self.cold_etag
            data = self.cold_json_bytes or b"{}"
            payload = self.cold_payload

        if if_none_match:
            cleaned = if_none_match.strip()
            if cleaned.startswith("W/"):
                cleaned = cleaned[2:].strip()
            cleaned = cleaned.strip('"')
            if cleaned == etag and etag:
                return 304, etag, b""
        if live_npu is not None and payload is not None and (live_npu.get("online") or live_npu.get("tasks") or live_npu.get("die_locks")):
            enriched = dict(payload)
            enriched["npu"] = live_npu
            data = json.dumps(enriched, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return 200, etag, data

    def _get_outcomes_cached(self) -> tuple[list[dict[str, Any]], set[str]]:
        out_path = self.workspace / "results" / "submit-queue-outcome.jsonl"
        sig = _stat_sig(out_path)
        with self._hot_cache_lock:
            if sig != self._outcome_sig:
                rows = collapse_reconciled_outcomes(read_jsonl(out_path))
                sids = {
                    str(o.get("submission_id"))
                    for o in rows
                    if o.get("submission_id") and o.get("status") in TERMINAL_OUTCOME_STATUSES
                }
                self._outcome_rows = rows
                self._outcome_sids = sids
                self._outcome_sig = sig
            return list(self._outcome_rows), set(self._outcome_sids)

    def _get_receipts_cached(self) -> list[dict[str, Any]]:
        results_dir = self.workspace / "results"
        if not results_dir.is_dir():
            return []

        with self._hot_cache_lock:
            receipts: list[dict[str, Any]] = []
            new_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
            try:
                for p_entry in os.scandir(results_dir):
                    if not p_entry.is_dir():
                        continue
                    prob_name = p_entry.name
                    try:
                        for sha_entry in os.scandir(p_entry.path):
                            if not sha_entry.is_dir():
                                continue
                            rec_path = Path(sha_entry.path) / "submission-receipt.json"
                            sig = _stat_sig(rec_path)
                            if sig == (0, 0):
                                continue
                            key = str(rec_path)
                            cached = self._receipt_cache.get(key)
                            if cached is not None and cached[0] == sig:
                                parsed_rec = cached[1]
                            else:
                                data = read_json(rec_path)
                                if not isinstance(data, dict) or not data.get("submission_id") or not data.get("submitted_at"):
                                    continue
                                dt = parse_iso(data["submitted_at"])
                                if not dt:
                                    continue
                                parsed_rec = {
                                    "title": prob_name,
                                    "sha": data.get("validated_commit") or sha_entry.name,
                                    "submission_id": str(data["submission_id"]),
                                    "account_id": str(data.get("account_id") or "primary"),
                                    "user_id": str(data.get("user_id") or ""),
                                    "submitted_dt": dt,
                                    "submitted_epoch": dt.timestamp(),
                                }
                            new_cache[key] = (sig, parsed_rec)
                            receipts.append(parsed_rec)
                    except OSError:
                        continue
            except OSError:
                return list(self._cached_receipts)

            receipts.sort(key=lambda x: (x["submitted_epoch"], x["title"], x["submission_id"]), reverse=True)
            self._receipt_cache = new_cache
            self._cached_receipts = receipts
            return list(receipts)

    def _get_remote_info_nonblocking(self, submission_id: str, session_file: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._remote_lock:
            cached = self._remote_info_cache.get(submission_id)
            if cached is not None and (now - cached[0]) < 3.0:
                return cached[1]
            if submission_id in self._remote_fetching:
                return cached[1] if cached is not None else None
            self._remote_fetching.add(submission_id)

        def _fetch() -> None:
            try:
                info = _query_remote_submission_cached(self.workspace, submission_id, session_file=session_file)
                if isinstance(info, dict):
                    with self._remote_lock:
                        self._remote_info_cache[submission_id] = (time.monotonic(), info)
            finally:
                with self._remote_lock:
                    self._remote_fetching.discard(submission_id)

        threading.Thread(target=_fetch, daemon=True, name=f"opsb-remote-judge-{submission_id[:8]}").start()
        return cached[1] if cached is not None else None

    def build_hot_pipeline_and_scheduler(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build `pipeline` + `scheduler_hot` in < 1.5 ms without blocking on network/subprocesses."""
        import datetime as _dt

        cfg = ConfigRepository.load()
        sched_cfg = cfg.get("scheduler", DEFAULT_CONFIG["scheduler"])
        gap_sec = int(sched_cfg.get("rate_limit_gap_sec", MIN_RATE_GAP_SEC))
        daily_limit = int(sched_cfg.get("daily_quota_limit", 50))
        closing_reserve = int(sched_cfg.get("closing_reserve", 12))
        noise_floor = float(sched_cfg.get("noise_floor_delta", 2.7))
        bt_threshold = float(sched_cfg.get("breakthrough_threshold", 5.0))
        auto_consumer = bool(sched_cfg.get("auto_consumer_enabled", False))

        now = utc_now()
        now_epoch = now.timestamp()
        outcome_rows, outcome_sids = self._get_outcomes_cached()
        receipts = self._get_receipts_cached()
        accounts = list_judge_accounts()
        uid_to_aid = {str(a.get("user_id")): str(a["id"]) for a in accounts if a.get("user_id")}

        bj_tz = _dt.timezone(_dt.timedelta(hours=8))
        bj_day_start_ep = now.astimezone(bj_tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        cutoff_24h = now_epoch - 86400

        last_epoch_by_aid: dict[str, float] = {str(a["id"]): 0.0 for a in accounts}
        today_sids_by_aid: dict[str, set[str]] = {str(a["id"]): set() for a in accounts}
        latest_sub_epoch = 0.0

        for r in receipts:
            ts = float(r["submitted_epoch"])
            latest_sub_epoch = max(latest_sub_epoch, ts)
            aid = str(r.get("account_id") or "")
            if not aid or aid not in last_epoch_by_aid:
                aid = uid_to_aid.get(str(r.get("user_id") or ""), "primary")
            if aid in last_epoch_by_aid:
                last_epoch_by_aid[aid] = max(last_epoch_by_aid[aid], ts)
                sid = str(r.get("submission_id") or "")
                if sid and ts >= bj_day_start_ep:
                    today_sids_by_aid[aid].add(sid)

        used_24h_fallback = 0
        for o in outcome_rows:
            if o.get("status") not in ("rejected", "skipped", "duplicate") and o.get("submission_id"):
                dt = parse_iso(o.get("submitted_at") or o.get("finished_at"))
                if not dt:
                    continue
                ts = dt.timestamp()
                latest_sub_epoch = max(latest_sub_epoch, ts)
                if ts >= cutoff_24h:
                    used_24h_fallback += 1
                aid = str(o.get("account_id") or "primary")
                if aid not in last_epoch_by_aid:
                    aid = "primary"
                last_epoch_by_aid[aid] = max(last_epoch_by_aid[aid], ts)
                sid = str(o.get("submission_id") or "")
                if sid and ts >= bj_day_start_ep:
                    today_sids_by_aid[aid].add(sid)

        account_slots: list[dict[str, Any]] = []
        for a in accounts:
            aid = str(a["id"])
            last_ep = last_epoch_by_aid.get(aid, 0.0)
            ends_ep = (last_ep + gap_sec) if last_ep > 0 else 0.0
            rem = max(0, int(round(ends_ep - now_epoch)))
            inf_path = inflight_file_for(aid)
            phase = "idle"
            active_title = None
            active_sha = None
            if inf_path.is_file():
                inf = read_inflight(inf_path)
                pid = inflight_pid(inf)
                if inf is not None and pid > 0 and _pid_alive(pid):
                    phase = str(inf.get("phase") or "packaging")
                    active_title = inf.get("title")
                    active_sha = inf.get("sha")
                else:
                    reap_inflight_if_stale(inf_path, inf)
            used_today = len(today_sids_by_aid.get(aid, set()))
            rem_today = max(0, daily_limit - used_today)
            account_slots.append(
                {
                    "id": aid,
                    "label": str(a.get("label") or aid),
                    "user_id": a.get("user_id"),
                    "session_file": str(a.get("session_file") or ""),
                    "logged_in": bool(a.get("logged_in")),
                    "enabled": bool(a.get("enabled", True)),
                    "cooldown_ends_epoch": round(ends_ep, 2),
                    "cooldown_remaining_sec": rem,
                    "used_today": used_today,
                    "daily_limit": daily_limit,
                    "remaining_today": rem_today,
                    "quota_exhausted": rem_today <= 0,
                    "phase": phase,
                    "active_title": active_title,
                    "active_sha": active_sha,
                }
            )

        slot_by_id = {str(s["id"]): s for s in account_slots}
        slot_by_uid = {str(s.get("user_id")): s for s in account_slots if s.get("user_id")}

        active_remotes: list[dict[str, Any]] = []
        finishing_syncs: list[dict[str, Any]] = []
        server_offset_sec = 0.0
        for r in receipts:
            age_sec = now_epoch - float(r["submitted_epoch"])
            if age_sec > 2100:
                break
            if r["submission_id"] not in outcome_sids:
                r_slot = slot_by_id.get(r.get("account_id") or "") or slot_by_uid.get(r.get("user_id") or "") or slot_by_id.get("primary") or {}
                r_sf = str(r_slot.get("session_file") or "")
                r_aid = str(r_slot.get("id") or r.get("account_id") or "primary")
                r_lbl = str(r_slot.get("label") or ("主账号" if r_aid == "primary" else r_aid))
                remote_info = self._get_remote_info_nonblocking(r["submission_id"], r_sf)
                if remote_info:
                    server_offset_sec = float(remote_info.get("server_offset_sec") or 0.0)
                    srv_create_dt = parse_iso(remote_info.get("create_time"))
                    adj_create_epoch = (srv_create_dt.timestamp() - server_offset_sec) if srv_create_dt else float(r["submitted_epoch"])
                    latest_sub_epoch = max(latest_sub_epoch, adj_create_epoch)
                    r_status = str(remote_info.get("status") or "Running")
                    if r_status not in TERMINAL_JUDGE_STATUSES:
                        active_remotes.append(
                            {
                                "id": f"remote-{r['submission_id']}",
                                "title": r["title"],
                                "sha": r["sha"],
                                "short_sha": r["sha"][:7],
                                "submission_id": r["submission_id"],
                                "judge_id": remote_info.get("judge_id"),
                                "remote_status": r_status,
                                "phase": _classify_remote_phase(r_status),
                                "started_epoch": round(adj_create_epoch, 2),
                                "elapsed_sec": max(0, int(now_epoch - adj_create_epoch)),
                                "account_id": r_aid,
                                "account_label": r_lbl,
                            }
                        )
                    else:
                        finishing_syncs.append(
                            {
                                "id": f"sync-{r['submission_id']}",
                                "title": r["title"],
                                "sha": r["sha"],
                                "short_sha": r["sha"][:7],
                                "submission_id": r["submission_id"],
                                "judge_id": remote_info.get("judge_id"),
                                "remote_status": r_status,
                                "phase": "fetching_score",
                                "started_epoch": round(adj_create_epoch, 2),
                                "elapsed_sec": max(0, int(now_epoch - adj_create_epoch)),
                                "account_id": r_aid,
                                "account_label": r_lbl,
                            }
                        )
                else:
                    sub_ep = float(r["submitted_epoch"])
                    active_remotes.append(
                        {
                            "id": f"remote-{r['submission_id']}",
                            "title": r["title"],
                            "sha": r["sha"],
                            "short_sha": r["sha"][:7],
                            "submission_id": r["submission_id"],
                            "judge_id": None,
                            "remote_status": "Uploading/Syncing",
                            "phase": "remote_queued",
                            "started_epoch": round(sub_ep, 2),
                            "elapsed_sec": max(0, int(now_epoch - sub_ep)),
                            "account_id": r_aid,
                            "account_label": r_lbl,
                        }
                    )

        if finishing_syncs:
            reconcile_sync_errors_async(self.workspace, on_reconciled=self.invalidate)
        finishing_score_sync = finishing_syncs[0] if finishing_syncs else None

        queue_lock_held = _probe_queue_lock(self.workspace / "results" / ".submit-queue.lock")
        cooldown_ends_epoch = (latest_sub_epoch + gap_sec) if latest_sub_epoch > 0 else 0.0
        cooldown_rem = max(0, int(round(cooldown_ends_epoch - now_epoch)))

        eligible_slots = [s for s in account_slots if s.get("enabled") and s.get("logged_in")]
        active_quota_slots = [s for s in eligible_slots if not s.get("quota_exhausted")]
        if active_quota_slots:
            avail_slots = [s for s in active_quota_slots if s.get("phase") not in ("packaging", "uploading")] or active_quota_slots
            best_slot = min(avail_slots, key=lambda x: int(x.get("cooldown_remaining_sec") or 0))
            cooldown_rem = int(best_slot.get("cooldown_remaining_sec") or 0)
            cooldown_ends_epoch = float(best_slot.get("cooldown_ends_epoch") or 0.0)
        elif eligible_slots:
            cooldown_rem = 0
            cooldown_ends_epoch = 0.0

        local_worker = _inspect_local_worker(queue_lock_held)
        active_submissions: list[dict[str, Any]] = []
        seen_active_pairs: set[tuple[str, str, str]] = set()
        for s in account_slots:
            if s.get("phase") in ("waiting_cooldown", "packaging", "uploading") and s.get("active_title"):
                a_sha = str(s.get("active_sha") or "")
                k = (str(s["active_title"]), a_sha[:7].lower(), str(s["id"]))
                if k not in seen_active_pairs:
                    seen_active_pairs.add(k)
                    st_ep = float(local_worker["started_epoch"]) if (local_worker and s["id"] == "primary") else now_epoch
                    active_submissions.append(
                        {
                            "id": f"act-{s['id']}-{s['active_title']}-{a_sha[:7].lower()}",
                            "title": str(s["active_title"]),
                            "sha": a_sha,
                            "short_sha": a_sha[:7],
                            "phase": str(s["phase"]),
                            "account_id": str(s["id"]),
                            "account_label": str(s["label"]),
                            "remote_submission_id": None,
                            "remote_judge_id": None,
                            "remote_status": None,
                            "started_epoch": round(st_ep, 2),
                            "elapsed_sec": max(0, int(now_epoch - st_ep)),
                        }
                    )
        if local_worker is not None and local_worker.get("title"):
            lw_title = str(local_worker.get("title") or "")
            lw_sha = str(local_worker.get("sha") or "")
            lw_s7 = lw_sha[:7]
            lw_k = (lw_title, lw_s7.lower(), "primary")
            if lw_k not in seen_active_pairs and not any(x["title"] == lw_title and x["short_sha"].lower() == lw_s7.lower() for x in active_submissions):
                seen_active_pairs.add(lw_k)
                st_ep = float(local_worker.get("started_epoch") or now_epoch)
                raw_p = str(local_worker.get("phase") or "packaging")
                mapped_p = "packaging" if raw_p in ("preparing", "consumer_locked") else raw_p
                active_submissions.append(
                    {
                        "id": f"act-primary-{lw_title}-{lw_s7.lower()}",
                        "title": lw_title,
                        "sha": lw_sha,
                        "short_sha": lw_s7,
                        "phase": mapped_p,
                        "account_id": "primary",
                        "account_label": str((slot_by_id.get("primary") or {}).get("label") or "主账号"),
                        "remote_submission_id": None,
                        "remote_judge_id": None,
                        "remote_status": None,
                        "started_epoch": round(st_ep, 2),
                        "elapsed_sec": max(0, int(now_epoch - st_ep)),
                    }
                )
        for ar in active_remotes:
            k = (ar["title"], ar["short_sha"].lower(), ar["account_id"])
            if k not in seen_active_pairs:
                seen_active_pairs.add(k)
                active_submissions.append(
                    {
                        "id": f"act-{ar['account_id']}-{ar['title']}-{ar['short_sha'].lower()}",
                        "title": ar["title"],
                        "sha": ar["sha"],
                        "short_sha": ar["short_sha"],
                        "phase": ar["phase"],
                        "account_id": ar["account_id"],
                        "account_label": ar["account_label"],
                        "remote_submission_id": ar["submission_id"],
                        "remote_judge_id": ar["judge_id"],
                        "remote_status": ar["remote_status"],
                        "started_epoch": ar["started_epoch"],
                        "elapsed_sec": ar["elapsed_sec"],
                    }
                )
        for fs in finishing_syncs:
            k = (fs["title"], fs["short_sha"].lower(), fs["account_id"])
            if k not in seen_active_pairs:
                seen_active_pairs.add(k)
                active_submissions.append(
                    {
                        "id": f"act-{fs['account_id']}-{fs['title']}-{fs['short_sha'].lower()}",
                        "title": fs["title"],
                        "sha": fs["sha"],
                        "short_sha": fs["short_sha"],
                        "phase": "fetching_score",
                        "account_id": fs["account_id"],
                        "account_label": fs["account_label"],
                        "remote_submission_id": fs["submission_id"],
                        "remote_judge_id": fs["judge_id"],
                        "remote_status": fs["remote_status"],
                        "started_epoch": fs["started_epoch"],
                        "elapsed_sec": fs["elapsed_sec"],
                    }
                )

        inflight_keys = sorted({f"{x['title']}|{x['short_sha']}" for x in active_submissions if x.get("title") and x.get("short_sha")})
        if local_worker is None:
            for s in eligible_slots:
                if s.get("phase") in ("waiting_cooldown", "packaging", "uploading"):
                    local_worker = {
                        "title": s.get("active_title") or "",
                        "sha": s.get("active_sha") or "",
                        "phase": s.get("phase") or "packaging",
                        "started_epoch": now_epoch,
                    }
                    break

        active_remote = active_remotes[0] if active_remotes else None
        has_other_idle_ready = any(
            s.get("phase") == "idle" and int(s.get("cooldown_remaining_sec") or 0) == 0
            for s in active_quota_slots
        )
        local_busy = (
            False
            if (len(active_quota_slots) >= 1 and len(eligible_slots) >= 2 and has_other_idle_ready)
            else (
                bool(local_worker is not None and local_worker.get("phase") in (
                    "waiting_cooldown", "packaging", "preparing", "uploading", "consumer_locked"
                ))
                or bool(queue_lock_held and active_remote is None and finishing_score_sync is None)
            )
        )
        all_exhausted = bool(eligible_slots) and not active_quota_slots
        can_submit = (not all_exhausted) and (not local_busy) and (cooldown_rem == 0)

        public_slots = [
            {
                "id": s["id"],
                "label": s["label"],
                "logged_in": s["logged_in"],
                "enabled": s["enabled"],
                "cooldown_remaining_sec": s["cooldown_remaining_sec"],
                "cooldown_ends_epoch": s["cooldown_ends_epoch"],
                "used_today": int(s.get("used_today") or 0),
                "daily_limit": int(s.get("daily_limit") or 50),
                "remaining_today": int(s.get("remaining_today") or 0),
                "quota_exhausted": bool(s.get("quota_exhausted")),
                "phase": s["phase"],
                "active_title": s["active_title"],
            }
            for s in account_slots
        ]

        eff_closing_reserve = (
            compute_effective_closing_reserve(list(self.problems_by_title.values()), closing_reserve, noise_floor)
            if self.problems_by_title
            else closing_reserve
        )
        if eligible_slots:
            team_daily_limit = sum(int(s.get("daily_limit") or daily_limit) for s in eligible_slots)
            team_used_today = sum(int(s.get("used_today") or 0) for s in eligible_slots)
            team_remaining = sum(max(0, int(s.get("daily_limit") or daily_limit) - int(s.get("used_today") or 0)) for s in eligible_slots)
            avail_shots = max(0, team_remaining - eff_closing_reserve)
        else:
            team_daily_limit = daily_limit
            team_used_today = used_24h_fallback
            avail_shots = max(0, daily_limit - used_24h_fallback - eff_closing_reserve)

        base_common = {
            "local_busy": local_busy,
            "can_submit": can_submit,
            "cooldown_ends_epoch": round(cooldown_ends_epoch, 2),
            "cooldown_remaining_sec": cooldown_rem,
            "rate_limit_gap_sec": gap_sec,
            "server_clock_offset_sec": server_offset_sec,
            "account_slots": public_slots,
            "active_submissions": active_submissions,
            "inflight_keys": inflight_keys,
        }

        if local_worker is not None and local_worker.get("phase") in (
            "waiting_cooldown", "packaging", "preparing", "uploading"
        ):
            w_title = local_worker.get("title")
            w_sha = local_worker.get("sha") or ""
            raw_p = local_worker["phase"]
            mapped_phase = "packaging" if raw_p == "preparing" else raw_p
            st_ep = float(local_worker["started_epoch"])
            pipeline = {
                **base_common,
                "is_busy": True,
                "phase": mapped_phase,
                "active_title": w_title,
                "active_sha": w_sha,
                "active_short_sha": w_sha[:7] if w_sha else "",
                "remote_submission_id": None,
                "remote_judge_id": None,
                "remote_status": None,
                "judge_started_epoch": round(st_ep, 2),
                "judge_elapsed_sec": max(0, int(now_epoch - st_ep)),
            }
        elif active_remote is not None:
            pipeline = {
                **base_common,
                "is_busy": True,
                "phase": active_remote["phase"],
                "active_title": active_remote["title"],
                "active_sha": active_remote["sha"],
                "active_short_sha": active_remote["sha"][:7],
                "remote_submission_id": active_remote["submission_id"],
                "remote_judge_id": active_remote["judge_id"],
                "remote_status": active_remote["remote_status"],
                "judge_started_epoch": round(active_remote["started_epoch"], 2),
                "judge_elapsed_sec": max(0, int(now_epoch - active_remote["started_epoch"])),
            }
        elif finishing_score_sync is not None:
            pipeline = {
                **base_common,
                "is_busy": True,
                "phase": "fetching_score",
                "active_title": finishing_score_sync["title"],
                "active_sha": finishing_score_sync["sha"],
                "active_short_sha": finishing_score_sync["sha"][:7],
                "remote_submission_id": finishing_score_sync["submission_id"],
                "remote_judge_id": finishing_score_sync["judge_id"],
                "remote_status": finishing_score_sync["remote_status"],
                "judge_started_epoch": round(finishing_score_sync["started_epoch"], 2),
                "judge_elapsed_sec": max(0, int(now_epoch - finishing_score_sync["started_epoch"])),
            }
        elif local_worker is not None:
            w_title = local_worker.get("title")
            w_sha = local_worker.get("sha") or ""
            raw_p = local_worker["phase"]
            mapped_phase = ("waiting_cooldown" if cooldown_rem > 0 else "packaging") if raw_p == "consumer_locked" else "packaging"
            st_ep = float(local_worker["started_epoch"])
            pipeline = {
                **base_common,
                "is_busy": True,
                "phase": mapped_phase,
                "active_title": w_title,
                "active_sha": w_sha,
                "active_short_sha": w_sha[:7] if w_sha else "",
                "remote_submission_id": None,
                "remote_judge_id": None,
                "remote_status": None,
                "judge_started_epoch": round(st_ep, 2),
                "judge_elapsed_sec": max(0, int(now_epoch - st_ep)),
            }
        else:
            pipeline = {
                **base_common,
                "is_busy": False,
                "local_busy": False,
                "can_submit": (cooldown_rem == 0) and (not all_exhausted),
                "phase": "idle",
                "active_title": None,
                "active_sha": None,
                "active_short_sha": None,
                "remote_submission_id": None,
                "remote_judge_id": None,
                "remote_status": None,
                "judge_started_epoch": None,
                "judge_elapsed_sec": 0,
            }

        scheduler_hot = {
            "auto_consumer_enabled": auto_consumer,
            "rate_limit_gap_sec": gap_sec,
            "cooldown_remaining_sec": cooldown_rem,
            "cooldown_ends_epoch": round(cooldown_ends_epoch, 2),
            "rate_ready": cooldown_rem == 0,
            "daily_quota_limit": team_daily_limit,
            "used_today_24h": team_used_today,
            "closing_reserve": eff_closing_reserve,
            "available_normal_shots": avail_shots,
            "noise_floor_delta": noise_floor,
            "breakthrough_threshold": bt_threshold,
            "queue_lock_held": queue_lock_held,
        }
        return pipeline, scheduler_hot


class WorkspaceStateCache:
    """Multi-workspace coordinator wrapping cold snapshots and hot pipeline assembly."""

    def __init__(self, default_workspace: Path, on_cold_updated: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._default_workspace = default_workspace.expanduser().resolve()
        self._on_cold_updated = on_cold_updated
        self._entries: dict[Path, _WorkspaceStateEntry] = {}

    def _entry(self, workspace: Path | None = None) -> _WorkspaceStateEntry:
        ws = (workspace or self._default_workspace).expanduser().resolve()
        with self._lock:
            ent = self._entries.get(ws)
            if ent is None:
                ent = _WorkspaceStateEntry(ws, on_cold_updated=self._on_cold_updated)
                self._entries[ws] = ent
            return ent

    def warm(self, workspace: Path | None = None) -> None:
        ent = self._entry(workspace)
        ent.ensure_cold_fresh(synchronous=True)
        try:
            ent.build_hot_pipeline_and_scheduler()
        except Exception:
            pass

    def invalidate(self, workspace: Path | None = None) -> str:
        ent = self._entry(workspace)
        ent.invalidate()
        ent.ensure_cold_fresh(synchronous=True)
        with ent._lock:
            return ent.cold_etag

    def get_problems_by_title(self, workspace: Path | None = None) -> dict[str, dict[str, Any]]:
        ent = self._entry(workspace)
        with ent._lock:
            return dict(ent.problems_by_title)

    def get_cold_meta(self, workspace: Path | None = None) -> tuple[str, float]:
        ent = self._entry(workspace)
        ent.ensure_cold_fresh(synchronous=False)
        with ent._lock:
            return ent.cold_etag, ent.cold_recompute_ms

    def get_snapshot(
        self,
        workspace: Path | None = None,
        if_none_match: str | None = None,
        *,
        live_npu: dict[str, Any] | None = None,
    ) -> tuple[int, str, bytes]:
        return self._entry(workspace).get_snapshot_response(if_none_match=if_none_match, live_npu=live_npu)

    def build_hot_frame(
        self,
        *,
        workspace: Path | None,
        npu_payload: dict[str, Any],
        stream_stats: dict[str, Any],
        uptime_sec: float,
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        ent = self._entry(workspace)
        ent.ensure_cold_fresh(synchronous=(ent.cold_payload is None))
        pipeline, scheduler_hot = ent.build_hot_pipeline_and_scheduler()
        with ent._lock:
            cold_etag = ent.cold_etag
            cold_ms = ent.cold_recompute_ms
        hot_ms = round((time.monotonic() - t0) * 1000, 2)
        return {
            "ts": round(time.time(), 3),
            "workspace": str(ent.workspace),
            "cold_etag": cold_etag,
            "npu": npu_payload,
            "pipeline": pipeline,
            "scheduler_hot": scheduler_hot,
            "daemon_stats": {
                "stream_connected": bool(stream_stats.get("stream_connected")),
                "probe_backend": str(stream_stats.get("probe_backend") or "dcmi"),
                "dcmi_sample_ms": round(float(stream_stats.get("dcmi_sample_ms") or 0.0), 2),
                "hot_build_ms": hot_ms,
                "cold_recompute_ms": cold_ms,
                "uptime_sec": round(uptime_sec, 1),
            },
        }
