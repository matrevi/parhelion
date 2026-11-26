"""Which formal runs may be compared case by case: the identity of their official inputs.

``suite_version`` bumps on any suite edit, including a final-only local variant (every WA
receipt adds some, scheduler.wa_repro), so pairing runs by version orphaned all earlier local
evidence. Two runs measure the same official workload exactly when every official case has the
same catalog ``key`` (a digest of the frozen oracle, base case, params and seed). The pairing key
is a digest of those ``(id, key)`` pairs. New reports record it as ``pairing_key``; older reports
are never rewritten, and the key is derived from their ``catalog`` at read time. A report without
per-case keys falls back to its suite version, so it only pairs with the same version as before.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def official_key(cases: list[dict[str, Any]]) -> str | None:
    """Digest of the official cases' ``(id, key)``; None when any official case lacks a key."""
    ids = sorted((str(c["id"]), c.get("key")) for c in cases
                 if isinstance(c, dict) and c.get("source") == "official" and c.get("id"))
    if not ids or any(not isinstance(key, str) for _, key in ids):
        return None
    return hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()[:16]


def same_inputs(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two reports measured the same official inputs; by suite version when one lacks keys."""
    ka, kb = pairing_key(a), pairing_key(b)
    if ka.startswith("suite-v") or kb.startswith("suite-v"):
        return a.get("suite_version") == b.get("suite_version")
    return ka == kb


def pairing_key(doc: dict[str, Any]) -> str:
    """The stored key, else one derived from the report's catalog (or cases), else its suite version."""
    stored = doc.get("pairing_key")
    if isinstance(stored, str) and stored:
        return stored
    derived = official_key(doc.get("catalog") or doc.get("cases") or [])
    return derived or f"suite-v{doc.get('suite_version')}"
