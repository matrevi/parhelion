"""Judge-side per-case evidence on disk: roster order, timed cases, per-SHA official times.

Sources, newest first where they overlap:

- normalized or legacy receipts (``scheduler.receipts``): ``time_by_case`` / ``tbest_by_case``
  keyed by testcase ID; the dict order of ``tbest_by_case`` is the ranking roster order;
- ``results/scoring-evidence/live-board-tbest.json``: per problem the ranking ``tbest`` list and
  the board submission's times, both in roster order and both without the cases the judge
  does not time (tbest None), plus ``total_testcase_count``;
- legacy ``public_times_us``: the first five *timed* cases in judge order.

When no receipt names the testcase IDs, roster keys are positions (``#01`` ...), usable only
when the live board lists every case. Nothing here touches the network.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from typing import Any

from . import receipts

LIVE_BOARD = ("scoring-evidence", "live-board-tbest.json")


def position_key(index: int) -> str:
    return f"#{index + 1:02d}"


def parse_iso(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=datetime.timezone.utc)


def _read(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def live_board(workspace: pathlib.Path, title: str) -> tuple[dict[str, Any], str | None]:
    doc = _read(workspace / "results" / LIVE_BOARD[0] / LIVE_BOARD[1])
    if not isinstance(doc, dict):
        return {}, None
    entry = (doc.get("problems") or {}).get(title)
    return (entry if isinstance(entry, dict) else {}), doc.get("fetched_at")


def _stamp(row: dict[str, Any]) -> str:
    return str(row.get("submitted_at") or row.get("finished_at") or "")


def roster(workspace: pathlib.Path, title: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """``{keys, kind, count, untimed, source}``; ``keys`` is the judge's roster order."""
    rows = receipts.load(workspace, title) if rows is None else rows
    live, _ = live_board(workspace, title)
    count = live.get("total_testcase_count") if isinstance(live.get("total_testcase_count"), int) else None
    named = [r for r in rows if receipts.case_tbest(r) or receipts.case_times(r)]
    if named:
        row = max(named, key=_stamp)
        tbest = receipts.case_tbest(row)
        keys = list(tbest or receipts.case_times(row))
        untimed = [k for k in keys if tbest and tbest.get(k) is None]
        if count is None or count == len(keys):
            kind = "position" if all(k.startswith("#") for k in keys) else "testcase_id"
            return {"keys": keys, "kind": kind, "count": len(keys), "untimed": untimed,
                    "source": row.get("submission_id")}
    listed = live.get("tbest") or []
    if count and len(listed) == count:
        return {"keys": [position_key(i) for i in range(count)], "kind": "position", "count": count,
                "untimed": [], "source": "live-board"}
    return {"keys": [], "kind": "unknown", "count": count or 0, "untimed": [], "source": None}


def _timed(order: dict[str, Any]) -> list[str]:
    return [k for k in order["keys"] if k not in set(order["untimed"])]


def observations(workspace: pathlib.Path, title: str, rows: list[dict[str, Any]] | None = None,
                 order: dict[str, Any] | None = None, as_of: datetime.datetime | None = None
                 ) -> list[dict[str, Any]]:
    """Official per-case times per judged submission: ``{sha, submission_id, source, at, times}``.

    ``times`` maps roster keys to microseconds. One entry per submission, the fullest source wins.
    With ``as_of`` only submissions judged before it count (a ranking row's times never change).
    """
    rows = receipts.load(workspace, title) if rows is None else rows
    order = roster(workspace, title, rows) if order is None else order
    keys, timed = set(order["keys"]), _timed(order)
    by_sid: dict[str, dict[str, Any]] = {}

    def add(row: dict[str, Any], source: str, times: dict[str, float]) -> None:
        sha = str(row.get("canonical_sha") or row.get("sha") or "")
        times = {k: v for k, v in times.items() if k in keys and v is not None and v > 0}
        sid = str(row.get("submission_id") or "")
        stamp = parse_iso(_stamp(row))
        if not sha or not sid or not times or (as_of is not None and (stamp is None or stamp >= as_of)):
            return
        if sid not in by_sid or len(times) > len(by_sid[sid]["times"]):
            by_sid[sid] = {"sha": sha, "submission_id": sid, "source": source, "at": _stamp(row), "times": times}

    for row in rows:
        full = receipts.case_times(row)
        if full:
            add(row, "receipt", full)
            continue
        # Positional public lists skip untimed cases; on a failed run they can skip more.
        public, _ = receipts.public_times(row)
        if public and timed and receipts.is_pass(row):
            add(row, "public", dict(zip(timed, public)))
    live, _ = live_board(workspace, title)
    board_sid = live.get("board_submission_id")
    board_row = next((r for r in rows if r.get("submission_id") == board_sid), None)
    ours = live.get("our_public_times_us") or []
    if board_row is not None and ours and len(ours) == len(timed):
        add(board_row, "live-board", dict(zip(timed, ours)))
    return sorted(by_sid.values(), key=lambda o: (o["at"], o["submission_id"]))


def tbest_now(workspace: pathlib.Path, title: str, as_of: datetime.datetime | None = None,
              rows: list[dict[str, Any]] | None = None, order: dict[str, Any] | None = None
              ) -> tuple[dict[str, float | None], str | None, str | None]:
    """Newest tbest per roster key observed before ``as_of``: ``(tbest, observed_at, source)``."""
    rows = receipts.load(workspace, title) if rows is None else rows
    order = roster(workspace, title, rows) if order is None else order
    best: tuple[str, dict[str, float | None], str] | None = None
    for row in rows:
        tbest = receipts.case_tbest(row)
        seen = receipts.tbest_observed_at(row)
        stamp = parse_iso(seen)
        if not tbest or stamp is None or (as_of is not None and stamp >= as_of):
            continue
        if set(tbest) != set(order["keys"]):
            continue
        if best is None or stamp.isoformat() > best[0]:
            best = (stamp.isoformat(), tbest, str(row.get("submission_id")))
    live, fetched = live_board(workspace, title)
    fetched_at = parse_iso(fetched)
    listed = live.get("tbest") or []
    timed = _timed(order)
    if (fetched_at is not None and (as_of is None or fetched_at < as_of) and listed
            and len(listed) == len(timed) and (best is None or fetched_at.isoformat() > best[0])):
        values: dict[str, float | None] = {k: None for k in order["keys"]}
        values.update(zip(timed, (float(v) for v in listed)))
        best = (fetched_at.isoformat(), values, "live-board")
    if best is None:
        return {}, None, None
    return best[1], best[0], best[2]
