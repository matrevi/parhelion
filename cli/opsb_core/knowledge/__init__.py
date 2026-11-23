"""Self-Evolving Triad Knowledge Base package (`breakthrough` + `snippets` + `community`)."""
from __future__ import annotations

from .audit import get_kb_evolution_history, revert_event, rollback_last
from .evolution import compute_confidence, promote_entry, record_kb_entry, record_kb_feedback, validate_evidence
from .query import format_prompt_cache_digest, get_kb_entry, list_kb_entries, query_kb
from .seeding import ensure_triad_kb_seeded, project_readonly_skills
from .store import (
    DEMOTE_MIN_FAILS,
    PROMOTE_MIN_DELTA_RAW,
    PROMOTE_MIN_SPEEDUP,
    VALID_PILLARS,
    VALID_PORTABILITIES,
    VALID_REGIMES,
    VALID_STATUSES,
    VALID_VERDICTS,
    WRITABLE_PILLARS,
    resolve_parhelion_home,
    triad_kb_dirs,
)

__all__ = [
    "DEMOTE_MIN_FAILS",
    "PROMOTE_MIN_DELTA_RAW",
    "PROMOTE_MIN_SPEEDUP",
    "VALID_PILLARS",
    "VALID_PORTABILITIES",
    "VALID_REGIMES",
    "VALID_STATUSES",
    "VALID_VERDICTS",
    "WRITABLE_PILLARS",
    "compute_confidence",
    "ensure_triad_kb_seeded",
    "format_prompt_cache_digest",
    "get_kb_entry",
    "get_kb_evolution_history",
    "list_kb_entries",
    "project_readonly_skills",
    "promote_entry",
    "query_kb",
    "record_kb_entry",
    "record_kb_feedback",
    "resolve_parhelion_home",
    "revert_event",
    "rollback_last",
    "triad_kb_dirs",
    "validate_evidence",
]
