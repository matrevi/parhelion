"""Cursor pagination shared by the MCP list tools and the output bound (4.2 E).

A page is ``offset``/``limit`` over the list a tool returns; ``cursor`` (the previous
``next_cursor``) wins over ``offset`` so a caller can walk pages without doing arithmetic. The
reply carries ``page: {offset, limit, total}`` and, while rows remain, ``next_cursor``. The same
window is what :func:`repage` re-cuts when a page is still above ``mcp.output_cap_bytes``: paging
comes before spilling, so the spill file only holds results that cannot fit as a page.
"""
from __future__ import annotations

import json
import re
from typing import Any

# The list(s) a page advances; the first one present is the page the reply tells the caller about.
PAGE_KEYS: dict[str, tuple[str, ...]] = {
    "parhelion_agent_messages": ("messages",),
    "parhelion_kb_query": ("results", "entries"),
    "parhelion_ledger": ("lines",),
    "parhelion_report": ("receipts", "items"),
}
_CURSOR_RE = re.compile(r"o(\d{1,9})")


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def size_of(value: Any) -> int:
    return len(compact(value).encode("utf-8"))


def encode_cursor(offset: int) -> str:
    return f"o{max(0, int(offset))}"


def decode_cursor(cursor: str) -> int:
    match = _CURSOR_RE.fullmatch(str(cursor or "").strip())
    if match is None:
        raise ValueError("cursor must be a next_cursor value from a previous page")
    return int(match.group(1))


def offset_of(args: dict[str, Any]) -> int:
    """The first row of this page: ``cursor`` wins, then ``offset``; a bad offset starts at 0."""
    cursor = str(args.get("cursor") or "").strip()
    if cursor:
        return decode_cursor(cursor)
    try:
        return max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        return 0


def page_window(args: dict[str, Any], *, default: int, maximum: int) -> tuple[int, int]:
    offset = offset_of(args)
    try:
        limit = int(args.get("limit") or default)
    except (TypeError, ValueError):
        limit = default
    return offset, max(1, min(maximum, limit))


def finish_page(payload: dict[str, Any], key: str, offset: int, total: int) -> dict[str, Any]:
    """Attach ``page`` and, when rows remain, the ``next_cursor`` of the current window."""
    rows = payload.get(key) if isinstance(payload.get(key), list) else []
    payload["page"] = {"offset": offset, "limit": len(rows), "total": max(int(total), offset + len(rows))}
    if offset + len(rows) < payload["page"]["total"]:
        payload["next_cursor"] = encode_cursor(offset + len(rows))
    else:
        payload.pop("next_cursor", None)
    return payload


def paginate(payload: dict[str, Any], key: str, args: dict[str, Any], *, total: int,
             default: int, maximum: int) -> dict[str, Any]:
    """One page of ``payload[key]``; the caller keeps ``total`` (matches before paging)."""
    offset, limit = page_window(args, default=default, maximum=maximum)
    rows = payload.get(key) if isinstance(payload.get(key), list) else []
    payload[key] = rows[offset:offset + limit]
    return finish_page(payload, key, offset, total)


def paginate_keys(payload: dict[str, Any], keys: tuple[str, ...], args: dict[str, Any], *, total: int,
                  default: int, maximum: int) -> dict[str, Any]:
    """One page over mirrored lists (``kb_query`` repeats its results under ``entries``)."""
    offset, limit = page_window(args, default=default, maximum=maximum)
    for key in keys:
        if isinstance(payload.get(key), list):
            payload[key] = payload[key][offset:offset + limit]
    return finish_page(payload, keys[0], offset, total)


def repage(tool: str, payload: Any, cap: int) -> dict[str, Any] | None:
    """A smaller page of the same result, or None when even one row cannot fit under ``cap``."""
    if not isinstance(payload, dict) or not isinstance(payload.get("page"), dict):
        return None
    keys = [key for key in PAGE_KEYS.get(tool, ()) if isinstance(payload.get(key), list)]
    if not keys:
        return None
    page = payload["page"]
    offset, total = int(page.get("offset") or 0), int(page.get("total") or 0)
    rows = payload[keys[0]]
    for keep in range(len(rows) - 1, 0, -1):
        candidate = dict(payload)
        for key in keys:
            candidate[key] = payload[key][:keep]
        candidate["page"] = {**page, "limit": keep}
        if offset + keep < total:
            candidate["next_cursor"] = encode_cursor(offset + keep)
        else:
            candidate.pop("next_cursor", None)
        if size_of(candidate) <= cap:
            return candidate
    return None
