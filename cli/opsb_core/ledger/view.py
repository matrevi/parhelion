"""Current views over ledger rows: fold ``supersedes`` and answer the standard queries.

Rows are plain dicts (a file's rows, or the merged rows of every worker of a problem, each carrying
``worker``). A row is *current* when no later row supersedes it. An old id keeps resolving: ``head``
follows the chain to the row that replaced it, so a handoff or peer note that cites ``L-0003`` still
finds the hypothesis after its status moved to ``L-0010``.

Queries (``query``): ``open`` (open, queued and testing hypotheses, most urgent first), ``unexploited``
(insights no experiment cites), ``failures`` (newest first), ``case:<id>``, ``since:<ref>``.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import model


def ref(row: dict[str, Any]) -> str:
    """The cross-worker reference of a row: ``<worker>/L-<seq>``."""
    return f"{row.get('worker') or ''}/{row['id']}" if row.get("worker") else str(row["id"])


def _target(row: dict[str, Any], value: str, default_worker: str = "") -> tuple[str, str]:
    worker, entry_id = model.parse_ref(value, row.get("worker") or default_worker)
    return worker, entry_id


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("worker") or ""), str(row["id"])


def replaced_by(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], tuple[str, str]]:
    """``(worker, old id) -> (worker, new id)`` for every superseded row."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        for old in row.get("supersedes") or []:
            try:
                out[_target(row, str(old))] = _key(row)
            except model.LedgerError:
                continue
    return out


def current(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(rows)
    gone = replaced_by(rows)
    return [r for r in rows if _key(r) not in gone]


def order_key(row: dict[str, Any]) -> tuple[str, str, int]:
    """Chronological order of rows, also across workers: timestamp, then worker, then sequence."""
    return str(row.get("ts") or ""), str(row.get("worker") or ""), model.seq_of(row.get("id"))


def _with_worker(rows: Iterable[dict[str, Any]], default_worker: str = "") -> list[dict[str, Any]]:
    return [r if r.get("worker") else {**r, "worker": default_worker} for r in rows]


def head(rows: Iterable[dict[str, Any]], entry_id: str, default_worker: str = "") -> dict[str, Any] | None:
    """The current row that ``entry_id`` (``L-3`` or ``worker/L-3``) resolves to."""
    rows = _with_worker(rows, default_worker)
    if not default_worker and len({r.get("worker") for r in rows}) == 1:
        default_worker = str(rows[0].get("worker") or "")  # a single worker's rows: a bare id is theirs
    try:
        key = model.parse_ref(entry_id, default_worker)
    except model.LedgerError:
        return None
    by_key = {_key(r): r for r in rows}
    gone = replaced_by(rows)
    for _ in range(len(rows) + 1):
        if key not in gone:
            return by_key.get(key)
        key = gone[key]
    return None


def chain_ids(rows: Iterable[dict[str, Any]], row: dict[str, Any]) -> set[str]:
    """Every id the current ``row`` answers to: its own and each id it replaced, transitively."""
    by_key = {_key(r): r for r in rows}
    seen = {str(row["id"])}
    todo = [row]
    while todo:
        cur = todo.pop()
        for old in cur.get("supersedes") or []:
            worker, oid = _target(cur, str(old))
            if worker == row.get("worker") and oid not in seen:
                seen.add(oid)
                if (worker, oid) in by_key:
                    todo.append(by_key[(worker, oid)])
    return seen


def sha_match(a: Any, b: Any) -> bool:
    x, y = str(a or "").lower(), str(b or "").lower()
    n = min(len(x), len(y))
    return n >= 7 and x[:n] == y[:n]


def of_type(rows: Iterable[dict[str, Any]], entry_type: str) -> list[dict[str, Any]]:
    return [r for r in current(rows) if r.get("type") == entry_type]


def open_hypotheses(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    hyps = [r for r in of_type(rows, "hypothesis") if r.get("status") in model.OPEN_STATUSES]
    return sorted(hyps, key=lambda r: (int(r.get("priority") or 3), str(r.get("ts") or "")))


def _cited(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """(worker, id) of everything an experiment cites in ``links`` or ``hypothesis_id``."""
    out: set[tuple[str, str]] = set()
    for exp in (r for r in rows if r.get("type") == "experiment"):
        for value in [*(exp.get("links") or []), exp.get("hypothesis_id")]:
            if value:
                try:
                    out.add(_target(exp, str(value)))
                except model.LedgerError:
                    continue
    return out


def unexploited_insights(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(rows)
    cited = _cited(rows)
    out = []
    for ins in of_type(rows, "insight"):
        if ins.get("exploited_by"):
            continue
        if any((ins.get("worker") or "", i) in cited for i in chain_ids(rows, ins)):
            continue
        out.append(ins)
    return out


def failures(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(of_type(rows, "failure"), key=order_key, reverse=True)


def negatives(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dead ends the platform recorded: experiments with a negative outcome and refuted hypotheses, newest first."""
    cur = current(rows)
    hit = [r for r in cur if (r.get("type") == "experiment" and r.get("outcome") in model.NEGATIVE_OUTCOMES)
           or (r.get("type") == "hypothesis" and r.get("status") == "refuted")]
    return sorted(hit, key=order_key, reverse=True)


def for_case(rows: Iterable[dict[str, Any]], case_id: str) -> list[dict[str, Any]]:
    cid = str(case_id)
    out = []
    for r in current(rows):
        scope = r.get("scope") if isinstance(r.get("scope"), dict) else {}
        per_case = r.get("per_case_us") if isinstance(r.get("per_case_us"), dict) else {}
        delta = r.get("delta_vs_baseline") if isinstance(r.get("delta_vs_baseline"), dict) else {}
        if (cid in [str(c) for c in scope.get("cases") or []] or cid in [str(c) for c in r.get("cases") or []]
                or cid in per_case or cid in (delta.get("cases") or {})):
            out.append(r)
    return out


def since(rows: Iterable[dict[str, Any]], entry_ref: str) -> list[dict[str, Any]]:
    """Current rows written after the referenced row (by timestamp, then worker and sequence)."""
    rows = list(rows)
    try:
        worker, eid = model.parse_ref(entry_ref)
    except model.LedgerError:
        return []
    anchor = next((r for r in rows if r["id"] == eid and (not worker or r.get("worker") == worker)), None)
    if anchor is None:
        return []
    return [r for r in current(rows) if order_key(r) > order_key(anchor)]


def for_sha(rows: Iterable[dict[str, Any]], entry_type: str, sha: str) -> list[dict[str, Any]]:
    return [r for r in of_type(rows, entry_type) if sha_match(r.get("sha"), sha)]


def query(rows: Iterable[dict[str, Any]], spec: str) -> list[dict[str, Any]]:
    """Run one named view: ``open``, ``unexploited``, ``failures``, ``case:<id>``, ``since:<ref>``."""
    name, _, arg = str(spec or "").strip().partition(":")
    name = name.strip().lower()
    if name in ("open", "hypotheses"):
        return open_hypotheses(rows)
    if name in ("unexploited", "insights_unexploited"):
        return unexploited_insights(rows)
    if name == "failures":
        return failures(rows)
    if name == "case" and arg.strip():
        return for_case(rows, arg.strip())
    if name == "since" and arg.strip():
        return since(rows, arg.strip())
    raise model.LedgerError("view must be open, unexploited, failures, case:<id> or since:<ref>")
