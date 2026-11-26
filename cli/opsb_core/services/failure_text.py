"""Keep the judge's message apart from local Python errors on rejected submissions (4.1 #11).

A failed upload used to store ``str(exc)`` plus the submit trace in ``detail``: a full Python
traceback whose last line wraps the judge's answer (``SomeError: HTTP 400: <message>``). Cards
showed the stack. New rows carry:

- ``judge_message``: the text the judge returned, for people;
- ``local_error``: the last line of the local exception;
- ``local_error_log``: the full text, written under ``logs/submit-errors/``.

``detail`` keeps a one-line summary for older readers. Rows written before 4.1 are split the same
way at read time (``card_text``), so history needs no migration.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from .. import config as _cfg

_JUDGE = re.compile(r"HTTP \d{3}: (.+)$")


def last_line(text: str) -> str:
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def split(text: Any) -> tuple[str | None, str | None]:
    """``(judge_message, local_error)`` of a stored failure text; either may be None."""
    text = str(text or "")
    if not text.strip():
        return None, None
    core = text.split(" | trace: ", 1)[0]
    tail = last_line(core)
    match = _JUDGE.search(tail)
    judge = match.group(1).strip() if match else None
    if judge and judge.startswith("{"):
        try:
            body = json.loads(judge)
            judge = str(body.get("message") or body.get("msg") or judge) if isinstance(body, dict) else judge
        except ValueError:
            pass
    return judge or None, tail[:300] or None


def record_fields(title: str, sha: str, text: str) -> dict[str, Any]:
    """Fields of a new rejected row; the full text goes to a log file, not into the journal."""
    judge, local = split(text)
    out: dict[str, Any] = {"judge_message": judge, "local_error": local, "detail": (judge or local or "")[:500]}
    try:
        folder = Path(_cfg.LOGS_DIR) / "submit-errors"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = folder / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', title)}-{str(sha)[:7]}-{stamp}.log"
        path.write_text(text, encoding="utf-8")
        out["local_error_log"] = str(path)
    except OSError:
        out["detail"] = text[:2000]  # keep the evidence when no log can be written
    return out


def card_text(row: dict[str, Any]) -> dict[str, Any]:
    """``judge_message`` / ``local_error`` of any row, splitting a legacy ``detail`` on read."""
    judge, local = row.get("judge_message"), row.get("local_error")
    if judge is None and local is None and row.get("status") in ("rejected", "blocked_compliance"):
        judge, local = split(row.get("detail"))
    return {"judge_message": judge, "local_error": local}


def display(row: dict[str, Any]) -> str:
    """What a held card shows: the judge's message, else the last line of the local error."""
    text = card_text(row)
    return str(text["judge_message"] or text["local_error"] or row.get("detail") or "")
