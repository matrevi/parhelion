"""Evidence used by both the scheduler and the final upload check."""
from __future__ import annotations

import json
import re
from pathlib import Path


OPENING_PROBES = 3


def submitted_attempts(workspace: Path, title: str, outcomes: list[dict]) -> int:
    rows = [row for row in outcomes if row.get("title") == title and row.get("submission_id")]
    ids = {row["submission_id"] for row in rows}
    for receipt in (workspace / "results" / title).glob("*/submission-receipt.json"):
        try:
            sid = json.loads(receipt.read_text()).get("submission_id")
        except (ValueError, OSError):
            continue
        if sid:
            ids.add(sid)
    return len(ids)


def compile_evidence(record: dict, log: Path) -> tuple[bool, str]:
    code = record.get("exit_code")
    if code in (0, 2):
        return True, f"server exit={code} (build+run executed)"
    try:
        contents = log.read_text(errors="replace")
    except OSError:
        contents = ""
    if "Built target" in contents and not re.search(r"error:|COMPILE ERROR|Error 2", contents):
        return True, "validation.log shows a completed build"
    return False, "no successful compile record"
