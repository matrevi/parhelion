"""Prompt-shape fingerprints and cache-miss attribution for canonical model-call records.

Modeled on Reasonix ``CaptureShape``/``CompareShape`` (docs/v4.2/research/token-economy.md):
each call carries fingerprints of the system prompt and of the normalized tool table, plus
flags for a new native context (``session_start``) and a declared history rewrite
(``rewrite``). A call is a *miss event* when the provider did not reuse the prefix the
previous call had already paid for (``lost_prefix`` above a slack), or when it is the
agent's first call. Every miss event gets exactly one reason, checked in this order:

``cold_start``               the agent's first recorded call;
``system_changed``           the system-prompt fingerprint differs from the previous call;
``tools_changed``            the tool-table fingerprint differs;
``compaction_or_truncation`` a new native context after a handoff, or a declared rewrite;
``ttl_gap``                  more than ``ttl_sec`` since the previous call;
``unknown``                  none of the above is visible in the logs.

Fingerprints that a driver cannot observe are ``None`` and never count as a change, so a
driver whose logs lack the system prompt can only reach the reasons its logs support.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Iterable

REASONS = ("cold_start", "system_changed", "tools_changed", "compaction_or_truncation", "ttl_gap", "unknown")
#: Per-driver prompt-cache lifetime used for ``ttl_gap`` (seconds). Anthropic's ephemeral
#: cache lives 5 minutes; DeepSeek's disk cache lives hours, so an hour is conservative.
DEFAULT_TTL_SEC = {"claude": 300, "antigravity": 300, "deepseek": 3600, "codex": 3600}
FALLBACK_TTL_SEC = 600
#: A continuing prefix may lose a few 64-token cache blocks to rounding; only a larger loss
#: is a miss event. The slack is max(MIN_SLACK_TOKENS, SLACK_RATIO * previous prompt).
MIN_SLACK_TOKENS = 256
SLACK_RATIO = 0.005


def fingerprint(value: Any) -> str:
    """Short stable digest of a string or JSON-serializable value."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def normalize_tools(tools: Any) -> list[dict[str, Any]]:
    """Copy of a tool table sorted by name, keeping name/description/parameters only.

    Sorting removes registration-order noise (Reasonix ``NormalizeToolSchemas``) so only a
    real schema change moves the fingerprint.
    """
    out: list[dict[str, Any]] = []
    for item in tools if isinstance(tools, list) else []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        out.append({
            "name": str(fn.get("name") or ""),
            "description": str(fn.get("description") or ""),
            "parameters": fn.get("parameters") or fn.get("input_schema") or fn.get("inputSchema") or {},
        })
    out.sort(key=lambda t: (t["name"], t["description"], json.dumps(t["parameters"], sort_keys=True, default=str)))
    return out


def tools_fingerprint(tools: Any) -> str | None:
    norm = normalize_tools(tools)
    return fingerprint(norm) if norm else None


def prompt_tokens(call: dict[str, Any]) -> int:
    return int(call.get("input_miss") or 0) + int(call.get("cache_read") or 0) + int(call.get("cache_write") or 0)


def _epoch(ts: Any) -> float | None:
    if isinstance(ts, (int, float)):
        return float(ts)
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def ttl_for(driver: str, override: Any = None) -> float:
    if isinstance(override, (int, float)) and override > 0:
        return float(override)
    if isinstance(override, dict) and isinstance(override.get(driver), (int, float)):
        return float(override[driver])
    return float(DEFAULT_TTL_SEC.get(str(driver or "").lower(), FALLBACK_TTL_SEC))


def compare(prev: dict[str, Any] | None, cur: dict[str, Any], *, ttl_sec: float) -> str:
    """Name the reason for a miss event on ``cur`` given the previous call (CompareShape)."""
    if prev is None:
        return "cold_start"
    for key, reason in (("system_fp", "system_changed"), ("tools_fp", "tools_changed")):
        a, b = prev.get(key), cur.get(key)
        if a is not None and b is not None and a != b:
            return reason
    if cur.get("session_start") or cur.get("rewrite"):
        return "compaction_or_truncation"
    t0, t1 = _epoch(prev.get("ts")), _epoch(cur.get("ts"))
    if t0 is not None and t1 is not None and t1 - t0 > ttl_sec:
        return "ttl_gap"
    return "unknown"


def classify(calls: Iterable[dict[str, Any]], *, ttl_sec: float | None = None,
             prev: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Annotate calls in order with ``lost_prefix``, ``miss`` and ``miss_reason`` (in place).

    ``prev`` continues an earlier classification (incremental parsing). Records whose usage
    is unavailable are skipped and do not break the chain.
    """
    out: list[dict[str, Any]] = []
    last = prev
    for call in calls:
        if call.get("usage") == "unavailable":
            out.append(call)
            continue
        ttl = ttl_sec if ttl_sec is not None else ttl_for(str(call.get("driver") or ""))
        if last is None:
            lost = 0
            miss = True
        else:
            before = prompt_tokens(last)
            lost = max(0, before - int(call.get("cache_read") or 0))
            # A changed fingerprint whose prefix still hit (cache_read kept up) is not a miss.
            miss = lost > max(MIN_SLACK_TOKENS, SLACK_RATIO * before)
        call["lost_prefix"] = lost
        call["miss"] = bool(miss)
        call["miss_reason"] = compare(last, call, ttl_sec=ttl) if miss else ""
        out.append(call)
        last = call
    return out


def empty_summary() -> dict[str, Any]:
    return {
        "calls": 0, "input_miss": 0, "cache_read": 0, "cache_write": 0, "output": 0,
        "miss_events": 0, "reasons": {r: 0 for r in REASONS}, "last_miss_reason": "",
        "last_ts": None, "model": "",
    }


def add_to_summary(summary: dict[str, Any], calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    for c in calls:
        if c.get("usage") == "unavailable":
            continue
        summary["calls"] += 1
        for k in ("input_miss", "cache_read", "cache_write", "output"):
            summary[k] += int(c.get(k) or 0)
        if c.get("miss"):
            summary["miss_events"] += 1
            reason = c.get("miss_reason") or "unknown"
            summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
            summary["last_miss_reason"] = reason
        summary["last_ts"] = c.get("ts") or summary["last_ts"]
        summary["model"] = c.get("model") or summary["model"]
    return summary


def hit_pct(summary: dict[str, Any]) -> float | None:
    """``cache_read / (cache_read + input_miss + cache_write)`` in percent; None without data.

    Cache writes are uncached input too (Anthropic bills them above the base rate); DeepSeek
    and Codex report none, so for them this is exactly ``cache_read / (cache_read + input_miss)``.
    """
    denom = int(summary.get("cache_read") or 0) + int(summary.get("input_miss") or 0) + int(summary.get("cache_write") or 0)
    return round(100.0 * int(summary.get("cache_read") or 0) / denom, 1) if denom > 0 else None


def summarize(calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    summary = add_to_summary(empty_summary(), calls)
    summary["cache_hit_pct"] = hit_pct(summary)
    return summary
