"""4.4 P34: which Harness drivers the platform may use for workers and scouts (the user ticks them).

``agents.available_drivers`` in ``config.json`` is a list of driver kinds. The orchestrator chooses only
among these when it opens seats, dispatches an external worker or answers a scout request; every spawn
path checks the list and refuses another driver with a reason. During development the default is the
DeepSeek Harness alone, set in the deployment config (``opsb driver available --set deepseek``). Without
the key every driver except Claude is available (the 4.3 behaviour). The older Claude-only switch
(``allow_claude_workers`` or ``PARHELION_ALLOW_CLAUDE_WORKERS=1``) still adds Claude on top of the list.
"""
from __future__ import annotations

import os
from typing import Any

CLAUDE = "claude"


def _config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    if cfg is not None:
        return cfg
    try:
        from .base import load_parhelion_config
        return load_parhelion_config()
    except Exception:
        return {}


def _legacy_claude(cfg: dict[str, Any]) -> bool:
    env = str(os.environ.get("PARHELION_ALLOW_CLAUDE_WORKERS", "")).strip().lower()
    return env in ("1", "true", "yes", "on") or bool(cfg.get("allow_claude_workers"))


def available(cfg: dict[str, Any] | None = None) -> list[str]:
    """The ticked driver kinds, in the order the user listed them (unknown kinds are dropped)."""
    from .registry import VALID_DRIVERS
    doc = _config(cfg)
    agents = doc.get("agents") if isinstance(doc.get("agents"), dict) else {}
    raw = agents.get("available_drivers")
    kinds = ([str(k).strip().lower() for k in raw] if isinstance(raw, list)
             else [k for k in VALID_DRIVERS if k != CLAUDE])
    out = [k for k in dict.fromkeys(kinds) if k in VALID_DRIVERS]
    if _legacy_claude(doc) and CLAUDE not in out:
        out.append(CLAUDE)
    return out


def is_available(kind: str, cfg: dict[str, Any] | None = None) -> bool:
    return str(kind or "").strip().lower() in available(cfg)


def refusal(kind: str, cfg: dict[str, Any] | None = None) -> str:
    """The reason a spawn of ``kind`` is refused ("" when the driver is available)."""
    if is_available(kind, cfg):
        return ""
    return (f"Harness '{kind}' is not available: the user enabled only {', '.join(available(cfg)) or 'none'} "
            "(Settings > Drivers, or `opsb driver available --set ...`).")


def set_available(kinds: list[str]) -> dict[str, Any]:
    """Store the ticked list (validated) in ``config.json`` and return it."""
    from .base import load_parhelion_config, save_parhelion_config
    from .registry import VALID_DRIVERS
    clean = [str(k).strip().lower() for k in kinds if str(k).strip()]
    unknown = [k for k in clean if k not in VALID_DRIVERS]
    if unknown:
        raise ValueError(f"Unknown driver(s): {', '.join(unknown)}. Supported: {', '.join(VALID_DRIVERS)}")
    cfg = load_parhelion_config()
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    cfg["agents"] = {**agents, "available_drivers": list(dict.fromkeys(clean))}
    save_parhelion_config(cfg)
    return {"ok": True, "available_drivers": available(cfg)}


def choose(preferred: str = "", cfg: dict[str, Any] | None = None) -> str:
    """``preferred`` when it is available, else the first available driver ("" when none is)."""
    kinds = available(cfg)
    want = str(preferred or "").strip().lower()
    return want if want in kinds else (kinds[0] if kinds else "")
