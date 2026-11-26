"""Platform auto-writes: the ledger as a by-product of tests, submission requests and judge receipts.

``on_test_settled``  a finished or failed test run -> one ``measurement`` and one ``experiment``;
``on_gate_request``  a submission request with no hypothesis for its SHA -> a platform ``hypothesis``;
``on_receipt``       a judged receipt -> a ``measurement`` and the verdict on the SHA's hypothesis.

All three are idempotent on their evidence keys (request id, SHA, submission id), so a replayed run
result or receipt batch writes nothing twice. Entries go to the ledger of the requesting worker, or to
the ``platform`` ledger when no worker owns the event. Callers wrap these in ``guard``: a ledger fault
must never lose a test result or a receipt.

The ``apply_*`` functions hold the logic and take any ledger-like object (``rows``, ``append``,
``supersede``, ``find``), so the replay benchmark rebuilds a ledger in memory with the same code.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from . import gitref, model, rows as builders, store, view

PLATFORM_WORKER = "platform"


def guard(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a ledger hook; any failure is reported on stderr and swallowed."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"ledger: {fn.__name__} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


def owner(agent_id: Any) -> str:
    name = str(agent_id or "")
    return name if store.NAME_RE.match(name) and ".." not in name else PLATFORM_WORKER


def _ledger(ws: Path, problem: str, agent_id: Any) -> store.Ledger:
    return store.Ledger(ws, problem, owner(agent_id))


def _timed(record: dict[str, Any]) -> bool:
    timing = (record.get("digest") or {}).get("timing") if isinstance(record.get("digest"), dict) else None
    return bool(isinstance(timing, dict) and timing.get("cases"))


# ---- logic, on any ledger-like object --------------------------------------------------------------

def apply_test(led: Any, record: dict[str, Any], *, diff_hash: str = "", subject: str = "") -> list[dict[str, Any]]:
    out, existing = [], led.rows()
    sha = str(record.get("sha") or "")
    for entry_type, fields, ekey in builders.test_entries(record, diff_hash=diff_hash, subject=subject):
        if entry_type == "experiment":
            hyps = view.for_sha(existing, "hypothesis", sha)
            if hyps:
                fields = {**fields, "hypothesis_id": hyps[-1]["id"]}
        out.append(led.append(entry_type, fields, ekey=ekey))
    return out


def apply_gate(led: Any, sha: str, *, note: str = "", subject: str = "", estimate: dict[str, Any] | None = None,
               enqueued: bool = False, intent: str = "exploit") -> dict[str, Any] | None:
    if view.for_sha(led.rows(), "hypothesis", sha):
        return None
    est = estimate or {}
    entry_type, fields, ekey = builders.gate_hypothesis(
        sha, note=note, subject=subject, mu=est.get("delta_expected"), sigma=est.get("confidence_sigma"),
        enqueued=enqueued, intent=intent)
    return led.append(entry_type, fields, ekey=ekey)


def apply_link(led: Any, sha: str, hypothesis_id: str = "", predicted_effect: Any = None, *,
               note: str = "", subject: str = "", estimate: dict[str, Any] | None = None,
               enqueued: bool = False, write: bool = True, intent: str = "exploit") -> dict[str, Any]:
    """``{id, auto, note}`` of the hypothesis a submission request names, or of the generated one."""
    from . import note as _note
    rows = led.rows()
    unknown = ""
    if hypothesis_id:
        head = view.head(rows, hypothesis_id)
        if head is None:  # a stale id must not refuse the submission: generate and say so
            unknown, hypothesis_id = str(hypothesis_id), ""
    if hypothesis_id:
        head = view.head(led.rows(), hypothesis_id)
        changes: dict[str, Any] = {}
        if predicted_effect:
            changes["predicted_effect"] = predicted_effect
        if enqueued and head.get("status") in model.OPEN_STATUSES:
            changes["status"] = "queued"
        row = _note.gate_hypothesis(led, {**changes, "id": hypothesis_id}, sha=sha) if changes else head
        return {"id": row["id"], "auto": False, "note": ""}
    rows = led.rows()
    existing = view.for_sha(rows, "hypothesis", sha)
    if existing and not write:
        return {"id": existing[-1]["id"], "auto": False, "note": unknown}
    created = None if not write else apply_gate(led, sha, note=note, subject=subject, estimate=estimate,
                                                enqueued=enqueued, intent=intent)
    row = created or (view.for_sha(led.rows(), "hypothesis", sha) or [{}])[-1]
    if not row.get("id"):
        return {"id": "", "auto": False, "note": "no hypothesis could be generated for this SHA"}
    note = (f"{unknown} is not in this ledger; the platform generated {row['id']} instead" if unknown
            else f"no hypothesis_id given: the platform generated {row['id']} from the submission note"
            if created else f"this SHA already has {row['id']}")
    return {"id": row["id"], "auto": bool(created or unknown), "unknown_id": unknown, "note": note}


def apply_receipt(led: Any, canon: dict[str, Any], *, gain: float | None = None,
                  prior: float | None = None) -> dict[str, Any]:
    """``canon`` is a canonical receipt (``official_score`` set). Writes the measurement and the verdicts once."""
    sid, sha = str(canon.get("submission_id") or ""), str(canon.get("sha") or "")
    entry_type, fields, ekey = builders.receipt_entry(canon, gain=gain, prior=prior)
    measured = led.append(entry_type, fields, ekey=ekey)
    result: dict[str, Any] = {"measurement": measured["id"], "new": not measured.get("duplicate"),
                              "hypotheses": [], "experiments": []}
    rows = led.rows()
    exps = view.for_sha(rows, "experiment", sha)
    for hyp in view.for_sha(rows, "hypothesis", sha):
        if hyp.get("status") not in model.OPEN_STATUSES:
            continue
        verdict = builders.judge(canon, gain, hyp.get("predicted_effect"))
        if verdict is None:
            continue
        status, note = verdict
        new = led.supersede(hyp["id"], {"status": status, "resolution_note": note,
                                        "resolved_by": exps[-1]["id"] if exps else measured["id"]},
                            ekey=f"resolve:{sid}:{hyp['id']}")
        result["hypotheses"].append({"id": new["id"], "status": status})
    judged = {"judged": canon.get("status"), "official_score": canon.get("official_score"),
              "gain": None if gain is None else round(gain, 2)}
    for exp in exps:
        if exp.get("receipt_id") == sid:
            continue
        actual = {**(exp.get("actual_outcome") or {}), **{k: v for k, v in judged.items() if v is not None}}
        patch: dict[str, Any] = {"receipt_id": sid, "submitted": True, "actual_outcome": actual}
        if builders.receipt_outcome(canon):
            patch["outcome"] = builders.receipt_outcome(canon)
        new = led.supersede(exp["id"], patch, ekey=f"receipt:{sid}:exp:{exp['id']}")
        result["experiments"].append(new["id"])
    return result


# ---- platform entry points -------------------------------------------------------------------------

def on_test_settled(ws: Path, record: dict[str, Any], *, hash_fn: Callable[[Path | str, str], str] = gitref.diff_hash,
                    subject_fn: Callable[[Path | str, str], str] = gitref.subject) -> list[dict[str, Any]]:
    """Write the measurement and experiment of a settled test-run record; returns the stored rows."""
    problem, rid = str(record.get("problem") or ""), str(record.get("request_id") or "")
    if not problem or not rid or record.get("status") not in ("finished", "failed"):
        return []
    led = _ledger(ws, problem, record.get("agent_id"))
    if led.find(f"test:{rid}:experiment") and (led.find(f"test:{rid}:measurement") or not _timed(record)):
        return []
    sha = str(record.get("sha") or "")
    repo = gitref.problem_repo(ws, problem)
    return apply_test(led, record, diff_hash=hash_fn(repo, sha) if repo else "",
                      subject=subject_fn(repo, sha) if repo else "")


def on_gate_request(ws: Path, problem: str, sha: str, agent_id: Any, *, note: str = "",
                    estimate: dict[str, Any] | None = None, enqueued: bool = False) -> dict[str, Any] | None:
    """Create the platform hypothesis of a submission request unless its SHA already has one.

    ``enqueued`` is whether the gate let the candidate into the queue (status ``queued``); a request the
    gate held stays ``open``. Previews (``enqueue`` false) are not requests and never reach this.
    """
    if not problem or not gitref.valid_sha(sha):
        return None
    repo = gitref.problem_repo(ws, problem)
    return apply_gate(_ledger(ws, problem, agent_id), sha, note=note,
                      subject=gitref.subject(repo, sha) if repo and not note.strip() else "",
                      estimate=estimate, enqueued=enqueued)


def on_gate_link(ws: Path, problem: str, sha: str, agent_id: Any, *, note: str = "",
                 estimate: dict[str, Any] | None = None, enqueued: bool = False, write: bool = True,
                 hypothesis_id: str = "", predicted_effect: Any = None,
                 intent: str = "exploit") -> dict[str, Any] | None:
    """The hypothesis behind one submission request (named, updated, or generated) — 4.2 C #3.

    A preview (``write`` false) only reports which id the ledger already holds and writes nothing.
    """
    if not problem or not gitref.valid_sha(sha):
        return None
    led = _ledger(ws, problem, agent_id)
    repo = gitref.problem_repo(ws, problem)
    return apply_link(led, sha, hypothesis_id, predicted_effect, note=note,
                      subject=gitref.subject(repo, sha) if repo and not note.strip() else "",
                      estimate=estimate, enqueued=enqueued, write=write, intent=intent)


def on_receipt(ws: Path, problem: str, row: dict[str, Any], agent_id: Any = None, *, gain: float | None = None,
               prior: float | None = None) -> dict[str, Any] | None:
    """Record a judged receipt once and resolve the hypothesis behind its SHA; None when not applicable.

    A Pass whose score no source has yet is skipped, and handled when the score arrives.
    """
    from ..services.judge_score_fetch import receipts_module
    receipts = receipts_module()
    canon = receipts.normalize(row)
    if not problem or not receipts.judged(canon) or canon.get("official_score") is None:
        return None
    return apply_receipt(_ledger(ws, problem, agent_id), canon, gain=gain, prior=prior)
