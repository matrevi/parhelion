"""Backward-compatibility facade for Self-Evolving Triad Knowledge Base.

Implementation lives in `opsb_core.knowledge.*`.
"""
from __future__ import annotations

from ..knowledge import (
    DEMOTE_MIN_FAILS,
    PROMOTE_MIN_DELTA_RAW,
    PROMOTE_MIN_SPEEDUP,
    VALID_PILLARS,
    VALID_PORTABILITIES,
    VALID_REGIMES,
    VALID_STATUSES,
    VALID_VERDICTS,
    WRITABLE_PILLARS,
    __all__,
    compute_confidence,
    ensure_triad_kb_seeded,
    format_prompt_cache_digest,
    get_kb_entry,
    get_kb_evolution_history,
    list_kb_entries,
    project_readonly_skills,
    promote_entry,
    query_kb,
    record_kb_entry,
    record_kb_feedback,
    resolve_parhelion_home,
    revert_event,
    rollback_last,
    triad_kb_dirs,
    validate_evidence,
)
