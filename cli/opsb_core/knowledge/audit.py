"""Triad KB evolution audit history (`kb-evolution.jsonl`) and clean rollback/revert helpers."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ..config import read_jsonl, utc_now
from .store import (
    VALID_PILLARS,
    _append_evolution_event,
    _new_event_id,
    _with_index_lock,
    triad_kb_dirs,
)


def _reverted_event_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {
        str(r.get("reverted_event_id") or "")
        for r in rows
        if r.get("action") == "revert" and r.get("reverted_event_id")
    }


def get_kb_evolution_history(
    limit: int = 50,
    parhelion_home: Path | None = None,
) -> dict[str, Any]:
    """Read kb-evolution.jsonl newest-first with revert tracking."""
    dirs = triad_kb_dirs(parhelion_home)
    rows = read_jsonl(dirs["evolution_log"])
    reverted_ids = _reverted_event_ids(rows)
    enriched = [
        {**row, "reverted": str(row.get("event_id") or "") in reverted_ids}
        for row in reversed(rows)
    ]
    lim = max(1, int(limit))
    return {
        "ok": True,
        "log_path": str(dirs["evolution_log"]),
        "total_events": len(enriched),
        "events": enriched[:lim],
    }


def revert_event(event_id: str, parhelion_home: Path | None = None) -> dict[str, Any]:
    """Revert a specific mutation event from kb-evolution.jsonl cleanly."""
    dirs = triad_kb_dirs(parhelion_home)
    rows = read_jsonl(dirs["evolution_log"])
    reverted_ids = _reverted_event_ids(rows)
    target = next((r for r in rows if str(r.get("event_id") or "") == event_id.strip()), None)
    if target is None:
        raise ValueError(f"Evolution event {event_id!r} not found.")
    if target.get("action") == "revert":
        raise ValueError(f"Cannot revert a revert event {event_id!r}.")
    if event_id.strip() in reverted_ids:
        raise ValueError(f"Evolution event {event_id!r} was already reverted.")

    pillar = str(target.get("pillar") or "")
    target_entry_id = str(target.get("entry_id") or "")
    if pillar not in VALID_PILLARS:
        raise ValueError(f"Invalid pillar {pillar!r} in evolution event {event_id!r}.")
    p_dir = dirs[pillar]
    idx_path = p_dir / "index.json"

    if target.get("action") == "create":
        def _remove_created(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
            entries = list(cur.get("entries") or [])
            removed = next((e for e in entries if isinstance(e, dict) and e.get("id") == target_entry_id), None)
            cur["entries"] = [e for e in entries if not (isinstance(e, dict) and e.get("id") == target_entry_id)]
            return cur, removed

        removed_entry = _with_index_lock(idx_path, _remove_created)
        if removed_entry and removed_entry.get("doc_path"):
            doc_p = p_dir / str(removed_entry["doc_path"])
            if doc_p.is_file():
                doc_p.unlink()
    else:
        before = target.get("before")
        if not isinstance(before, dict):
            raise ValueError(f"Evolution event {event_id!r} has no 'before' snapshot to restore.")

        def _restore_before(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            entries = list(cur.get("entries") or [])
            replaced = False
            for i, e in enumerate(entries):
                if isinstance(e, dict) and e.get("id") == target_entry_id:
                    entries[i] = copy.deepcopy(before)
                    replaced = True
                    break
            if not replaced:
                entries.append(copy.deepcopy(before))
            cur["entries"] = entries
            return cur, before

        _with_index_lock(idx_path, _restore_before)
        created_snip = target.get("created_snippet_id")
        if created_snip:
            snip_dir = dirs["snippets"]
            snip_idx = snip_dir / "index.json"

            def _remove_snip(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
                entries = list(cur.get("entries") or [])
                rem = next((e for e in entries if isinstance(e, dict) and e.get("id") == created_snip), None)
                cur["entries"] = [e for e in entries if not (isinstance(e, dict) and e.get("id") == created_snip)]
                return cur, rem

            rem_snip = _with_index_lock(snip_idx, _remove_snip)
            if rem_snip and rem_snip.get("doc_path"):
                sp = snip_dir / str(rem_snip["doc_path"])
                if sp.is_file():
                    sp.unlink()

    revert_row = {
        "event_id": _new_event_id(),
        "timestamp": utc_now().isoformat(),
        "action": "revert",
        "pillar": pillar,
        "entry_id": target_entry_id,
        "reverted_event_id": target["event_id"],
        "before": target.get("after"),
        "after": target.get("before"),
    }
    _append_evolution_event(dirs["home"] / ".kb-evolution.lock", dirs["evolution_log"], revert_row)
    return {
        "ok": True,
        "reverted_event_id": target["event_id"],
        "entry_id": target_entry_id,
        "pillar": pillar,
        "event": revert_row,
    }


def rollback_last(parhelion_home: Path | None = None) -> dict[str, Any]:
    """Revert the most recent non-reverted KB mutation event."""
    dirs = triad_kb_dirs(parhelion_home)
    rows = read_jsonl(dirs["evolution_log"])
    reverted_ids = _reverted_event_ids(rows)
    for row in reversed(rows):
        ev_id = str(row.get("event_id") or "")
        if row.get("action") != "revert" and ev_id and ev_id not in reverted_ids:
            return revert_event(ev_id, parhelion_home=dirs["home"])
    raise ValueError("No revertible KB evolution events found.")
