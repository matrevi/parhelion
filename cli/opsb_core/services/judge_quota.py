"""Judge-side daily quota cache.

The local receipt count is not the judge's usage: submissions sent outside the Parhelion
queue (manual `--force`, another agent's shell) never write a receipt, so the UI showed
"3/50" while the judge had accepted 13 that day. This cache is written from the judge's own
data (submission history or a 429 body) and is authoritative for the Beijing calendar day.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from typing import Any

_BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def beijing_date(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(_BEIJING).strftime("%Y-%m-%d")


def quota_path(workspace: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(workspace).expanduser() / "results" / ".judge-quota.json"


def write_quota(workspace: pathlib.Path | str, *, used: int, limit: int = 50, source: str = "") -> bool:
    try:
        doc = {
            "beijing_date": beijing_date(),
            "used": max(0, int(used)),
            "limit": max(1, int(limit)),
            "remaining": max(0, int(limit) - int(used)),
            "source": source,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        path = quota_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def read_quota(workspace: pathlib.Path | str) -> dict[str, Any] | None:
    """Return the cache only when it belongs to the current Beijing day."""
    try:
        doc = json.loads(quota_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if str(doc.get("beijing_date") or "") != beijing_date():
        return None
    return doc
