"""Append-only JSONL storage of the ledger: ``<contest>/results/ledger/<problem>/<worker>.jsonl``.

One file per worker. An entry is never edited: a change is a new row whose ``supersedes`` lists the
rows it replaces (``supersede`` builds it from the current head). Appends hold an exclusive flock,
so every ``Ledger`` handle (threads, daemon, CLI) numbers entries ``L-<seq>`` without gaps or
repeats. Rows written by the platform carry an ``ekey`` (``test:<request_id>:exp``,
``receipt:<submission_id>``, ...) and are idempotent on it: replaying a test result or a receipt
returns the stored row and writes nothing.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterator

from . import model, view

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _name(value: str, what: str) -> str:
    if not NAME_RE.match(str(value or "")) or ".." in str(value):
        raise model.LedgerError(f"invalid {what} name {value!r}")
    return str(value)


def ledger_root(workspace: Path | str) -> Path:
    return Path(workspace).expanduser() / "results" / "ledger"


def problem_dir(workspace: Path | str, problem: str) -> Path:
    return ledger_root(workspace) / _name(problem, "problem")


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Every parsable row of one ledger file; a torn or foreign line is skipped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and model.ID_RE.match(str(row.get("id") or "")):
            rows.append(row)
    return rows


def workers(workspace: Path | str, problem: str) -> list[str]:
    root = problem_dir(workspace, problem)
    return sorted(p.stem for p in root.glob("*.jsonl")) if root.is_dir() else []


def read_problem(workspace: Path | str, problem: str) -> list[dict[str, Any]]:
    """The rows of every worker of one problem, merged by timestamp (ties by worker, then seq)."""
    rows: list[dict[str, Any]] = []
    for worker in workers(workspace, problem):
        for row in read_rows(problem_dir(workspace, problem) / f"{worker}.jsonl"):
            rows.append({**row, "worker": row.get("worker") or worker})
    rows.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("worker")), model.seq_of(r.get("id"))))
    return rows


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _with_key(existing: list[dict[str, Any]], worker: str, ekey: str | None) -> dict[str, Any] | None:
    hit = next((r for r in existing if r.get("ekey") == ekey), None) if ekey else None
    return None if hit is None else {**hit, "worker": worker, "duplicate": True}


def new_row(existing: list[dict[str, Any]], worker: str, problem: str, ts: str, entry_type: str,
            fields: dict[str, Any], source: str, *, validate_as: str | None = None) -> dict[str, Any]:
    """The next row of a ledger: validated, numbered after ``existing``, fitted to the line limit."""
    body = model.build(entry_type, fields, source=validate_as or source)
    body["source"] = source
    seq = max((model.seq_of(r.get("id")) for r in existing), default=0) + 1
    return model.fit({"id": f"L-{seq:04d}", "ts": ts, "worker": worker, "problem": problem, **body})


def successor(existing: list[dict[str, Any]], worker: str, entry_id: str, changes: dict[str, Any], source: str,
              ekey: str | None) -> tuple[str, dict[str, Any]]:
    """``(type, fields)`` of the row that replaces the current head of ``entry_id``.

    A model-written change (``source`` not ``platform``) cannot touch platform-owned fields or the
    ``measured`` confidence.
    """
    head = view.head(existing, entry_id, worker)
    if head is None:
        raise model.LedgerError(f"unknown ledger entry {entry_id}")
    if head["type"] == "measurement" and source != "platform":
        raise model.LedgerError("measurements are written by the platform only")
    patch = dict(changes) if source == "platform" else model.worker_fields(changes)
    if source != "platform" and patch.get("confidence") == "measured":
        patch["confidence"] = "inferred"
    keep = {k: v for k, v in head.items()
            if k not in ("id", "ts", "worker", "problem", "type", "source", "supersedes", "ekey", "truncated",
                         "duplicate")}
    fields = {**keep, **patch, "supersedes": [head["id"]]}
    if ekey:
        fields["ekey"] = ekey
    return head["type"], fields


class Ledger:
    """One worker's ledger of one problem, stored in ``<contest>/results/ledger/<problem>/<worker>.jsonl``."""

    def __init__(self, workspace: Path | str, problem: str, worker: str, *,
                 clock: Callable[[], str] | None = None) -> None:
        self.workspace = Path(workspace).expanduser()
        self.problem = _name(problem, "problem")
        self.worker = _name(worker, "worker")
        self.path = problem_dir(self.workspace, self.problem) / f"{self.worker}.jsonl"
        self._clock = clock or _now

    def rows(self) -> list[dict[str, Any]]:
        return [{**r, "worker": r.get("worker") or self.worker} for r in read_rows(self.path)]

    def find(self, ekey: str) -> dict[str, Any] | None:
        """The stored row of an evidence key, if the platform already wrote it."""
        hit = next((r for r in read_rows(self.path) if r.get("ekey") == ekey), None)
        return None if hit is None else {**hit, "worker": self.worker}

    def _write(self, fd: int, existing: list[dict[str, Any]], entry_type: str, fields: dict[str, Any],
               source: str, *, validate_as: str | None = None) -> dict[str, Any]:
        row = new_row(existing, self.worker, self.problem, self._clock(), entry_type, fields, source,
                      validate_as=validate_as)
        os.write(fd, (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        return row

    def append(self, entry_type: str, fields: dict[str, Any], *, source: str = "platform",
               ekey: str | None = None) -> dict[str, Any]:
        """Add an entry and return it; with ``ekey`` an existing row of that key is returned instead."""
        with _exclusive(self.path) as fd:
            existing = read_rows(self.path)
            return _with_key(existing, self.worker, ekey) or self._write(
                fd, existing, entry_type, {**fields, **({"ekey": ekey} if ekey else {})}, source)

    def supersede(self, entry_id: str, changes: dict[str, Any], *, source: str = "platform",
                  ekey: str | None = None) -> dict[str, Any]:
        """Replace the current head of ``entry_id`` by a new row: its fields updated with ``changes``."""
        with _exclusive(self.path) as fd:
            existing = read_rows(self.path)
            hit = _with_key(existing, self.worker, ekey)
            if hit is not None:
                return hit
            entry_type, fields = successor(existing, self.worker, entry_id, changes, source, ekey)
            return self._write(fd, existing, entry_type, fields, source, validate_as="platform")


class MemoryLedger:
    """The same API over a list: the replay benchmark rebuilds a ledger from logged events with it."""

    def __init__(self, worker: str = "replay", problem: str = "replay", clock: Callable[[], str] | None = None) -> None:
        self.worker, self.problem, self._rows = worker, problem, []
        self.clock = clock or _now  # the replay sets it to the logged time of each event

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows]

    def find(self, ekey: str) -> dict[str, Any] | None:
        return next((dict(r) for r in self._rows if r.get("ekey") == ekey), None)

    def append(self, entry_type: str, fields: dict[str, Any], *, source: str = "platform",
               ekey: str | None = None) -> dict[str, Any]:
        hit = _with_key(self._rows, self.worker, ekey)
        if hit is not None:
            return hit
        row = new_row(self._rows, self.worker, self.problem, self.clock(), entry_type,
                      {**fields, **({"ekey": ekey} if ekey else {})}, source)
        self._rows.append(row)
        return dict(row)

    def supersede(self, entry_id: str, changes: dict[str, Any], *, source: str = "platform",
                  ekey: str | None = None) -> dict[str, Any]:
        hit = _with_key(self._rows, self.worker, ekey)
        if hit is not None:
            return hit
        entry_type, fields = successor(self._rows, self.worker, entry_id, changes, source, ekey)
        row = new_row(self._rows, self.worker, self.problem, self.clock(), entry_type, fields, source,
                      validate_as="platform")
        self._rows.append(row)
        return dict(row)
