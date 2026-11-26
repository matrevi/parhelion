"""Entry schema of the experiment ledger (docs/v4.2/research/compaction-memory.md section 5.3).

One entry is one JSON line of at most ``LINE_LIMIT`` bytes. Platform-owned fields are filled by
code (``source=platform``, ``confidence=measured``, ``diff_hash``, outcomes, per-case numbers);
``worker_fields`` strips them from anything a model writes. ``unverified`` is the peer level: a
teammate's claim (``source=peer``) that this worker has not measured itself. Types and their own
fields:

``measurement`` platform only: key, value, unit, n_repeats, noise_band, env_id, test_level
``hypothesis``  status, predicted_effect, test_plan, priority (1 = most urgent .. 5), resolved_by, resolution_note
``experiment``  hypothesis_id, change_summary, diff_hash, test_run_id, per_case_us, baseline_sha, delta_vs_baseline,
                outcome, lesson, receipt_id, submitted; prediction and result: expected_outcome (model),
                actual_outcome (platform); verdicts: was_diagnosis_correct, was_fix_effective; lessons,
                avoid_patterns ({pattern, scope, revisit_if}), try_patterns
``insight``     actionable_next, est_gain, cases, exploited_by
``failure``     approach, why_failed, cost_spent, retry_conditions
``decision``    chosen, rejected, reason
``constraint``  text, rule_source
"""
from __future__ import annotations

import json
import re
from typing import Any

TYPES = ("measurement", "hypothesis", "experiment", "insight", "failure", "decision", "constraint")
SOURCES = ("platform", "worker", "peer")
CONFIDENCE = ("measured", "inferred", "speculative", "unverified")
OPEN_STATUSES = ("open", "queued", "testing")
HYPOTHESIS_STATUS = OPEN_STATUSES + ("confirmed", "refuted", "inconclusive", "abandoned")
OUTCOMES = ("better", "worse", "neutral", "noise", "WA", "RE", "TLE", "compile_fail")
NEGATIVE_OUTCOMES = ("worse", "WA", "RE", "TLE", "compile_fail")

LINE_LIMIT = 1024
CLAIM_MAX = 240
WHY_MAX = 300
SUMMARY_MAX = 200
ID_RE = re.compile(r"^L-(\d+)$")
REF_RE = re.compile(r"^(?:([A-Za-z0-9._-]+)/)?(L-\d+)$")

COMMON = ("claim", "why", "scope", "base_sha", "sha", "evidence", "confidence", "links", "supersedes", "revisit_if")
PLATFORM_ONLY = ("source", "diff_hash", "actual_outcome", "per_case_us", "delta_vs_baseline", "outcome", "receipt_id",
                 "n_repeats", "noise_band", "env_id", "test_level", "test_run_id", "key", "value", "unit", "ekey", "median_us",
                 "tbest_us", "suite_version", "baseline_sha",
                 "resolved_by", "submitted", "exploited_by", "worker", "problem", "ts", "id", "truncated")
BY_TYPE: dict[str, tuple[str, ...]] = {
    "measurement": ("key", "value", "unit", "n_repeats", "noise_band", "env_id", "test_level", "test_run_id",
                    "receipt_id", "per_case_us", "median_us", "tbest_us", "suite_version"),
    "hypothesis": ("status", "predicted_effect", "test_plan", "priority", "resolved_by", "resolution_note"),
    "experiment": ("hypothesis_id", "change_summary", "diff_hash", "test_run_id", "per_case_us", "baseline_sha",
                   "delta_vs_baseline", "outcome", "lesson", "receipt_id", "submitted", "expected_outcome",
                   "actual_outcome", "was_diagnosis_correct", "was_fix_effective", "lessons", "avoid_patterns",
                   "try_patterns"),
    "insight": ("actionable_next", "est_gain", "cases", "exploited_by"),
    "failure": ("approach", "why_failed", "cost_spent", "retry_conditions"),
    "decision": ("chosen", "rejected", "reason"),
    "constraint": ("text", "rule_source"),
}
_CLIP = {"claim": CLAIM_MAX, "why": WHY_MAX, "change_summary": SUMMARY_MAX, "lesson": SUMMARY_MAX}


class LedgerError(ValueError):
    """An entry that cannot be stored."""


def clip(text: Any, limit: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def seq_of(entry_id: Any) -> int:
    m = ID_RE.match(str(entry_id or ""))
    return int(m.group(1)) if m else 0


def parse_ref(ref: str, default_worker: str = "") -> tuple[str, str]:
    """``(worker, id)`` of ``L-12`` or ``worker/L-12``; an unparsable ref raises."""
    m = REF_RE.match(str(ref or "").strip())
    if not m:
        raise LedgerError(f"bad ledger reference {ref!r}")
    return m.group(1) or default_worker, m.group(2)


def worker_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """What a model may set: the entry minus every platform-owned field."""
    return {k: v for k, v in fields.items() if k not in PLATFORM_ONLY}


def build(entry_type: str, fields: dict[str, Any], *, source: str = "platform") -> dict[str, Any]:
    """Validate and normalize the body of one entry (id, ts, worker and problem are added by the store)."""
    if entry_type not in TYPES:
        raise LedgerError(f"type must be one of {', '.join(TYPES)}")
    if source not in SOURCES:
        raise LedgerError(f"source must be one of {', '.join(SOURCES)}")
    if entry_type == "measurement" and source != "platform":
        raise LedgerError("measurements are written by the platform only")
    raw = dict(fields) if source == "platform" else worker_fields(fields)
    allowed = set(COMMON) | set(BY_TYPE[entry_type]) | ({"ekey", "diff_hash"} if source == "platform" else set())
    unknown = sorted(k for k in raw if k not in allowed)
    if unknown:
        raise LedgerError(f"unknown field(s) for {entry_type}: {', '.join(unknown)}")
    row: dict[str, Any] = {"type": entry_type, "source": source}
    for k, v in raw.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        row[k] = clip(v, _CLIP[k]) if k in _CLIP else v
    confidence = row.get("confidence", "measured" if source == "platform" else "inferred")
    if confidence not in CONFIDENCE:
        raise LedgerError(f"confidence must be one of {', '.join(CONFIDENCE)}")
    row["confidence"] = confidence if source == "platform" or confidence != "measured" else "inferred"
    if entry_type != "measurement" and not row.get("claim"):
        raise LedgerError("claim is required")
    if entry_type == "hypothesis":
        status = row.setdefault("status", "open")
        if status not in HYPOTHESIS_STATUS:
            raise LedgerError(f"status must be one of {', '.join(HYPOTHESIS_STATUS)}")
        priority = row.setdefault("priority", 3)
        if not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 5:
            raise LedgerError("priority must be an integer 1..5")
    if entry_type == "experiment" and row.get("outcome") not in (None, *OUTCOMES):
        raise LedgerError(f"outcome must be one of {', '.join(OUTCOMES)}")
    for key in ("evidence", "links", "supersedes"):
        if key in row and not isinstance(row[key], list):
            row[key] = [row[key]]
    return row


def _size(row: dict[str, Any]) -> int:
    return len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _shrink_value(value: Any) -> Any | None:
    """One step smaller: drop the last item of a container or cut a string; None when nothing is left."""
    if isinstance(value, dict) and value:
        return dict(list(value.items())[:-1]) or None
    if isinstance(value, list) and value:
        return value[:-1] or None
    if isinstance(value, str) and len(value) > 40:
        return value[: len(value) // 2] + "…"
    return None


def fit(row: dict[str, Any], limit: int = LINE_LIMIT) -> dict[str, Any]:
    """Shrink the largest optional fields until the line fits; the names cut are listed in ``truncated``."""
    if _size(row) <= limit:
        return row
    cut: list[str] = []
    protected = {"id", "ts", "worker", "problem", "type", "source", "confidence", "ekey", "status", "sha"}
    guard = 0
    target = limit - 48  # room for the ``truncated`` marker
    while _size(row) > target and guard < 500:
        guard += 1
        names = sorted((k for k in row if k not in protected), key=lambda k: -_size({k: row[k]}))
        shrunk = False
        for name in names:
            smaller = _shrink_value(row[name])
            if smaller is None and name in ("claim",):
                continue
            if smaller is None:
                del row[name]
            else:
                row[name] = smaller
            if name not in cut:
                cut.append(name)
            shrunk = True
            break
        if not shrunk:
            row["claim"] = clip(row.get("claim", ""), 60)
            break
    if cut:
        row["truncated"] = cut
    return row
