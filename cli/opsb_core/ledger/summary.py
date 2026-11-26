"""The ledger summary: a text block built by code from ledger rows, never by a model.

It lists every open, queued or testing hypothesis and every unexploited insight (all of them, one
line each, so a seed always names them), the latest dead ends, and the latest per-case measurement.
When the block would exceed its budget the lines shorten in steps and finally reduce to bare ids;
an id is never dropped.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import model, view

DEFAULT_MAX_CHARS = 3000
FAILURE_LINES = 15
CASE_LINES = 10
DETAIL_STEPS = (140, 80, 0)


def _ref(row: dict[str, Any], own: str) -> str:
    """Bare ``L-0003`` for the owning worker's rows, ``worker/L-0003`` for teammates'."""
    return str(row["id"]) if not row.get("worker") or row.get("worker") == own else view.ref(row)


def _sha7(row: dict[str, Any]) -> str:
    sha = str(row.get("sha") or "")[:7]
    return f" {sha}" if sha else ""


def _predicted(row: dict[str, Any]) -> str:
    pred = row.get("predicted_effect")
    if isinstance(pred, dict):
        if isinstance(pred.get("delta_mu"), (int, float)):
            return f" predicted {pred['delta_mu']:+.2f}±{float(pred.get('delta_sigma') or 0):.2f}"
        return " predicted " + ", ".join(f"{k} {v}" for k, v in list(pred.items())[:3])
    return f" predicted {pred}" if pred else ""


def _hyp_line(row: dict[str, Any], detail: int, own: str) -> str:
    head = f"- {_ref(row, own)} [{row.get('status')} p{row.get('priority', 3)}]{_sha7(row)}"
    if not detail:
        return head
    return f"{head} {model.clip(row.get('claim'), detail)}{_predicted(row)[: detail // 2]}"


def _insight_line(row: dict[str, Any], detail: int, own: str) -> str:
    cases = row.get("cases") or (row.get("scope") or {}).get("cases") or []
    head = f"- {_ref(row, own)}" + (f" cases {','.join(str(c) for c in cases[:6])}" if cases else "")
    if not detail:
        return head
    nxt = f" next: {model.clip(row.get('actionable_next'), detail // 2)}" if row.get("actionable_next") else ""
    return f"{head} {model.clip(row.get('claim'), detail)}{nxt}"


def _negative_line(row: dict[str, Any], detail: int, own: str) -> str:
    if row.get("type") == "failure":
        what = f"failed: {model.clip(row.get('approach') or row.get('claim'), detail or 40)}"
        why = model.clip(row.get("why_failed"), detail // 2) if detail else ""
        retry = f" retry if {model.clip(row.get('retry_conditions'), detail // 3)}" if row.get("retry_conditions") and detail else ""
        return f"- {_ref(row, own)} {what}" + (f" because {why}" if why else "") + retry
    if row.get("type") == "hypothesis":
        note = f" {model.clip(row.get('resolution_note'), detail)}" if detail and row.get("resolution_note") else ""
        return f"- {_ref(row, own)} refuted{_sha7(row)}: {model.clip(row.get('claim'), detail or 40)}{note}"
    level = (row.get("actual_outcome") or {}).get("level") or ""
    why = f" {model.clip(row.get('lesson') or row.get('claim'), detail)}" if detail else ""
    return f"- {_ref(row, own)} {row.get('outcome')}{_sha7(row)} {level}{why}".rstrip()


def _latest_measurement(rows: list[dict[str, Any]]) -> str:
    tests = [r for r in view.of_type(rows, "measurement") if isinstance(r.get("per_case_us"), dict)
             and r.get("test_level")]
    if not tests:
        return ""
    last = max(tests, key=view.order_key)
    cases = list(last["per_case_us"].items())[:CASE_LINES]
    body = " ".join(f"{k}:{v}" for k, v in cases)
    noise = last.get("noise_band")
    tail = f" noise {noise}" if noise else ""
    return f"Latest test {_sha7(last).strip()} {last.get('test_level')} (min µs): {body}{tail}"


def render(rows: Iterable[dict[str, Any]], *, max_chars: int = DEFAULT_MAX_CHARS, header: str = "",
           own: str = "") -> str:
    """The summary text; empty string when the ledger has nothing to say."""
    rows = list(rows)
    hyps, insights = view.open_hypotheses(rows), view.unexploited_insights(rows)
    dead = (view.failures(rows) + view.negatives(rows))
    dead.sort(key=view.order_key, reverse=True)
    shown, older = dead[:FAILURE_LINES], max(0, len(dead) - FAILURE_LINES)
    measured = _latest_measurement(rows)
    if not (hyps or insights or dead or measured):
        return ""
    out = ""
    for detail in DETAIL_STEPS:
        lines = [header or "LEDGER SUMMARY (code-built from the ledger)"]
        lines.append(f"Open hypotheses ({len(hyps)}):" if hyps else "Open hypotheses: none")
        lines += [_hyp_line(r, detail, own) for r in hyps]
        lines.append(f"Unexploited insights ({len(insights)}):" if insights else "Unexploited insights: none")
        lines += [_insight_line(r, detail, own) for r in insights]
        if shown:
            lines.append(f"Recent dead ends ({len(shown)}" + (f" of {len(dead)}" if older else "") + "):")
            lines += [_negative_line(r, detail, own) for r in shown]
            if older:
                lines.append(f"{older} earlier dead ends: ledger view failures")
        if measured and detail:
            lines.append(measured)
        out = "\n".join(lines)
        if len(out) <= max_chars:
            return out
    return out  # bare ids still over budget: the ids must stay, the caller decides what else to cut


def ids_missing(text: str, rows: Iterable[dict[str, Any]], own: str = "") -> list[str]:
    """Refs of open hypotheses and unexploited insights that ``text`` does not mention."""
    rows = list(rows)
    need = [_ref(r, own) for r in view.open_hypotheses(rows) + view.unexploited_insights(rows)]
    return [ref for ref in need if ref not in text]
