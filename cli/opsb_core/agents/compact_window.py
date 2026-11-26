"""When to compact (4.2 C #7): at a judgement or a stage boundary, not only when the window fills.

Three boundaries trigger a compaction:

``test_terminal``   a ``parhelion_test`` run of this worker reached a terminal state;
``judged``          a judge receipt arrived (a hypothesis just got its conclusion);
``stage_boundary``  the worker filed a stage report (``parhelion_agent_report``).

A boundary only acts once the context is at least ``agents.compact_floor_percent`` of the threshold
(default 40%), so a worker does not pay for a handoff after every short turn. ``manager.compact_threshold``
stays the hard ceiling: at the threshold the context is compacted whatever the boundary. A compaction
never starts while a tool call is running, because the handoff prompt would race the running call.
"""
from __future__ import annotations

from typing import Any

from .. import config as _cfg

BOUNDARIES = ("test_terminal", "judged", "stage_boundary")
FLOOR_PERCENT = 40
FLOOR_MIN = 10
FLOOR_MAX = 100
DEFAULT_PERCENT = 70  # manager.COMPACT_PERCENT: the window's own percentage trigger stays a hard cap


def floor_percent(cfg: dict[str, Any] | None = None) -> int:
    """``agents.compact_floor_percent`` (default 40), clamped to 10..100."""
    try:
        doc = cfg if cfg is not None else _cfg.ConfigRepository.load()
    except Exception:
        doc = {}
    agents = (doc or {}).get("agents") if isinstance(doc, dict) else None
    value = agents.get("compact_floor_percent") if isinstance(agents, dict) else None
    try:
        return max(FLOOR_MIN, min(FLOOR_MAX, int(value)))
    except (TypeError, ValueError):
        return FLOOR_PERCENT


def should_compact(tokens: int | None, window: int | None, *, boundary: str = "", cfg: dict[str, Any] | None = None,
                   tool_in_flight: bool = False, percent: int | None = None,
                   threshold: int | None = None) -> tuple[bool, str]:
    """``(compact?, reason)`` for the current context; ``threshold`` defaults to ``compact_threshold``."""
    if tool_in_flight:
        return False, "tool_in_flight"
    if threshold is None:
        from .manager import compact_threshold
        threshold = compact_threshold(window, cfg)
    used = int(tokens or 0)
    if used >= int(threshold) or int(percent or 0) >= DEFAULT_PERCENT:
        return True, "hard_cap"
    floor = int(threshold) * floor_percent(cfg) // 100
    if used < floor:
        return False, "below_floor"
    if str(boundary or "") in BOUNDARIES:
        return True, str(boundary)
    return False, "no_boundary"
