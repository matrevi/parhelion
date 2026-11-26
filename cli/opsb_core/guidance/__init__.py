"""4.4 stuck guidance (docs/v4.4/stuck-guidance.md): detect a stuck seat, keep a dossier, push one card.

``stuck``    P30 classification of one seat from its ledger, test registry and the gap triage (pure);
``gap``      the per-case gap triage: ratio to tbest, judge points, weighted headroom and tiers (pure);
``dossier``  P31 files under ``<worktree>/.parhelion/dossier/``, written by code only;
``cards``    P32 guide cards: one active card per seat, the card store and the outcome log;
``monitor``  the daemon tick that ties them together, the hot-state fields and the seed line.

Nothing here calls a model or the network, and nothing blocks a tool call: a fault in guidance is
reported on stderr and the worker's tool reply goes out unchanged.
"""
from __future__ import annotations

from typing import Any


def settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """``guidance`` config over the defaults of ``config.DEFAULT_CONFIG`` (a partial section keeps the rest)."""
    from .. import config as _cfg
    base = dict(_cfg.DEFAULT_CONFIG.get("guidance") or {})
    try:
        doc = cfg if cfg is not None else _cfg.ConfigRepository.load()
    except Exception:
        doc = {}
    section = doc.get("guidance") if isinstance(doc, dict) else None
    if isinstance(section, dict):
        base.update(section)
    return base
