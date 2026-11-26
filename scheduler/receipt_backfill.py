"""``opsb bench receipts backfill``: give old receipt rows the canonical fields (4.1 #8).

Only data already on disk is used; nothing is fetched:

- the row itself (``timing_score`` holds the score and all per-case times of a Pass that did
  not beat the board);
- ``results/<title>/<sha>/submission-receipt.json`` names the commit of a submission whose
  journal rows lost it (judge-history rows with an empty ``sha``);
- ``results/scoring-evidence/live-board-tbest.json`` keeps the board submission's per-case
  times and the tbest list in roster order. Keys are testcase IDs when a receipt of the same
  problem names them, roster positions (``#01`` ...) otherwise.

A row gains a backfill only when that adds information. The new row is appended with
``source="receipt-backfill"`` (its writer is kept in ``backfilled_from_source``) and keeps the
original ``finished_at`` / ``submitted_at``, so readers that react to fresh verdicts do not see
a new one. Deduplication by ``submission_id`` makes the backfill row the canonical one; a second
run appends nothing. Rows that disk data cannot complete are listed with what is missing.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import pathlib
import re
from collections import Counter
from typing import Any

from . import judge_cases, receipts

SHA_RE = re.compile(r"[0-9a-f]{40}")


def _receipt_shas(workspace: pathlib.Path) -> dict[str, str]:
    out = {}
    for path in (workspace / "results").glob("*/*/submission-receipt.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sha = str(doc.get("validated_commit") or path.parent.name).lower() if isinstance(doc, dict) else ""
        if isinstance(doc, dict) and doc.get("submission_id") and SHA_RE.fullmatch(sha):
            out[str(doc["submission_id"])] = sha
    return out


def _live_results(workspace: pathlib.Path, title: str, rows: list[dict[str, Any]]):
    """``(board_sid, case_results, observed_at)`` from the live board, or ``(None, [], None)``."""
    live, fetched = judge_cases.live_board(workspace, title)
    order = judge_cases.roster(workspace, title, rows)
    timed = [k for k in order["keys"] if k not in set(order["untimed"])]
    times, tbest = live.get("our_public_times_us") or [], live.get("tbest") or []
    if not live.get("board_submission_id") or not timed or not (len(times) == len(tbest) == len(timed)):
        return None, [], None
    results = []
    for key in order["keys"]:
        if key in timed:
            idx = timed.index(key)
            results.append({"testcase_id": key, "status": "Pass", "time_us": float(times[idx]),
                            "tbest_us": float(tbest[idx])})
        else:
            results.append({"testcase_id": key, "status": "Pass", "time_us": None, "tbest_us": None})
    return str(live["board_submission_id"]), results, fetched


def _missing(row: dict[str, Any]) -> list[str]:
    gaps = []
    if receipts.official_score(row) is None:
        gaps.append("score")
    if not receipts.case_times(row):
        gaps.append("per-case times")
    if not receipts.is_pass(row) and not receipts.case_results(row):
        gaps.append("failing case")
    if not SHA_RE.fullmatch(str(row.get("sha") or "")):
        gaps.append("sha")
    return gaps


def plan(workspace: pathlib.Path, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What a backfill would append, and what stays missing. Pure: writes nothing."""
    path = receipts.ledger_path(workspace)
    rows = receipts.read_rows(path) if rows is None else rows
    canon = [r for r in receipts.canonical(rows) if receipts.judged(r)]
    shas = _receipt_shas(workspace)
    live = {}
    for title in sorted({str(r.get("title") or "") for r in canon}):
        sid, results, fetched = _live_results(workspace, title, [r for r in canon if r.get("title") == title])
        if sid:
            live[sid] = (results, fetched)
    append, missing, complete = [], [], 0
    for row in canon:
        base, adds = dict(row), []
        sid = str(row.get("submission_id"))
        if not SHA_RE.fullmatch(str(row.get("sha") or "")) and sid in shas:
            base["sha"] = shas[sid]
            adds.append("sha")
        if sid in live and not receipts.case_times(row):
            results, fetched = live[sid]
            base["case_results"] = results
            base["tbest_observed_at"] = fetched
            if results and results[0]["testcase_id"].startswith("#"):
                base["case_key_kind"] = "position"
            adds.append("per-case times (live board)")
        if row.get("score") is None and receipts.official_score(row) is not None and not receipts.is_normalized(row):
            adds.append("official_score")
        if receipts.case_times(row) and not receipts.is_normalized(row) and not row.get("time_by_case"):
            adds.append("time_by_case")
        new = receipts.normalize(base)
        gaps = _missing(new)
        if adds:
            new.update(source=receipts.BACKFILL_SOURCE, backfilled_from_source=receipts.effective_source(row) or None)
            append.append({"row": new, "adds": adds, "missing": gaps})
        elif gaps:
            missing.append({"submission_id": sid, "title": row.get("title"), "status": row.get("status"),
                            "missing": gaps})
        else:
            complete += 1
    for entry in append:
        if entry["missing"]:
            missing.append({"submission_id": entry["row"]["submission_id"], "title": entry["row"].get("title"),
                            "status": entry["row"].get("status"), "missing": entry["missing"]})
    return {"ledger_rows": len(rows), "receipts": len(canon), "complete": complete, "append": append,
            "missing": missing}


def report(doc: dict[str, Any], applied: int | None = None) -> dict[str, Any]:
    gaps = Counter(g for m in doc["missing"] for g in m["missing"])
    return {
        "ledger_rows": doc["ledger_rows"], "receipts": doc["receipts"], "complete": doc["complete"],
        "append": len(doc["append"]), "applied": applied,
        "appended": [{"submission_id": e["row"]["submission_id"], "title": e["row"].get("title"),
                      "sha": str(e["row"].get("sha") or "")[:7], "status": e["row"].get("status"),
                      "official_score": e["row"].get("official_score"), "adds": e["adds"]} for e in doc["append"]],
        "missing_counts": dict(sorted(gaps.items())),
        "missing": doc["missing"],
    }


def run(workspace: pathlib.Path, apply: bool = False) -> dict[str, Any]:
    if not apply:
        return report(plan(workspace))
    results = workspace / "results"
    with (results / ".submit-queue-io.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            doc = plan(workspace)  # re-planned under the journal lock: concurrent writers stay safe
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if doc["append"]:
                with receipts.ledger_path(workspace).open("a", encoding="utf-8") as fh:
                    for entry in doc["append"]:
                        fh.write(json.dumps({**entry["row"], "backfilled_at": stamp}, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return report(doc, applied=len(doc["append"]))
