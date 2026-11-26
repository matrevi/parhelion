"""Model-authored ledger writes: the ``parhelion_note`` path (4.2 C #3, P06).

A model writes an entry as a new append-only row. The platform owns ``source`` (a worker row is
``worker``, a peer note ``peer``), the ids, the timestamps, the outcome and every measurement, so a
note that names one of those fields is *rejected* instead of silently ignored: a model must learn
that ``confidence=measured`` or ``outcome=better`` are not its to claim.

``confidence`` is capped at ``inferred`` for a model; only a platform row backed by a run record is
``measured``. A status change (or any other change of an existing entry) is a new row whose
``supersedes`` names the old one: ``id`` in a note is read as that target, so the history stays.
"""
from __future__ import annotations

from typing import Any

from . import model, view


class NoteError(model.LedgerError):
    """A note the model may not write (platform field, bad reference, cap)."""


def check_platform_fields(fields: dict[str, Any]) -> None:
    """Raise when a model wrote a field only the platform may set (``id`` is read as the update target)."""
    taken = sorted(k for k in fields if k in model.PLATFORM_ONLY and k != "id")
    if taken:
        raise NoteError(f"{', '.join(taken)} is written by the platform, not by a note; drop the field "
                        f"(a status change is a new row: pass status, and supersedes or id of the old one)")


def target_of(fields: dict[str, Any]) -> str:
    """The entry a note updates: its ``supersedes`` (first) or its ``id``; empty for a new entry."""
    for key in ("supersedes", "id"):
        value = fields.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            return str(value).strip()
    return ""


def prepare(fields: dict[str, Any], rows: list[dict[str, Any]], *, source: str = "worker") -> tuple[str, dict[str, Any]]:
    """``(target, fields)`` of a new note row: platform fields rejected, the update target resolved."""
    check_platform_fields(fields)
    body = {k: v for k, v in fields.items() if k != "id"}
    target = target_of(fields)
    if target:
        head = view.head(rows, target)
        if head is None:
            raise NoteError(f"unknown ledger entry {target!r}: cite the id parhelion_ledger or the seed gave you")
        body["supersedes"] = [target]
        body.setdefault("type", head.get("type"))
        if head.get("type") == "measurement":
            raise NoteError("measurements are written by the platform only")
    elif body.get("supersedes"):
        body["supersedes"] = [str(s) for s in body["supersedes"]] if isinstance(body["supersedes"], list) else [str(body["supersedes"])]
    if body.get("confidence") == "measured":
        body["confidence"] = "inferred"  # a model cannot claim a measured confidence
    if body.get("confidence") == "unverified" and source != "peer":
        body["confidence"] = "speculative"  # unverified marks a teammate's claim (agents/peer.py)
    return target, body


def write(ledger: Any, fields: dict[str, Any], *, source: str = "worker") -> dict[str, Any]:
    """Append (or supersede) one model-written entry and return the stored row."""
    rows = ledger.rows()
    target, body = prepare(fields, rows, source=source)
    entry_type = str(body.pop("type", "") or "")
    if not entry_type:
        raise NoteError("type is required: one of " + ", ".join(model.TYPES))
    if target:
        return ledger.supersede(target, body, source=source)
    return ledger.append(entry_type, body, source=source)


def gate_hypothesis(ledger: Any, fields: dict[str, Any], *, sha: str, source: str = "worker") -> dict[str, Any]:
    """The hypothesis an explicit ``parhelion_submit_gate`` request names (or updates)."""
    rows = ledger.rows()
    target, body = prepare(fields, rows, source=source)
    body.pop("type", None)
    body.setdefault("sha", sha)
    body.setdefault("status", "queued")
    body.setdefault("priority", 3)
    if target:
        return ledger.supersede(target, body, source=source)
    body.setdefault("claim", f"candidate {str(sha)[:7]}")
    return ledger.append("hypothesis", body, source=source)
