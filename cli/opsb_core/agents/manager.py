"""Daemon-hosted Multi-Harness AgentMeshManager coordinating worker lifecycles & message bus."""
from __future__ import annotations

import importlib
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import config as _cfg
from ..drivers.base import resolve_session_logs_dir, resolve_workspace_state_dir
from ..drivers.brief import (
    compose_bench_author_brief,
    compose_session_brief,
    prepare_session_worktree,
    resolve_problem_repo,
)
from ..drivers.registry import VALID_DRIVERS, doctor_drivers, get_active_driver_kind, get_driver
from ..metrics.store import UsageMonitor, orchestrator_target, worker_target
from ..services.git_fast import read_head_fast
from .adapters import BaseWorkerAdapter, create_worker_adapter, worker_runtime_dir
from .bus_store import AgentBusStore
from .judge_feedback import calibration_evidence, judge_feedback
from .ownership import claim_runner, owned_groups, reap_groups

AdapterFactory = Callable[[Path, Path, str, str, Path], BaseWorkerAdapter]
MAX_WAIT_SEC = 45.0
DEFAULT_WAIT_SEC = 25.0
STAGNATION_STREAK_THRESHOLD = 6
STRATEGIC_KINDS = frozenset({"breakthrough", "stagnation", "escape_zero", "directive", "question", "error"})
PROVIDER_FAULT = re.compile(
    r"insufficient balance|insufficient_quota|quota exceeded|billing|payment required|\b402\b|\b401\b"
    r"|unauthori[sz]ed|invalid api key|rate limit|\b429\b", re.I)
SUSPENDED_ERROR = "suspended: provider fault"
DEFAULT_CONTEXT_WINDOW = 200_000
COMPACT_PERCENT = 70
CHECKPOINT_MAX_CHARS = 4000
CHECKPOINT_TAIL = "Continue from the state card. Do not re-read files you already summarized."
SQUAD_MAX_CHARS = 4000


def compact_threshold(window: int | None = None, cfg: dict[str, Any] | None = None) -> int:
    """Absolute-token compaction threshold.

    Default: 30% of the reported model window clamped to [120k, 400k] (200k assumed when
    no window was reported). `agents.compact_tokens` overrides it, clamped to [80k, 600k].
    """
    try:
        doc = cfg if cfg is not None else _cfg.ConfigRepository.load()
    except Exception:
        doc = {}
    agents_cfg = (doc or {}).get("agents") if isinstance(doc, dict) else None
    override = agents_cfg.get("compact_tokens") if isinstance(agents_cfg, dict) else None
    if override is not None:
        try:
            return max(80_000, min(600_000, int(override)))
        except (TypeError, ValueError):
            pass
    size = int(window) if isinstance(window, int) and window > 0 else DEFAULT_CONTEXT_WINDOW
    return max(120_000, min(400_000, int(size * 0.3)))


def _git_branch_and_sha(path: Path) -> tuple[str, str]:
    if not path.is_dir():
        return "", ""
    sha, _, _ = read_head_fast(path)
    branch = ""
    head_file = path / ".git"
    try:
        if head_file.is_file():
            txt = head_file.read_text(encoding="utf-8", errors="replace").strip()
            if txt.startswith("gitdir:"):
                gd = Path(txt.split(":", 1)[1].strip())
                if not gd.is_absolute():
                    gd = (path / gd).resolve()
                hf = gd / "HEAD"
                if hf.is_file():
                    ht = hf.read_text(encoding="utf-8", errors="replace").strip()
                    if ht.startswith("ref: refs/heads/"):
                        branch = ht[len("ref: refs/heads/"):]
        elif (head_file / "HEAD").is_file():
            ht = (head_file / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            if ht.startswith("ref: refs/heads/"):
                branch = ht[len("ref: refs/heads/"):]
    except Exception:
        pass
    return branch, (sha[:7] if sha else "")


def _git_diff_stat(path: Path) -> str:
    if not path.is_dir():
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "diff", "--stat", "HEAD"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2.5,
        )
        return proc.stdout.strip()[-400:] if proc.returncode == 0 else ""
    except Exception:
        return ""


CLAUDE_DRIVER = "claude"


def claude_workers_allowed(cfg: dict[str, Any] | None = None) -> bool:
    """Admin opt-in for Claude workers.

    Policy: the orchestrator prefers its own harness (native subagents) and must not
    dispatch Claude workers unless an administrator explicitly allows it —
    `PARHELION_ALLOW_CLAUDE_WORKERS=1` or `"allow_claude_workers": true` in config.json.
    """
    import os

    if str(os.environ.get("PARHELION_ALLOW_CLAUDE_WORKERS", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        from ..drivers.base import load_parhelion_config

        doc = cfg if cfg is not None else load_parhelion_config()
    except Exception:
        doc = {}
    return bool((doc or {}).get("allow_claude_workers"))


class _WorkerRunner:
    """Per-agent turn pump keeping persistent RPC adapters warm across follow-up messages."""

    def __init__(self, agent_id: str, store: AgentBusStore, adapter: BaseWorkerAdapter,
                 on_notify: Callable[[], None], *, resume: bool = False,
                 on_fault: Callable[[str, str], None] | None = None) -> None:
        self.agent_id = agent_id
        self.store = store
        self.adapter = adapter
        self._on_notify = on_notify
        self._queue: queue.Queue[tuple[str, bool] | None] = queue.Queue()
        self.squad_turn = False
        self._stop_flag = threading.Event()
        self._context_percent: int | None = None
        self._context_tokens = 0
        self._context_window: int | None = None
        self._on_fault = on_fault
        self._stage_result = ""
        self.lease = claim_runner(store.db_path.parent, agent_id)
        if resume:
            try:
                current = store.get_agent(agent_id)
                if not current or current["status"] == "stopped":
                    raise RuntimeError("Worker seat is inactive")
                reap_groups(agent_id, owned_groups({agent_id}).get(agent_id, set()))
            except BaseException:
                self.lease.close()
                raise
        self.adapter.on_spawn = lambda pid, pgid: store.update_agent(agent_id, pid=pid, pgid=pgid)
        self._thread = threading.Thread(target=self._loop, name=f"agent-{agent_id}", daemon=True)
        self._thread.start()

    def enqueue(self, prompt: str, *, squad: bool = False) -> None:
        if not self._stop_flag.is_set():
            self._queue.put((prompt, squad))

    def interrupt(self) -> None:
        self.adapter.interrupt()

    def stop(self) -> None:
        self._stop_flag.set()
        self.adapter.stop()
        self._queue.put(None)
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError(f"Worker {self.agent_id} is still stopping")

    def _on_tool_event(self, kind: str, label: str) -> None:
        if kind == "context":
            percent = int(label)
            if self._context_percent != percent:
                self._context_percent = percent
                self.store.update_agent(self.agent_id, context_percent=percent)
                self._on_notify()
            return
        if kind == "context_tokens":
            self._context_tokens = int(label)
            return
        if kind == "context_window":
            self._context_window = int(label) or None
            return
        if kind == "model":
            self.store.append_message(self.agent_id, self._reply_direction, "model", label)
            self._on_notify()
            return
        if kind == "tool":
            updates: dict[str, Any] = {"last_tool": label}
            if self.adapter.native_session_id:
                updates["native_session_id"] = self.adapter.native_session_id
            if self.adapter.pid:
                updates["pid"] = self.adapter.pid
                updates["pgid"] = self.adapter.pgid
            self.store.update_agent(self.agent_id, **updates)
            self._on_notify()

    @property
    def _reply_direction(self):
        return "squad_note" if self.squad_turn else "worker_to_orch"

    def _should_compact(self) -> bool:
        return (self._context_tokens >= compact_threshold(self._context_window)
                or (self._context_percent or 0) >= COMPACT_PERCENT)

    def _checkpoint(self, rec: dict[str, Any], reply: str) -> str:
        """Stage result (or the reply tail), the platform state card and a continuation line."""
        from ..services import state_card
        card = state_card.build(self.adapter.workspace, str(rec.get("problem") or ""), self.agent_id)
        stage = (self._stage_result or str(reply or "")[-1500:]).strip()
        room = max(0, CHECKPOINT_MAX_CHARS - len(card) - len(CHECKPOINT_TAIL) - 2)
        return "\n".join([stage[:room], card, CHECKPOINT_TAIL])[:CHECKPOINT_MAX_CHARS]

    def _compact(self, rec: dict[str, Any], reply: str) -> None:
        self.store.update_agent(self.agent_id, status="compacting")
        self._on_notify()
        checkpoint = self._checkpoint(rec, reply)
        self._context_percent = None
        try:
            outcome = self.adapter.compact(checkpoint)
            if outcome:
                self.store.append_message(self.agent_id, self._reply_direction, "compact", outcome)
        finally:
            self._context_tokens = 0
            if not self._stop_flag.is_set():
                self.store.update_agent(self.agent_id, status="idle",
                    native_session_id=self.adapter.native_session_id,
                    context_percent=self._context_percent)

    def _loop(self) -> None:
        try:
            self._run_turns()
        finally:
            try:
                self.adapter.stop()
                self.store.update_agent(self.agent_id, pid=None, pgid=None)
            finally:
                self.lease.close()

    def _run_turns(self) -> None:
        while not self._stop_flag.is_set():
            item = self._queue.get()
            if item is None or self._stop_flag.is_set():
                break
            prompt, self.squad_turn = item
            rec = self.store.get_agent(self.agent_id) or {}
            self.squad_turn = self.squad_turn or rec.get("role_label") == "bench-author"
            turn_n = int(rec.get("turn_count") or 0) + 1
            self.store.update_agent(self.agent_id, status="working", turn_count=turn_n, error="")
            self._stage_result = ""
            self._on_notify()
            try:
                reply = self.adapter.send_turn(prompt, on_event=self._on_tool_event)
                branch, sha7 = _git_branch_and_sha(self.adapter.worktree)
                self.store.update_agent(
                    self.agent_id,
                    status="idle" if not self._stop_flag.is_set() else "stopped",
                    pid=self.adapter.pid,
                    pgid=self.adapter.pgid,
                    native_session_id=self.adapter.native_session_id,
                    branch=branch or rec.get("branch", ""),
                    last_commit_sha=sha7 or rec.get("last_commit_sha", ""),
                    last_tool="",
                )
                if not self._stop_flag.is_set() and reply:
                    self.store.append_message(
                        self.agent_id, self._reply_direction, "result", reply, commit_sha=sha7
                    )
                    if self._should_compact():
                        self._compact(rec, reply)
            except Exception as exc:
                fault = ""
                if not self._stop_flag.is_set():
                    err_s = str(exc)[:600]
                    self.store.update_agent(self.agent_id, status="error", error=err_s, last_tool="")
                    self.store.append_message(self.agent_id, "worker_to_orch", "error", err_s)
                    fault = err_s if PROVIDER_FAULT.search(err_s) else ""
                self._stop_flag.set()
                if fault and callable(self._on_fault):
                    try:
                        self._on_fault(self.adapter.driver_kind, fault)
                    except Exception:
                        pass
                return
            finally:
                self.squad_turn = False
                self._on_notify()


class AgentMeshManager:
    """Manages cross-Harness external workers, role boundaries, and SSE hot-mesh projections."""

    def __init__(
        self,
        *,
        on_change: Callable[[], None] | None = None,
        adapter_factory: AdapterFactory = create_worker_adapter,
        max_workers: int | None = None,
    ) -> None:
        self._on_change = on_change
        self._adapter_factory = adapter_factory
        self.max_workers = int(max_workers) if max_workers is not None else None
        self._lock = threading.Lock()
        self.sprint_lock = threading.Lock()
        self._suspend_lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stores: dict[str, AgentBusStore] = {}
        self._feedback_versions = {}
        self._runners: dict[str, _WorkerRunner] = {}
        self._doctor_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._native_cache: dict[str, tuple[float, tuple[dict[str, Any], list[dict[str, Any]]]]] = {}
        self._ws_signals_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._usage_monitor = UsageMonitor()

    # ---- provider fault circuit breaker -------------------------------------------------
    @staticmethod
    def _suspensions_path(ws: Path) -> Path:
        return resolve_workspace_state_dir(Path(ws).expanduser().resolve()) / "driver_suspensions.json"

    def _read_suspensions(self, ws: Path) -> dict[str, Any]:
        doc = _cfg.read_json(self._suspensions_path(ws), {})
        return doc if isinstance(doc, dict) else {}

    def _write_suspensions(self, ws: Path, doc: dict[str, Any]) -> None:
        path = self._suspensions_path(ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def driver_suspended(self, ws: Path, driver: str) -> bool:
        return str(driver or "").strip().lower() in self._read_suspensions(ws)

    def suspension_rows(self, ws: Path, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Provider-fault suspensions with the workers they stopped, for the hot mesh."""
        rows = []
        for drv, entry in sorted(self._read_suspensions(ws).items()):
            entry = entry if isinstance(entry, dict) else {}
            rows.append({"driver": drv, "since": entry.get("since") or "", "reason": entry.get("reason") or "",
                         "workers": [w["agent_id"] for w in workers if str(w.get("driver") or "").lower() == drv
                                     and w.get("error") == SUSPENDED_ERROR][:12]})
        return rows

    def clear_driver_suspension(self, ws: Path, driver: str) -> bool:
        drv = str(driver or "").strip().lower()
        with self._suspend_lock:
            doc = self._read_suspensions(ws)
            if drv not in doc:
                return False
            doc.pop(drv, None)
            self._write_suspensions(ws, doc)
        self._notify()
        return True

    def _make_runner(self, ws: Path, agent_id: str, store: AgentBusStore, adapter: BaseWorkerAdapter,
                     *, resume: bool = False) -> _WorkerRunner:
        return _WorkerRunner(agent_id, store, adapter, self._notify, resume=resume,
                             on_fault=lambda drv, err: self.suspend_driver(ws, drv, err))

    def suspend_driver(self, ws: Path, driver: str, reason: str) -> dict[str, Any]:
        """Pause every dispatch to `driver` after a provider (balance/quota/auth) fault."""
        ws = Path(ws).expanduser().resolve()
        drv = str(driver or "").strip().lower()
        text = " ".join(str(reason or "").split())[:300]
        with self._suspend_lock:
            doc = self._read_suspensions(ws)
            entry = doc.get(drv)
            fresh = not isinstance(entry, dict)
            if fresh:
                entry = {"since": _cfg.utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": text}
                doc[drv] = entry
                self._write_suspensions(ws, doc)
        store = self._store(ws)
        if fresh:
            store.append_message(
                "platform", "worker_to_orch", "error",
                f"驱动 {drv} 因供应商故障暂停：{entry['reason']}。恢复后用 parhelion_sprint_start 或 parhelion_agent_spawn 重新派发。",
                priority="strategic", event_key=f"suspend:{drv}:{str(entry['since'])[:13]}")
        with self._lock:
            runners = [r for r in self._runners.values()
                       if r.store is store and str(r.adapter.driver_kind).lower() == drv
                       and r._thread is not threading.current_thread()]
        stopped = []
        for runner in runners:
            rec = store.get_agent(runner.agent_id) or {}
            if rec.get("status") in ("stopped", "error"):
                continue
            try:
                runner.stop()
            except Exception:
                pass
            store.update_agent(runner.agent_id, status="error", error=SUSPENDED_ERROR, last_tool="")
            stopped.append(runner.agent_id)
        self._notify()
        return {"ok": True, "driver": drv, "since": entry["since"], "stopped": stopped, "new": fresh}

    def deliver_test_results(self, ws: Path) -> int:
        """Post finished test runs to the requesting worker; wake it if idle and it has not observed the result."""
        try:
            test_runs = importlib.import_module(__package__.rsplit(".", 1)[0] + ".services.test_runs")
        except Exception:
            return 0  # work package A not available yet
        ws = Path(ws).expanduser().resolve()
        store = self._store(ws)
        delivered = 0
        for record in test_runs.list_undelivered(ws) or []:
            request_id = str(record.get("request_id") or "")
            agent_id = str(record.get("agent_id") or "")
            if not request_id or not agent_id:
                continue
            line = test_runs.compact_line(record)
            msg = store.append_message(agent_id, "orch_to_worker", "test_result", line,
                                       problem=str(record.get("problem") or ""), event_key=f"test:{request_id}")
            if msg and not record.get("observed"):
                rec = store.get_agent(agent_id)
                if rec and rec.get("status") == "idle":
                    try:
                        self._enqueue_to_worker(ws, rec, f"{line} Full digest: parhelion_test_wait request_id={request_id}")
                    except Exception:
                        pass
            test_runs.mark_delivered(ws, request_id)
            delivered += 1
        if delivered:
            self._notify()
        return delivered

    def deliver_judgements(self, ws: Path, threshold: float) -> None:
        ledger = ws / "results/submit-queue-outcome.jsonl"
        if not ledger.is_file():
            return
        store = self._store(ws)
        authors = {a["problem"]: a["agent_id"] for a in reversed(store.list_agents(include_stopped=False, limit=-1))
                   if a["role_label"] == "bench-author"}
        version = ledger.stat().st_mtime_ns, ledger.stat().st_size, tuple(sorted(authors.items()))
        if self._feedback_versions.get(ws) == version:
            return
        rows_by_title = {}
        for row in _cfg.read_jsonl(ledger):
            if row.get("submission_id") and row.get("title"):
                rows_by_title.setdefault(row["title"], []).append(row)
        requests = {(r.get("title"), r.get("sha"), r.get("queued_at")): r.get("agent_id")
                    for r in _cfg.read_jsonl(ws / "results/submit-queue.jsonl") if r.get("agent_id")}
        for title, rows in rows_by_title.items():
            cal = _cfg.read_json(ws / "results" / title / "calibration.json", {})
            gamma = float(cal.get("decay_factor_gamma") or 1.0)
            results = list(judge_feedback(rows, threshold, gamma))
            calibration_seqs = []
            for row, events in results:
                sha, sid = row.get("sha") or "", row["submission_id"]
                agent = row.get("agent_id") or requests.get((title, sha, row.get("queued_at")))
                body = f"{title} {sha[:7]} · {row['status']} · score {row.get('score')} · submission {sid}"
                revision = ":unscored" if row["status"] == "Pass" and row.get("score") is None else ""
                author = authors.get(title)
                if author:
                    key = f"calibration:{sid}:{sha}:{author}{revision}"
                    if not store.has_message(key):
                        evidence, artifacts = calibration_evidence(ws, row)
                        msg = store.append_message(author, "squad_to_worker", "calibration", evidence, artifacts=artifacts,
                                                   problem=title, commit_sha=sha, event_key=key)
                        if msg:
                            calibration_seqs.append(msg["seq"])
                if row["status"] in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error"):
                    from ..services.queue_service import check_commit_safety_gate
                    if sha and check_commit_safety_gate(ws, title, sha):
                        events.append(("escape_zero", "Review the local/online divergence, add a regression case, and assign an independent KB review."))
                if agent:
                    feedback = body + " " + " ".join(message for _, message in events)
                    msg = store.append_message(agent, "orch_to_worker", "judge_result", feedback,
                                               problem=title, commit_sha=sha, event_key=f"receipt:{sid}:{agent}{revision}")
                    if msg:
                        target = store.get_agent(agent)
                        if target:
                            self._enqueue_to_worker(ws, target, feedback, spawn_runner=False)
                        self._notify()
                for kind, message in events:
                    msg = store.append_message(agent or "platform", "worker_to_orch", kind, body + " " + message,
                                               problem=title, priority="strategic", commit_sha=sha,
                                               event_key=f"strategy:{sid}:{kind}")
                    if msg:
                        self._notify()
            if calibration_seqs:
                note = (f"{len(calibration_seqs)} official calibration receipt(s) for {title}. Read your inbox "
                        f"starting at since_seq={calibration_seqs[0] - 1} through seq {calibration_seqs[-1]}. "
                        "Use the linked reports to assess coverage, then send actionable findings to your teammates.")
                author_rec = store.get_agent(authors[title])
                if author_rec:
                    self._enqueue_to_worker(ws, author_rec, note, squad=True)
                self._notify()
        self._feedback_versions[ws] = version

    def _notify(self) -> None:
        with self._cond:
            self._cond.notify_all()
        if callable(self._on_change):
            try:
                self._on_change()
            except Exception:
                pass

    def _store(self, workspace: Path) -> AgentBusStore:
        key = str(workspace.expanduser().resolve())
        with self._lock:
            st = self._stores.get(key)
            if st is None:
                st = AgentBusStore(Path(key))
                self._stores[key] = st
            return st

    def recover_workers(self, workspace: Path) -> None:
        """Only daemon startup reconciles detached workers. A reader has no ownership."""
        store = self._store(workspace)
        records = store.list_agents(limit=-1)
        groups = owned_groups({r["agent_id"] for r in records})
        for rec in records:
            aid = rec["agent_id"]
            if rec["status"] in ("stopped", "error") and not (rec["pid"] or aid in groups):
                continue
            try:
                lease = claim_runner(store.db_path.parent, aid)
            except RuntimeError:
                continue
            with lease:
                reap_groups(aid, groups.get(aid, set()))
                current = store.get_agent(aid)
                if current and current["status"] not in ("stopped", "error"):
                    error = "Previous worker runtime ended; send a follow-up to resume this seat."
                    store.update_agent(aid, status="error", error=error, pid=None, pgid=None, last_tool="")
                elif current:
                    store.update_agent(aid, pid=None, pgid=None)

    def _cached_doctor(self) -> dict[str, Any]:
        now = time.monotonic()
        ts, data = self._doctor_cache
        if data and (now - ts) < 6.0:
            return data
        try:
            fresh = doctor_drivers()
        except Exception:
            fresh = {"active_driver": get_active_driver_kind(), "diagnostics": []}
        self._doctor_cache = (now, fresh)
        return fresh

    def _observed_driver_sessions(self, workspace: Path, active_driver: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return (primary_orchestrator_session, native_subagents) from a single cached GUI observation."""
        key = f"{workspace.expanduser().resolve()}::{active_driver}"
        now = time.monotonic()
        cached = self._native_cache.get(key)
        if cached and (now - cached[0]) < 3.5:
            return cached[1]
        primary: dict[str, Any] = {}
        subs: list[dict[str, Any]] = []
        try:
            rows = get_driver(active_driver).observe_gui_sessions(workspace=workspace, limit=12)
            for r in rows:
                if not primary and not r.get("parent_conversation_id"):
                    primary = r
                is_sub = bool(r.get("parent_conversation_id")) or str(r.get("status") or "") == "running"
                if not is_sub:
                    continue
                subs.append({
                    "id": str(r.get("session_id") or r.get("native_session_id") or "")[:24],
                    "driver": active_driver,
                    "role_label": str(r.get("agent_name") or "subagent")[:20],
                    "status": str(r.get("status") or "idle"),
                    "problem": str(r.get("problem") or workspace.name),
                    "steps": int(r.get("observed_steps") or 0),
                    "last_tool": str(r.get("last_tool") or "")[:32],
                    "title": str(r.get("brief_preview") or r.get("agent_name") or "")[:64],
                    "deep_link": str(r.get("deep_link") or ""),
                })
                if len(subs) >= 4:
                    break
            if not primary and rows:
                primary = rows[0]
        except Exception:
            primary, subs = {}, []
        res = (primary, subs)
        self._native_cache[key] = (now, res)
        return res

    @staticmethod
    def _fallback_driver_deep_link(driver: str, diag: dict[str, Any]) -> str:
        drv = str(driver or "").lower()
        scheme = str(diag.get("url_scheme") or "").strip()
        if drv == "antigravity":
            return "antigravity://open"
        if drv == "deepseek":
            return "dsh://open"
        if drv == "codex":
            return "codex://open"
        if drv == "claude":
            return "claude://" if diag.get("gui_installed") else ""
        return f"{scheme}://open" if scheme else ""

    def _build_worker_jump(
        self,
        ws: Path,
        worker: dict[str, Any],
        diag: dict[str, Any],
        orch_deep_link: str = "",
    ) -> tuple[str, str, str]:
        """Return (deep_link, resume_command, jump_mode) for an external MCP worker."""
        drv = str(worker.get("driver") or "").lower()
        aid = str(worker.get("agent_id") or "")
        nsid = str(worker.get("native_session_id") or "").strip()
        wt = str(worker.get("worktree") or ws)
        bin_path = str(diag.get("binary_path") or "").strip()

        if drv == "deepseek":
            # DSH ACP shares ~/.dsh/sessions with DeepSeek Harness Desktop
            return "dsh://open", "", "deeplink"

        if drv == "antigravity":
            dl = orch_deep_link if orch_deep_link.startswith("antigravity://") else "antigravity://open"
            return dl, "", "deeplink"

        if drv == "codex":
            worker_codex_home = worker_runtime_dir(ws, aid) / "codex_home"
            exe = bin_path or "codex"
            res_arg = shlex.quote(nsid) if nsid else "--last"
            cmd = f"cd {shlex.quote(wt)} && CODEX_HOME={shlex.quote(str(worker_codex_home))} {shlex.quote(exe)} resume {res_arg}"
            dl = f"codex://threads/{nsid}" if nsid else "codex://open"
            return dl, cmd, ("terminal" if bin_path else "deeplink")

        if drv == "claude":
            exe = bin_path or "claude"
            res_flag = f"--resume {shlex.quote(nsid)}" if nsid else "--continue"
            cmd = f"cd {shlex.quote(wt)} && {shlex.quote(exe)} {res_flag}"
            dl = "claude://" if diag.get("gui_installed") else ""
            return dl, cmd, ("terminal" if bin_path else "deeplink")

        if drv in ("pi", "gemini"):
            exe = bin_path or drv
            cmd = f"cd {shlex.quote(wt)} && {shlex.quote(exe)}"
            return "", cmd, "terminal"

        return self._fallback_driver_deep_link(drv, diag), "", "deeplink"

    def _cached_ws_signals(self, workspace: Path) -> dict[str, Any]:
        """Cached (3s) per-problem score, difficulty & ESCAPE_LOCK map for Squad grouping & alerts."""
        key = str(workspace.expanduser().resolve())
        now = time.monotonic()
        cached = self._ws_signals_cache.get(key)
        if cached and (now - cached[0]) < 3.0:
            return cached[1]
        ws = Path(key)
        snap = _cfg.read_json(ws / "results" / "score-snapshot.json", {})
        scores: dict[str, dict[str, Any]] = {}
        escape_locked: list[str] = []
        if isinstance(snap, dict):
            for prob, row in snap.items():
                if not isinstance(row, dict):
                    continue
                cal = _cfg.read_json(ws / "results" / prob / "calibration.json", {})
                esc = bool(isinstance(cal, dict) and cal.get("escape_lock"))
                if esc:
                    escape_locked.append(prob)
                scores[prob] = {
                    "score": float(row.get("score") or 0.0),
                    "status": str(row.get("status") or ""),
                    "difficulty": int(_cfg.DEFAULT_DIFFICULTIES.get(prob, 2)),
                    "escape_locked": esc,
                }
        res = {"scores": scores, "escape_locked": escape_locked}
        self._ws_signals_cache[key] = (now, res)
        return res

    def get_harness_pool(self, workspace: Path) -> dict[str, Any]:
        doc = self._cached_doctor()
        active = str(doc.get("active_driver") or get_active_driver_kind()).lower()
        store = self._store(workspace)
        allow_claude = claude_workers_allowed()
        eligible: list[dict[str, Any]] = []
        orchestrator_info: dict[str, Any] = {"kind": active, "display_name": active.upper(), "protocol": "", "gui_app_path": ""}
        for d in doc.get("diagnostics") or []:
            k = str(d.get("kind") or "").lower()
            if k == active:
                orchestrator_info = {
                    "kind": k,
                    "display_name": str(d.get("display_name") or k),
                    "protocol": str(d.get("protocol") or ""),
                    "gui_app_path": str(d.get("gui_app_path") or ""),
                    "installed": bool(d.get("installed")),
                    "auth_ok": bool(d.get("auth_ok")),
                }
                continue
            if k == CLAUDE_DRIVER and not allow_claude:
                continue  # policy: Claude workers need explicit admin opt-in
            eligible.append({
                "kind": k,
                "display_name": str(d.get("display_name") or k),
                "protocol": str(d.get("protocol") or ""),
                "gui_app_path": str(d.get("gui_app_path") or ""),
                "installed": bool(d.get("installed")),
                "auth_ok": bool(d.get("auth_ok")),
                "has_cli": bool(d.get("binary_path")),
                "detail": str(d.get("detail") or ""),
            })
        hot = self.build_hot_mesh(workspace)
        return {
            "ok": True,
            "workspace": str(workspace.expanduser().resolve()),
            "orchestrator": orchestrator_info,
            "disabled_self_driver": active,
            "eligible_external_drivers": eligible,
            "dispatch_policy": {
                "prefer_native_subagents": True,
                "external_spawn_scope": "admin_requested_multi_harness_only",
                "claude_workers_allowed": allow_claude,
            },
            "squads": hot["squads"],
            "strategic_alerts": hot["strategic_alerts"],
            "active_workers": store.list_agents(include_stopped=False),
            "all_workers": store.list_agents(include_stopped=True),
        }

    def spawn_agent(
        self,
        workspace: Path,
        *,
        driver: str,
        task: str,
        role_label: str = "worker",
        problem: str = "",
        collab_mode: str = "solo",
        axis: str = "",
        worktree_mode: str = "isolated",
        track: str = "",
        _sprint: bool = False,
    ) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        drv_kind = str(driver or "").strip().lower()
        if drv_kind not in VALID_DRIVERS:
            raise ValueError(f"Unknown driver '{driver}'. Supported: {', '.join(VALID_DRIVERS)}")
        active = get_active_driver_kind()
        if drv_kind == active and not _sprint:
            raise ValueError(
                f"Self-dispatch blocked: '{drv_kind}' is the primary orchestrator. "
                "Manage homogeneous subagents natively; use Parhelion MCP only for OTHER Harnesses."
            )
        if drv_kind == CLAUDE_DRIVER and not claude_workers_allowed():
            raise ValueError(
                "Claude workers are disabled by policy: prefer the orchestrator's own native "
                "subagents, and only dispatch Claude after an administrator enables it "
                "(`PARHELION_ALLOW_CLAUDE_WORKERS=1` or config allow_claude_workers=true)."
            )
        if not str(task or "").strip():
            raise ValueError("task prompt must be non-empty")

        if self.driver_suspended(ws, drv_kind):
            if _sprint:
                return {"ok": False, "blocked": True, "reason": "driver suspended"}
            # An explicit admin spawn confirms the provider has recovered.
            self.clear_driver_suspension(ws, drv_kind)

        store = self._store(ws)
        if self.max_workers is not None:
            live_count = len(store.list_agents(include_stopped=False, limit=-1))
            if live_count >= self.max_workers:
                raise RuntimeError(f"External worker limit reached ({live_count}/{self.max_workers}); stop an idle worker first.")

        agent_id = f"{drv_kind[:3]}-{uuid.uuid4().hex[:6]}"
        safe_role = re.sub(r"[^A-Za-z0-9._-]+", "-", (role_label or "worker").strip()).strip("-")[:20] or "worker"
        mode = worktree_mode if worktree_mode in ("isolated", "readonly", "shared") else "isolated"
        c_mode = collab_mode if collab_mode in ("orthogonal", "pipeline", "solo") else "solo"
        clean_axis = str(axis or "").strip()[:28]
        prob_title = problem.strip()
        if prob_title:
            resolved_title, repo_dir, _ = resolve_problem_repo(ws, prob_title)
            prob_title = resolved_title
            if mode == "isolated":
                wt = prepare_session_worktree(ws, resolved_title, track_name=track or f"{drv_kind}-{safe_role}-{agent_id[-4:]}")
                if _sprint and wt == repo_dir:
                    raise ValueError(f"Cannot isolate a sprint worker in {repo_dir}; check the repository HEAD")
            else:
                wt = repo_dir if repo_dir.is_dir() else ws
        else:
            wt = ws

        branch, sha7 = _git_branch_and_sha(wt)
        worker_prompt = (
            f"{prob_title or ws.name} · {safe_role}\n{task.strip()}\n\n"
            "Use Parhelion for tests and candidate submissions. Use parhelion_squad to share findings with teammates."
        )
        if safe_role == "bench-author" and prob_title:
            worker_prompt = compose_bench_author_brief(ws, prob_title) + "\n\n" + task.strip()
        elif prob_title:
            try:
                worker_prompt = compose_session_brief(ws, prob_title, goal=task.strip(), worktree=wt, branch=branch,
                                                      role=safe_role, agent_id=agent_id)
            except Exception:
                pass
        rec = store.create_agent({
            "agent_id": agent_id,
            "driver": drv_kind,
            "role_label": safe_role,
            "problem": prob_title or ws.name,
            "collab_mode": c_mode,
            "axis": clean_axis,
            "worktree": str(wt),
            "worktree_mode": mode,
            "branch": branch,
            "status": "spawning",
            "last_commit_sha": sha7,
        })
        if rec["agent_id"] != agent_id:
            return {"ok": rec["status"] != "error", "agent": rec, "reused": True}
        msg = store.append_message(agent_id, "orch_to_worker", "task", task.strip(), problem=prob_title or ws.name)
        log_path = resolve_session_logs_dir() / f"mesh-{agent_id}.jsonl"
        try:
            with self._lock:
                adapter = self._adapter_factory(ws, wt, drv_kind, agent_id, log_path)
                runner = self._make_runner(ws, agent_id, store, adapter)
                self._runners[agent_id] = runner
        except Exception as exc:
            store.update_agent(agent_id, status="error", error=str(exc)[:600])
            raise
        runner.enqueue(worker_prompt)
        self._notify()
        return {"ok": True, "agent": rec, "message": msg}

    def send_message(self, workspace: Path, agent_id: str, message: str, *, delivery: str = "queue") -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        store = self._store(ws)
        rec = store.get_agent(agent_id)
        if rec is None:
            raise KeyError(f"Unknown agent_id: {agent_id}")
        if rec["status"] == "stopped":
            raise RuntimeError(f"Agent '{agent_id}' is stopped; spawn a new worker instead.")
        kind = "interrupt" if delivery == "interrupt" else "followup"
        msg = store.append_message(agent_id, "orch_to_worker", kind, message.strip())
        wake = self._enqueue_to_worker(ws, rec, message.strip(), interrupt=delivery == "interrupt", explicit=True)
        self._notify()
        out = {"ok": True, "agent_id": agent_id, "delivery": kind, "message": msg, "woken": bool(wake.get("ok"))}
        if not wake.get("ok"):
            out["reason"] = wake.get("reason", "")
        return out

    def _enqueue_to_worker(self, ws, rec, message, *, interrupt=False, squad=False, explicit=False,
                           spawn_runner=True) -> dict[str, Any]:
        """Wake a worker with `message`. Never wakes stopped seats or seats of a suspended driver.

        Error seats are skipped unless an explicit orchestrator follow-up resumes them.
        Callers record the bus message themselves; a refusal only means nobody is woken.
        """
        store = self._store(ws)
        agent_id = rec["agent_id"]
        if self.driver_suspended(ws, rec.get("driver") or ""):
            return {"ok": False, "reason": "driver suspended"}
        with self._lock:
            old = self._runners.get(agent_id)
        if explicit and rec["status"] == "error" and old and spawn_runner:
            old.stop()
        with self._lock:
            rec = store.get_agent(agent_id)
            if not rec or rec["status"] == "stopped":
                return {"ok": False, "reason": "worker stopped"}
            if rec["status"] == "error" and not explicit:
                return {"ok": False, "reason": "worker error"}
            runner = self._runners.get(agent_id)
            if runner is None or not runner._thread.is_alive():
                if not spawn_runner:
                    return {"ok": False, "reason": "worker runtime not attached"}
                log_path = resolve_session_logs_dir() / f"mesh-{agent_id}.jsonl"
                adapter = self._adapter_factory(ws, Path(rec["worktree"] or ws), rec["driver"], agent_id, log_path)
                adapter.native_session_id = str(rec.get("native_session_id") or "")
                runner = self._make_runner(ws, agent_id, store, adapter, resume=True)
                self._runners[agent_id] = runner
        if interrupt:
            runner.interrupt()
        runner.enqueue(message, squad=squad)
        return {"ok": True}

    @staticmethod
    def _squad_targets(mates: dict[str, dict[str, Any]], recipient: str) -> list[str]:
        """Resolve agent_id, `all`, or an axis / role_label / sprint-<axis> name (case-insensitive)."""
        key = recipient.strip()
        if key in mates:
            return [key]
        low = key.lower()
        if low == "all" and mates:
            return list(mates)
        hits = []
        if low and low != "all":
            for aid, mate in mates.items():
                axis = str(mate.get("axis") or "").lower()
                names = {axis, str(mate.get("role_label") or "").lower(), ("sprint-" + axis) if axis else ""}
                if low in names - {""}:
                    hits.append(aid)
        if len(hits) == 1:
            return hits
        roster = ", ".join(f"{aid}:{m.get('axis') or '-'}:{m.get('role_label') or '-'}" for aid, m in mates.items())
        problem = "matches several teammates" if hits else "matches no active teammate"
        raise ValueError(f"Recipient '{key}' {problem}. Teammates (agent_id:axis:role_label): {roster or 'none'}")

    def squad(self, ws, sender, *, recipient="", message="", request_id=""):
        store = self._store(ws)
        me = store.get_agent(sender)
        if not me or not me["problem"] or me["status"] in ("stopped", "error"):
            raise ValueError("An active problem worker is required")
        mates = {a["agent_id"]: a for a in store.list_agents(include_stopped=False, limit=-1)
                 if a["problem"] == me["problem"] and a["agent_id"] != sender}
        if not recipient and not message:
            return {"ok": True, "problem": me["problem"], "peers": [
                {k: a[k] for k in ("agent_id", "role_label", "axis", "driver", "status")} for a in mates.values()]}
        if not message.strip() or len(request_id) > 128:
            raise ValueError("Send a non-empty message to a teammate; request_id is limited to 128 characters")
        targets = self._squad_targets(mates, recipient)
        text = message.strip()
        if len(text) > SQUAD_MAX_CHARS:
            text = text[:SQUAD_MAX_CHARS] + "…[truncated]"
        sent: list[tuple[str, dict[str, Any]]] = []
        for target in targets:
            body = f"[{sender} → {target}] {text}"
            msg = store.append_message(target, "squad_to_worker", "peer", body, problem=me["problem"],
                                       event_key=f"peer:{sender}:{target}:{request_id}" if request_id else None)
            if msg:
                self._enqueue_to_worker(ws, mates[target], body, squad=True)
            sent.append((target, msg))
        if any(msg for _, msg in sent):
            self._notify()
        if recipient.strip().lower() == "all" and recipient.strip() not in mates:
            return {"ok": True, "recipients": [t for t, _ in sent], "seqs": [m.get("seq") for _, m in sent],
                    "duplicate": not any(m for _, m in sent)}
        target, msg = sent[0]
        return {"ok": True, "recipient": target, "duplicate": not bool(msg), "seq": msg.get("seq")}

    def wait_events(
        self,
        workspace: Path,
        *,
        since_seq: int = 0,
        agent_ids: list[str] | None = None,
        problem: str | None = None,
        strategic_only: bool = True,
        timeout_sec: float = DEFAULT_WAIT_SEC,
    ) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        store = self._store(ws)
        cap_timeout = max(0.5, min(MAX_WAIT_SEC, float(timeout_sec)))
        id_set = {str(x) for x in (agent_ids or []) if str(x).strip()}
        deadline = time.monotonic() + cap_timeout

        def _collect() -> list[dict[str, Any]]:
            return store.list_messages(
                problem=problem or None,
                since_seq=since_seq,
                strategic_only=strategic_only,
                recipients=id_set,
                for_orchestrator=True,
                limit=40,
            )

        with self._cond:
            matched = _collect()
            while not matched:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                self._cond.wait(timeout=min(rem, 1.0))
                matched = _collect()

        agents = store.list_agents(include_stopped=True)
        if id_set:
            agents = [a for a in agents if a["agent_id"] in id_set]
        if problem:
            agents = [a for a in agents if str(a.get("problem") or "").lower() == problem.lower()]
        # After a bus reset the old cursor is past the counter; resume from the top, not from it.
        next_seq = max([store.resolve_cursor(since_seq), *[int(m["seq"]) for m in matched]])
        squad_map: dict[str, list[str]] = {}
        for a in agents:
            p = str(a.get("problem") or ws.name)
            st_tag = f"{a['driver'][:3]}:{a['status']}"
            if int(a.get("no_gain_streak") or 0) >= STAGNATION_STREAK_THRESHOLD:
                st_tag += f"(stag:{a['no_gain_streak']})"
            squad_map.setdefault(p, []).append(st_tag)
        return {
            "ok": True,
            "timed_out": len(matched) == 0,
            "next_seq": next_seq,
            "strategic_only": strategic_only,
            "messages": matched,
            "squad_digest": [f"{p}: {', '.join(tags)}" for p, tags in sorted(squad_map.items())],
            "agents": [
                {
                    "agent_id": a["agent_id"], "driver": a["driver"], "role_label": a["role_label"],
                    "status": a["status"], "problem": a["problem"], "collab_mode": a.get("collab_mode", "solo"),
                    "axis": a.get("axis", ""), "turn_count": a["turn_count"],
                    "no_gain_streak": int(a.get("no_gain_streak") or 0),
                    "unread_for_orch": a["unread_for_orch"], "last_tool": a["last_tool"],
                    "last_commit_sha": a["last_commit_sha"],
                }
                for a in agents
            ],
        }

    def read_messages(
        self,
        workspace: Path,
        *,
        agent_id: str | None = None,
        problem: str | None = None,
        since_seq: int = 0,
        limit: int = 60,
        ack: bool = True,
        include_squad: bool = True,
    ) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        store = self._store(ws)
        msgs = store.list_messages(agent_id=agent_id, problem=problem, since_seq=since_seq, limit=limit,
                                   include_squad=include_squad)
        if ack and agent_id and msgs:
            store.ack_messages(agent_id, max(int(m["seq"]) for m in msgs), reader="orch")
            self._notify()
        agent = store.get_agent(agent_id) if agent_id else None
        diff_stat = _git_diff_stat(Path(agent["worktree"])) if (agent and agent.get("worktree")) else ""
        return {
            "ok": True,
            "agent": agent,
            "problem": problem or (agent.get("problem") if agent else ""),
            "diff_stat": diff_stat,
            "latest_seq": store.latest_seq(),
            "cursor_reset": bool(since_seq) and store.resolve_cursor(since_seq) == 0,
            "messages": msgs,
        }

    def worker_report(
        self,
        workspace: Path,
        agent_id: str,
        *,
        kind: str = "progress",
        summary: str,
        no_gain_streak: int | None = None,
        artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        store = self._store(ws)
        rec = store.get_agent(agent_id)
        if rec is None:
            raise KeyError(f"Unknown worker agent_id: {agent_id}")
        valid_kinds = ("progress", "breakthrough", "stagnation", "escape_zero", "question", "result", "error")
        norm_kind = kind if kind in valid_kinds else "progress"
        with self._lock:
            runner = self._runners.get(agent_id)
        if norm_kind in ("result", "breakthrough", "stagnation", "escape_zero"):
            if runner is not None:
                runner._stage_result = summary.strip()[:4000]
        streak = int(rec.get("no_gain_streak") or 0)
        # Judged receipt analysis owns these automatic alerts. Workers can always
        # raise an explicit question when a decision is needed.
        if norm_kind in ("stagnation", "breakthrough", "escape_zero"):
            norm_kind = "progress"
        priority = "strategic" if norm_kind in STRATEGIC_KINDS else "normal"
        _, sha7 = _git_branch_and_sha(Path(rec["worktree"])) if rec.get("worktree") else ("", "")
        if norm_kind == "question":
            store.update_agent(agent_id, status="waiting_input")
        local = rec["role_label"] == "bench-author" or bool(runner and runner.squad_turn)
        msg = store.append_message(
            agent_id, "squad_note" if local and priority == "normal" else "worker_to_orch", norm_kind, summary.strip()[:4000],
            artifacts=artifacts, commit_sha=sha7, priority=priority, problem=str(rec.get("problem") or ""),
        )
        self._notify()
        return {"ok": True, "seq": msg["seq"], "agent_id": agent_id, "kind": norm_kind, "priority": priority, "no_gain_streak": streak}

    def post_admin_directive(
        self,
        workspace: Path,
        body: str,
        *,
        problem: str = "",
        broadcast_workers: bool = False,
    ) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        text = str(body or "").strip()
        if not text:
            raise ValueError("directive body must be non-empty")
        store = self._store(ws)
        msg = store.append_message(
            "admin", "system", "directive", text[:4000], priority="strategic", problem=problem.strip()
        )
        dispatched: list[str] = []
        if broadcast_workers:
            for a in store.list_agents(include_stopped=False):
                if problem and str(a.get("problem") or "").lower() != problem.lower():
                    continue
                try:
                    self.send_message(ws, a["agent_id"], f"[Admin Directive] {text}", delivery="queue")
                    dispatched.append(a["agent_id"])
                except Exception:
                    pass
        self._notify()
        return {"ok": True, "message": msg, "dispatched_workers": dispatched}

    def worker_inbox(self, workspace: Path, agent_id: str, *, since_seq: int = 0, ack_seq: int | None = None) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        store = self._store(ws)
        if ack_seq is not None:
            store.ack_messages(agent_id, int(ack_seq), reader="worker")
        msgs = store.list_messages(agent_id=agent_id, since_seq=since_seq, worker_inbox=True, limit=30)
        return {"ok": True, "agent_id": agent_id, "messages": msgs, "latest_seq": store.latest_seq(),
                "cursor_reset": bool(since_seq) and store.resolve_cursor(since_seq) == 0}

    def stop_agent(self, workspace: Path, agent_id: str, *, cleanup_worktree: bool = False) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        store = self._store(ws)
        rec = store.get_agent(agent_id)
        if rec is None:
            raise KeyError(f"Unknown agent_id: {agent_id}")
        with self._lock:
            runner = self._runners.get(agent_id)
        if runner is not None:
            runner.stop()
        lease = claim_runner(store.db_path.parent, agent_id)
        with lease:
            if not runner or rec.get("pid"):
                reap_groups(agent_id, owned_groups({agent_id}).get(agent_id, set()))
            updated = store.update_agent(agent_id, status="stopped", pid=None, pgid=None, last_tool="")
        removed_wt = False
        if cleanup_worktree and rec.get("worktree_mode") == "isolated" and rec.get("worktree"):
            wt_p = Path(rec["worktree"])
            if wt_p.is_dir() and ".worktrees" in wt_p.parts:
                try:
                    repo_root = wt_p.parent.parent
                    subprocess.run(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(wt_p)], check=False, timeout=10)
                    removed_wt = not wt_p.exists()
                except Exception:
                    pass
        store.append_message(agent_id, "system", "lifecycle", f"Agent {agent_id} stopped.")
        self._notify()
        return {"ok": True, "agent": updated, "worktree_removed": removed_wt}

    def cleanup_stopped(self, workspace: Path, agent_id: str | None = None) -> dict[str, Any]:
        removed = self._store(workspace).cleanup_stopped(agent_id)
        self._notify()
        return {"ok": True, "removed": removed}

    def stop_all(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
            self._runners.clear()
        for r in runners:
            try:
                r.stop()
                rec = r.store.get_agent(r.agent_id)
                if rec and rec["status"] not in ("error", "stopped"):
                    r.store.update_agent(r.agent_id, status="stopped", pid=None, pgid=None)
            except Exception:
                pass

    def build_hot_mesh(self, workspace: Path) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        doc = self._cached_doctor()
        active = str(doc.get("active_driver") or get_active_driver_kind()).lower()
        store = self._store(ws)
        from .sprint import sprint_seats
        all_workers = store.list_agents(limit=-1)
        seats = sprint_seats(all_workers)
        workers = [w for w in all_workers if w["status"] != "stopped" or
                   seats.get((w["problem"], Path(w["worktree"]).name), w)["agent_id"] == w["agent_id"]]
        now = time.time()
        diag_by_kind = {str(d.get("kind") or "").lower(): d for d in (doc.get("diagnostics") or [])}
        orch_diag = diag_by_kind.get(active, {})
        ws_sig = self._cached_ws_signals(ws)
        scores_map = ws_sig.get("scores") or {}

        allow_claude = claude_workers_allowed()
        ready_drivers = [
            {
                "kind": str(d.get("kind") or "").lower(),
                "display_name": str(d.get("display_name") or ""),
                "protocol": str(d.get("protocol") or ""),
                "gui_app_path": str(d.get("gui_app_path") or ""),
                "installed": bool(d.get("installed")),
                "auth_ok": bool(d.get("auth_ok")),
                "has_cli": bool(d.get("binary_path")),
            }
            for d in (doc.get("diagnostics") or [])
            if str(d.get("kind") or "").lower() != active
            and not (str(d.get("kind") or "").lower() == CLAUDE_DRIVER and not allow_claude)
        ]
        orch_session, native_subs = self._observed_driver_sessions(ws, active)
        orch_deep_link = str(orch_session.get("deep_link") or "") or self._fallback_driver_deep_link(active, orch_diag)
        orch_title = str(orch_session.get("brief_preview") or ws.name)[:64]
        for sub in native_subs:
            if not sub.get("deep_link"):
                sub["deep_link"] = orch_deep_link
        active_w_count = sum(1 for w in workers if w.get("status") not in ("stopped", "error"))
        # Cache hit/miss fields are optional: served from a background-refreshed cache, never parsed here.
        orch_usage_target = orchestrator_target(orch_session, active)
        usage_targets = [worker_target(w["agent_id"], str(w["driver"]).lower()) for w in workers]
        usage_fields = self._usage_monitor.fields(ws, usage_targets + ([orch_usage_target] if orch_usage_target else []))

        # Group workers into Problem Squads + collect 4 Strategic Alerts
        squad_groups: dict[str, list[dict[str, Any]]] = {}
        strategic_alerts: list[dict[str, Any]] = []
        for prob in ws_sig.get("escape_locked") or []:
            strategic_alerts.append({
                "id": f"esc-{prob}", "kind": "escape_zero", "problem": prob,
                "agent_id": "", "label": f"{prob} ESCAPE_LOCK",
            })

        worker_rows: list[dict[str, Any]] = []
        for w in workers:
            drv = str(w["driver"]).lower()
            w_diag = diag_by_kind.get(drv) or {}
            w_dl, w_resume_cmd, w_jump_mode = self._build_worker_jump(ws, w, w_diag, orch_deep_link=orch_deep_link)
            prob = str(w.get("problem") or ws.name)
            streak = int(w.get("no_gain_streak") or 0)
            if streak >= STAGNATION_STREAK_THRESHOLD and w.get("status") not in ("stopped",):
                strategic_alerts.append({
                    "id": f"stag-{w['agent_id']}", "kind": "stagnation", "problem": prob,
                    "agent_id": w["agent_id"], "label": f"{prob} · {w['role_label']} ({streak}/6)",
                })
            row = {
                "agent_id": w["agent_id"],
                "driver": drv,
                "gui_app_path": str(w_diag.get("gui_app_path") or ""),
                "native_session_id": str(w.get("native_session_id") or ""),
                "deep_link": w_dl,
                "resume_command": w_resume_cmd,
                "jump_mode": w_jump_mode,
                "role_label": w["role_label"],
                "status": w["status"],
                "problem": prob,
                "collab_mode": str(w.get("collab_mode") or "solo"),
                "axis": str(w.get("axis") or ""),
                "worktree": w["worktree"],
                "worktree_mode": w["worktree_mode"],
                "branch": w["branch"],
                "turn_count": int(w.get("turn_count") or 0),
                "no_gain_streak": streak,
                "unread_count": int(w.get("unread_for_orch") or 0),
                "last_tool": w.get("last_tool") or "",
                "last_summary": w.get("last_summary") or "",
                "context_percent": w.get("context_percent"),
                "last_commit_sha": w.get("last_commit_sha") or "",
                "error": w.get("error") or "",
                "elapsed_sec": max(0, int(now - float(w.get("last_active_epoch") or now))),
                "last_msg_epoch": float(w.get("last_msg_epoch") or 0.0),
                "last_msg_dir": w.get("last_msg_dir") or "",
                **usage_fields.get(w["agent_id"], {}),
            }
            worker_rows.append(row)
            squad_groups.setdefault(prob, []).append(row)

        pulses = store.recent_pulses(window_sec=12.0, limit=16)
        for p in pulses:
            if p["kind"] in ("breakthrough", "directive", "stagnation"):
                strategic_alerts.append({
                    "id": f"msg-{p['seq']}", "kind": p["kind"], "problem": p.get("problem") or "",
                    "agent_id": p["agent_id"], "label": p["preview"][:40],
                })

        squads: list[dict[str, Any]] = []
        for prob, items in squad_groups.items():
            s_info = scores_map.get(prob) or {}
            modes = {i["collab_mode"] for i in items}
            squad_mode = "orthogonal" if "orthogonal" in modes else ("pipeline" if "pipeline" in modes else ("orthogonal" if len(items) > 1 else "solo"))
            squads.append({
                "problem": prob,
                "difficulty": int(s_info.get("difficulty") or _cfg.DEFAULT_DIFFICULTIES.get(prob, 2)),
                "current_score": float(s_info.get("score") or 0.0),
                "collab_mode": squad_mode,
                "escape_locked": bool(s_info.get("escape_locked", False)),
                "worker_ids": [i["agent_id"] for i in items],
                "active_count": sum(1 for i in items if i["status"] not in ("stopped", "error")),
                "stagnated_count": sum(1 for i in items if i["no_gain_streak"] >= STAGNATION_STREAK_THRESHOLD),
            })
        squads.sort(key=lambda s: (-int(s["escape_locked"]), -s["difficulty"], -s["active_count"], s["problem"]))

        return {
            "orchestrator": {
                "driver": active,
                "display_name": str(orch_diag.get("display_name") or active.upper()),
                "protocol": str(orch_diag.get("protocol") or ""),
                "gui_app_path": str(orch_diag.get("gui_app_path") or ""),
                "deep_link": orch_deep_link,
                "session_title": orch_title,
                "workspace_name": ws.name,
                "active_workers": active_w_count,
                "native_subagents": len(native_subs),
                "squad_count": len(squads),
                "status": "active" if (active_w_count > 0 or any(n["status"] == "running" for n in native_subs)) else "standby",
                **(usage_fields.get(orch_usage_target["key"], {}) if orch_usage_target else {}),
            },
            "ready_drivers": ready_drivers,
            "native_subagents": native_subs,
            "squads": squads,
            "strategic_alerts": strategic_alerts[:12],
            "workers": worker_rows,
            "pulses": pulses,
            "suspensions": self.suspension_rows(ws, all_workers),
            "latest_seq": store.latest_seq(),
            "ts": now,
        }
