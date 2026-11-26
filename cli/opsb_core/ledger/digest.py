"""The capped ledger digest of a compaction seed (4.2 C #5, research R4).

Built by code from ledger rows, never rewritten by a model: a model submits delta entries through
``parhelion_note`` and this module merges them.

Order and caps:

1. every open, queued or testing hypothesis, most urgent first (``priority`` 1..5), at most
   ``LIVE_DETAIL_MAX`` of them with their claim and prediction, the rest as bare ids;
2. every unexploited insight, same cap (the two sections share one budget of live items);
3. the newest dead ends, ``FAILURE_LINES`` at most, one line each with its id, then a count;
4. the latest timed measurement with its noise band.

An id of an open hypothesis or of an unexploited insight is never dropped: when the lines do not
fit, the detail goes first, then the failure lines, and finally the ids of the live items stay in one
``also open:`` line. That is the invariant the handoff validator of P05 checks against.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import model, view

LIVE_DETAIL_MAX = 16  # research R4: 16-20 live items with detail, the rest as ids
LIVE_ID_MAX = 48
FAILURE_LINES = 18  # #5: the last 15-20 failure directions
CASE_LINES = 12
DETAIL_STEPS = (120, 80, 40, 0)


def ref(row: dict[str, Any], own: str) -> str:
    """Bare ``L-0003`` for this worker's rows, ``worker/L-0003`` for a teammate's."""
    return str(row["id"]) if not row.get("worker") or row.get("worker") == own else view.ref(row)


def _sha7(row: dict[str, Any]) -> str:
    sha = str(row.get("sha") or "")[:7]
    return f" {sha}" if sha else ""


def key_numbers(row: dict[str, Any]) -> str:
    """The key numbers of one entry: its prediction, its estimate or its cost."""
    for key, fmt in (("predicted_effect", "pred"), ("est_gain", "gain")):
        value = row.get(key)
        if isinstance(value, dict) and value:
            body = ", ".join(f"{k} {v}" for k, v in list(value.items())[:3])
            return f" {fmt} {body}"
        if isinstance(value, str) and value.strip():
            return f" {fmt} {model.clip(value, 60)}"
    if row.get("cost_spent"):
        return f" cost {model.clip(row.get('cost_spent'), 40)}"
    return ""


def _hyp_line(row: dict[str, Any], detail: int, own: str) -> str:
    head = f"- {ref(row, own)} [{row.get('status')} p{row.get('priority', 3)}]{_sha7(row)}"
    if not detail:
        return head
    return f"{head} {model.clip(row.get('claim'), detail)}{key_numbers(row)[: max(20, detail // 2)]}"


def _insight_line(row: dict[str, Any], detail: int, own: str) -> str:
    cases = row.get("cases") or (row.get("scope") or {}).get("cases") or []
    head = f"- {ref(row, own)}" + (f" cases {','.join(str(c) for c in cases[:6])}" if cases else "")
    if not detail:
        return head
    nxt = f" next: {model.clip(row.get('actionable_next'), detail // 2)}" if row.get("actionable_next") else ""
    return f"{head} {model.clip(row.get('claim'), detail)}{key_numbers(row)[: max(16, detail // 2)]}{nxt}"


def _failure_line(row: dict[str, Any], detail: int, own: str) -> str:
    if row.get("type") == "failure":
        what = model.clip(row.get("approach") or row.get("claim"), detail or 40)
        why = model.clip(row.get("why_failed"), detail // 2) if detail else ""
        retry = (f" retry if {model.clip(row.get('retry_conditions'), detail // 3)}"
                 if row.get("retry_conditions") and detail else "")
        return f"- {ref(row, own)} failed: {what}" + (f" because {why}" if why else "") + retry
    if row.get("type") == "hypothesis":
        note = f" {model.clip(row.get('resolution_note'), detail)}" if detail and row.get("resolution_note") else ""
        return f"- {ref(row, own)} refuted{_sha7(row)}: {model.clip(row.get('claim'), detail or 40)}{note}"
    level = (row.get("actual_outcome") or {}).get("level") or ""
    why = f" {model.clip(row.get('lesson') or row.get('claim'), detail)}" if detail else ""
    return f"- {ref(row, own)} {row.get('outcome')}{_sha7(row)} {level}{why}".rstrip()


def live(rows: Iterable[dict[str, Any]], own: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(open hypotheses, unexploited insights)`` in digest order."""
    rows = list(rows)
    return view.open_hypotheses(rows), view.unexploited_insights(rows)


def dead_ends(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failures and negative experiments, newest first."""
    dead = view.failures(rows) + [r for r in view.negatives(rows) if r.get("type") == "experiment"]
    dead.sort(key=view.order_key, reverse=True)
    return dead


def latest_measurement(rows: Iterable[dict[str, Any]]) -> str:
    """The newest timed test's per-case minima and noise band (the case under study)."""
    tests = [r for r in view.of_type(rows, "measurement") if isinstance(r.get("per_case_us"), dict)
             and r.get("test_level")]
    if not tests:
        return ""
    last = max(tests, key=view.order_key)
    body = " ".join(f"{k}:{v}" for k, v in list(last["per_case_us"].items())[:CASE_LINES])
    noise = last.get("noise_band")
    tail = f" noise {noise}" if noise else ""
    return f"Latest test {str(last.get('sha') or '')[:7]} {last.get('test_level')} (min µs): {body}{tail}"


def _live_sections(hyps: list[dict[str, Any]], insights: list[dict[str, Any]], detail: int, own: str
                   ) -> list[str]:
    """Both live sections at one detail level: at most ``LIVE_DETAIL_MAX`` detailed items, then ids."""
    budget = [LIVE_DETAIL_MAX]

    def one(items: list[dict[str, Any]], fmt) -> list[str]:
        out = []
        for row in items:
            if budget[0] > 0:
                budget[0] -= 1
                out.append(fmt(row, detail, own))
            else:
                out.append(fmt(row, 0, own))
        return out

    lines = [f"Open hypotheses ({len(hyps)}):" if hyps else "Open hypotheses: none"]
    lines += one(hyps, _hyp_line)
    lines.append(f"Unexploited insights ({len(insights)}):" if insights else "Unexploited insights: none")
    lines += one(insights, _insight_line)
    return lines


def render(rows: Iterable[dict[str, Any]], *, own: str = "", max_chars: int = 3000,
           failures: int = FAILURE_LINES, header: str = "LEDGER (code-built; parhelion_ledger gives the full rows)") -> str:
    """The digest text; empty when the ledger has nothing to say.

    Shortens in steps until it fits ``max_chars``: detail first, then the failure lines, and the ids
    of the live entries stay in an ``also open`` line when even bare ids overflow.
    """
    rows = list(rows)
    hyps, insights = live(rows, own)
    dead = dead_ends(rows)
    measured = latest_measurement(rows)
    if not (hyps or insights or dead or measured):
        return ""
    out = ""
    for detail in DETAIL_STEPS:
        for shown in _failure_counts(failures):
            out = _render(header, hyps, insights, dead, shown, detail, own, measured)
            if len(out) <= max_chars:
                return out
    return out  # ids kept, the caller decides what else to cut


def _failure_counts(limit: int) -> list[int]:
    return [n for n in (limit, limit // 2, 6, 0) if 0 <= n <= limit]


def _render(header: str, hyps: list[dict[str, Any]], insights: list[dict[str, Any]], dead: list[dict[str, Any]],
            failures: int, detail: int, own: str, measured: str) -> str:
    lines = [header]
    lines += _live_sections(hyps, insights, detail, own)
    kept = dead[:failures]
    older = max(0, len(dead) - len(kept))
    if kept:
        lines.append(f"Recent dead ends ({len(kept)}" + (f" of {len(dead)}" if older else "") + "):")
        lines += [_failure_line(r, detail, own) for r in kept]
    elif dead:
        lines.append(f"Dead ends: {len(dead)} recorded")
    if older:
        lines.append(f"{older} earlier dead ends: parhelion_ledger(view=failures) or parhelion_recall")
    if measured and detail:
        lines.append(measured)
    return "\n".join(lines)


def ids_missing(text: str, rows: Iterable[dict[str, Any]], own: str = "") -> list[str]:
    """Refs of the live entries that ``text`` does not name (the seed must name all of them)."""
    rows = list(rows)
    hyps, insights = live(rows, own)
    return [r for r in (ref(row, own) for row in hyps + insights) if r not in text]
