"""Turn platform state changes into hub events: bus messages, agent states, test runs, receipts.

Every scanner is cursor-based, so a scan after a quiet period reports everything that changed
since the previous one (intermediate agent or test states may collapse into the latest one):

- ``inbox``: new bus rows read through ``AgentBusStore.list_messages`` after the last row seen
  (the cursor becomes the last returned seq, so a store that resolves stale cursors keeps working);
- ``agent``: ``status`` transitions in ``list_agents``;
- ``test``: status transitions of test-run registry records (active runs are refreshed so a
  finished subprocess settles; per-case progress feeds ``waiting_for`` but wakes nobody);
- ``judge``: newly judged rows in ``results/submit-queue-outcome.jsonl``, once per ``submission_id``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..services import test_runs
from .bus_store import AgentBusStore

BUS_PAGE = 200
PREVIEW_CHARS = 140
STRATEGIC = frozenset({"breakthrough", "stagnation", "escape_zero", "question", "error", "directive"})
JUDGED_FALLBACK = frozenset({"Pass", "Accepted", "Fail", "Wrong Answer", "Compile Error", "Runtime Error",
                             "Time Limit Exceeded", "Memory Limit Exceeded", "System Error"})


def _preview(text: Any, limit: int = PREVIEW_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _judged(row: dict[str, Any]) -> bool:
    try:
        from ..services.judge_score_fetch import receipts_module
        return bool(receipts_module().judged(row))
    except Exception:
        return str(row.get("status") or "") in JUDGED_FALLBACK


def ledger_path(workspace: Path) -> Path:
    return workspace / "results" / "submit-queue-outcome.jsonl"


class SourceScanner:
    """Holds the cursors of the four event sources for one workspace."""

    def __init__(self, workspace: Path, store: AgentBusStore) -> None:
        self.ws = Path(workspace).expanduser().resolve()
        self.store = store
        self.bus_seq = store.latest_seq()
        self.agents = {a["agent_id"]: str(a["status"]) for a in store.list_agents(limit=-1)}
        self.tests: dict[str, dict[str, Any]] = {}
        for rid, mtime, rec in self._registry(prime=True):
            self.tests[rid] = self._test_state(rec, mtime)
        path = ledger_path(self.ws)
        self.ledger_offset = path.stat().st_size if path.is_file() else 0
        self.ledger_sig: tuple[int, int] | None = None
        self.receipt_ids = {str(r.get("submission_id")) for r in self._read_ledger(0, self.ledger_offset)[0]
                            if r.get("submission_id") and _judged(r)}

    # ------------------------------------------------------------------ scans
    def scan(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for part in (self._bus, self._agents, self._tests, self._judge):
            try:
                out.extend(part())
            except Exception:
                continue  # one broken source must not silence the others
        return out

    def _bus(self) -> list[dict[str, Any]]:
        out = []
        while True:
            msgs = self.store.list_messages(since_seq=self.bus_seq, limit=BUS_PAGE)
            if not msgs:
                return out
            self.bus_seq = int(msgs[-1]["seq"])
            for m in msgs:
                direction, kind = str(m.get("direction") or ""), str(m.get("kind") or "")
                if direction in ("orch_to_worker", "squad_to_worker"):
                    audience, who = "worker", ""
                elif direction == "worker_to_orch" or kind == "directive":
                    audience, who = "orchestrator", f" {m.get('agent_id')}"
                else:
                    continue  # squad notes and lifecycle rows are not addressed to a waiter
                out.append({"source": "inbox", "agent_id": str(m.get("agent_id") or ""),
                            "problem": str(m.get("problem") or ""), "ref": str(m["seq"]), "kind": kind,
                            "audience": audience, "strategic": kind in STRATEGIC,
                            "line": f"inbox #{m['seq']}{who} {kind}: {_preview(m.get('body'))}"})
            if len(msgs) < BUS_PAGE:
                return out

    def _agents(self) -> list[dict[str, Any]]:
        out = []
        current = {a["agent_id"]: a for a in self.store.list_agents(limit=-1)}
        for aid, a in current.items():
            old, new = self.agents.get(aid), str(a["status"])
            if old == new:
                continue
            why = f" ({_preview(a.get('error'), 80)})" if new == "error" and a.get("error") else ""
            out.append({"source": "agent", "agent_id": aid, "problem": str(a.get("problem") or ""), "ref": aid,
                        "kind": new, "line": f"agent {aid} {a.get('problem') or '-'} {old or 'new'}→{new}{why}"})
        for aid in set(self.agents) - set(current):
            out.append({"source": "agent", "agent_id": aid, "problem": "", "ref": aid, "kind": "removed",
                        "line": f"agent {aid} removed"})
        self.agents = {aid: str(a["status"]) for aid, a in current.items()}
        return out

    def _registry(self, *, prime: bool = False):
        root = test_runs.registry_dir(self.ws)
        try:
            entries = list(os.scandir(root))
        except OSError:
            return
        for entry in entries:
            rid = entry.name[:-5]
            if not entry.name.endswith(".json") or len(rid) != 32:
                continue
            try:
                mtime = entry.stat().st_mtime_ns
            except OSError:
                continue
            prev = None if prime else self.tests.get(rid)
            active = prev is not None and prev["status"] in test_runs.ACTIVE
            if prev is not None and prev["mtime"] == mtime and not active:
                continue
            try:
                rec = (test_runs.refresh(self.ws, rid) if (active or (prev is None and not prime))
                       else json.loads(Path(entry.path).read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                continue
            if isinstance(rec, dict):
                yield rid, mtime, rec

    @staticmethod
    def _test_state(rec: dict[str, Any], mtime: int) -> dict[str, Any]:
        prog = rec.get("progress") if isinstance(rec.get("progress"), dict) else {}
        return {"mtime": mtime, "status": str(rec.get("status") or ""), "agent_id": str(rec.get("agent_id") or ""),
                "problem": str(rec.get("problem") or ""), "level": str(rec.get("level") or ""),
                "sha7": str(rec.get("sha") or "")[:7], "done": prog.get("cases_done"), "selected": prog.get("selected")}

    def _tests(self) -> list[dict[str, Any]]:
        out = []
        for rid, mtime, rec in list(self._registry()):
            prev = self.tests.get(rid)
            state = self._test_state(rec, mtime)
            self.tests[rid] = state
            if prev is not None and prev["status"] == state["status"]:
                continue
            if state["status"] in test_runs.TERMINAL:
                line = f"test {rid[:8]} {_preview(test_runs.compact_line(rec), 200)}"
            else:
                line = f"test {rid[:8]} {state['problem']} {state['sha7']} {state['level']} {state['status']}"
            out.append({"source": "test", "agent_id": state["agent_id"], "problem": state["problem"], "ref": rid,
                        "kind": state["status"], "line": line})
        return out

    def _read_ledger(self, start: int, end: int | None = None) -> tuple[list[dict[str, Any]], int]:
        path = ledger_path(self.ws)
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read() if end is None else fh.read(max(0, end - start))
        except OSError:
            return [], start
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return [], start
        rows = []
        for line in chunk[: cut + 1].splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows, start + cut + 1

    def _judge(self) -> list[dict[str, Any]]:
        path = ledger_path(self.ws)
        try:
            st = path.stat()
        except OSError:
            return []
        sig = (st.st_size, st.st_mtime_ns)
        if sig == self.ledger_sig:
            return []
        self.ledger_sig = sig
        if st.st_size < self.ledger_offset:
            self.ledger_offset = 0  # rewritten journal: seen ids keep replays silent
        rows, self.ledger_offset = self._read_ledger(self.ledger_offset)
        out = []
        for row in rows:
            sid = str(row.get("submission_id") or "")
            if not sid or sid in self.receipt_ids or not _judged(row):
                continue
            self.receipt_ids.add(sid)
            title, sha7 = str(row.get("title") or ""), str(row.get("sha") or "")[:7]
            out.append({"source": "judge", "agent_id": str(row.get("agent_id") or ""), "problem": title, "ref": sid,
                        "kind": str(row.get("status") or ""),
                        "line": f"judge {title} {sha7} {row.get('status')} score {row.get('score')} submission {sid}"})
        return out

    # ------------------------------------------------------------------ descriptions
    def active_tests(self, agent_id: str | None = None, problem: str | None = None) -> list[str]:
        out = []
        for rid, t in list(self.tests.items()):  # the scan thread may be updating the map
            if t["status"] not in test_runs.ACTIVE or (agent_id and t["agent_id"] != agent_id):
                continue
            if problem and t["problem"].lower() != problem.lower():
                continue
            prog = f", {t['done']}/{t['selected']} cases" if t.get("selected") else ""
            out.append(f"{t['problem']} {t['level']} run {rid[:8]} ({t['status']}{prog})")
        return sorted(out)

    def waiting_for(self, role: str, agent_id: str | None, problem: str, sources: tuple[str, ...]) -> str:
        parts = []
        if role == "worker":
            if "test" in sources:
                runs = self.active_tests(agent_id)
                parts.append(f"your {runs[0]}" + (f" and {len(runs) - 1} more" if len(runs) > 1 else "")
                             if runs else "a test of yours (none is running)")
            if "inbox" in sources:
                parts.append(f"messages to {agent_id}")
            if "judge" in sources and problem:
                parts.append(f"receipts for {problem}")
            if "agent" in sources:
                parts.append("a change of your seat status")
        else:
            states = list(self.agents.values())
            if "inbox" in sources:
                parts.append(f"worker messages ({states.count('working')} working, {states.count('idle')} idle)")
            if "test" in sources:
                runs = self.active_tests(problem=problem or None)
                parts.append(f"{len(runs)} test run(s) in flight" + (f", e.g. {runs[0]}" if runs else ""))
            if "judge" in sources:
                parts.append("judge receipts" + (f" for {problem}" if problem else ""))
            if "agent" in sources:
                parts.append("worker status changes")
        return "Waiting for " + "; ".join(parts) + "." if parts else "Waiting."

    def snapshot(self, role: str, agent_id: str | None) -> dict[str, Any]:
        if role == "worker":
            return {"status": self.agents.get(agent_id or "", "unknown"), "tests_active": self.active_tests(agent_id)[:5],
                    "bus_latest_seq": self.bus_seq}
        counts: dict[str, int] = {}
        for status in self.agents.values():
            counts[status] = counts.get(status, 0) + 1
        unread = sum(int(a.get("unread_for_orch") or 0) for a in self.store.list_agents(limit=-1))
        return {"agents": counts, "unread_for_orch": unread, "tests_active": self.active_tests()[:5],
                "bus_latest_seq": self.bus_seq}
