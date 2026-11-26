"""Backward-compatibility facade for Self-Evolving Triad Knowledge Base.

Implementation lives in `opsb_core.knowledge.*`.
"""
from __future__ import annotations

from ..knowledge import (
    DEFAULT_EVIDENCE_CLASS,
    DEMOTE_MIN_FAILS,
    EVIDENCE_CLASSES,
    STRONG_EVIDENCE_CLASSES,
    VALID_CATEGORIES,
    PROMOTE_MIN_DELTA_RAW,
    PROMOTE_MIN_SPEEDUP,
    VALID_PILLARS,
    VALID_PORTABILITIES,
    VALID_REGIMES,
    VALID_STATUSES,
    VALID_VERDICTS,
    WRITABLE_PILLARS,
    __all__,
    classify_community_entry_is_hardware,
    compute_confidence,
    evidence_composition,
    ensure_triad_kb_seeded,
    format_prompt_cache_digest,
    get_kb_entry,
    has_strong_evidence,
    get_kb_evolution_history,
    list_kb_entries,
    migrate_evidence_classes,
    migrate_hardware_entries_from_community,
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
