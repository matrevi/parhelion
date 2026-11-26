"""The checkpoint of a compaction: state card first, the tail of the stage result after it.

The old ``_checkpoint`` kept the first characters of the stage result and cut its end, where the
conclusions sit. This one reserves room for the platform state card and the closing line and gives
the rest to the END of the stage result. The total is bounded by ``agents.seed_budget_chars``
(default 8000, clamped to 3000..20000); in this package that budget only sizes the checkpoint.
"""
from __future__ import annotations

from typing import Any

from .. import config as _cfg

CHECKPOINT_TAIL = "Continue from the state card. Do not re-read files you already summarized."
SEED_BUDGET_DEFAULT = 8000
SEED_BUDGET_MIN = 3000
SEED_BUDGET_MAX = 20000
STAGE_FALLBACK_CHARS = 3000  # reply tail used when the worker reported no stage result
MARK = "…"


def seed_budget(cfg: dict[str, Any] | None = None) -> int:
    """``agents.seed_budget_chars`` from the config, clamped; the default when unset or invalid."""
    try:
        doc = cfg if cfg is not None else _cfg.ConfigRepository.load()
    except Exception:
        doc = {}
    section = (doc or {}).get("agents") if isinstance(doc, dict) else None
    value = section.get("seed_budget_chars") if isinstance(section, dict) else None
    try:
        return max(SEED_BUDGET_MIN, min(SEED_BUDGET_MAX, int(value)))
    except (TypeError, ValueError):
        return SEED_BUDGET_DEFAULT


def tail_of(text: str, room: int) -> str:
    """The last ``room`` characters of ``text`` (starting at a line break when one is near), marked as cut."""
    text = text.strip()
    if room <= 0:
        return ""
    if len(text) <= room:
        return text
    cut = text[len(text) - (room - 1):]
    newline = cut.find("\n", 0, 120)
    return MARK + (cut[newline + 1:] if newline >= 0 else cut)


def build(card: str, stage: str, budget: int | None = None) -> str:
    """``card`` + the tail of ``stage`` + the closing line, at most ``budget`` characters."""
    budget = seed_budget() if budget is None else budget
    card = card.strip()
    room = budget - len(card) - len(CHECKPOINT_TAIL) - 2
    return "\n".join(p for p in (card, tail_of(stage, room), CHECKPOINT_TAIL) if p)
