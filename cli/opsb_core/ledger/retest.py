"""Retest interception and the near-duplicate filter (4.2 §3 target, research R7).

Two checks run before a ``parhelion_test`` request reaches the device queue:

``intercept``   the same SHA at the same level under the same suite version already has a formal run.
                The stored result comes back with the ledger ids that explain it, and no device work is
                queued. ``force_rerun=true`` plus a written ``reason`` is the only way past it.
``near_duplicate``  the candidate's diff is nearly the patch of a recent experiment: first by the git
                patch id (``ledger.gitref.diff_hash``), then by the similarity of the normalized diff
                text. Above ``SIMILARITY`` the recent experiment and its result are returned and the note
                asks for the difference before spending device time.

Both are advice with teeth only on cost: a forced rerun proceeds, and an intercepted result is a real
result of the same code. Nothing here blocks a submission or a gate decision.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import config as _cfg
from . import gitref, store, view

SIMILARITY = 0.90
COMPARE_LIMIT = 12
DIFF_MAX_CHARS = 40_000
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _records(workspace: Path, problem: str) -> list[dict[str, Any]]:
    from ..services import test_runs
    out = []
    for path in sorted(test_runs.registry_dir(workspace).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and str(record.get("problem") or "") == problem:
            out.append(record)
    return out


def suite_version(workspace: Path, problem: str) -> Any:
    """The committed suite version of the problem (None when it cannot be read)."""
    try:
        import importlib
        _cfg.ensure_scheduler_path()
        suite = importlib.import_module("scheduler.suite").load_suite(Path(workspace), problem)
        return suite.get("suite_version") or suite.get("version")
    except Exception:
        return None


def formal_record(record: dict[str, Any]) -> bool:
    """A finished, timed, all-pass formal run: the evidence a repeat request would only re-measure."""
    digest = record.get("digest") if isinstance(record.get("digest"), dict) else {}
    summary = digest.get("summary") if isinstance(digest.get("summary"), dict) else {}
    counts = summary.get("counts") or {}
    return (record.get("status") == "finished" and record.get("exit_code") == 0
            and bool(digest) and digest.get("formal", True) is not False
            and not any(k != "PASS" and v for k, v in counts.items()))


def existing_run(workspace: Path, problem: str, sha: str, level: str, cases: list[str] | None = None,
                 version: Any = None) -> dict[str, Any] | None:
    """The newest formal run of ``(sha, level)`` at the current suite version, if any."""
    wanted = sorted(str(c) for c in (cases or []))
    best: tuple[str, dict[str, Any]] = ("", {})
    for record in _records(workspace, problem):
        if str(record.get("sha") or "") != sha or str(record.get("level") or "") != level:
            continue
        if not formal_record(record):
            continue
        got = sorted(str(c) for c in record.get("cases") or [])
        if wanted and got != wanted:
            continue
        digest = record.get("digest") if isinstance(record.get("digest"), dict) else {}
        if version is not None and digest.get("suite_version") not in (None, version):
            continue
        stamp = str(record.get("finished_at") or record.get("created_at") or "")
        if stamp >= best[0]:
            best = (stamp, record)
    return best[1] or None


def ledger_ids(workspace: Path, problem: str, record: dict[str, Any], agent_id: str = "") -> list[str]:
    """Ledger refs of the entries an existing run already wrote (the platform writes them on settle)."""
    rid = str(record.get("request_id") or "")
    if not rid:
        return []
    out: list[str] = []
    for worker in store.workers(Path(workspace), problem):
        led = store.Ledger(Path(workspace), problem, worker)
        for suffix in ("measurement", "experiment"):
            row = led.find(f"test:{rid}:{suffix}")
            if row is not None:
                out.append(view.ref(row))
        if out:
            break
    return out


def intercept(workspace: Path | str, problem: str, sha: str, level: str, *, cases: list[str] | None = None,
              force_rerun: bool = False, reason: str = "", agent_id: str = "") -> dict[str, Any] | None:
    """The reply for a retest of an already-formal ``(sha, level, suite version)``; None to queue it."""
    ws = Path(workspace).expanduser()
    if force_rerun and str(reason or "").strip():
        return None  # a written reason is the documented way to measure again
    record = existing_run(ws, problem, sha, level, cases, suite_version(ws, problem))
    if record is None:
        return None
    from ..services import test_runs
    ids = ledger_ids(ws, problem, record, agent_id)
    return {
        **test_runs.public_view(record), "intercepted": True, "reused": True,
        "reason": ("this SHA and level already have a formal run under the current suite; use its result, "
                   "or pass force_rerun=true with a reason to measure again"),
        "ledger_ids": ids,
        "line": test_runs.compact_line(record),
    }


def normalize_patch(text: str) -> list[str]:
    """Changed and context lines of a patch, each keeping its ``+``/``-``/`` `` marker, whitespace collapsed.

    The marker matters: without it a revert (``-L``) reads exactly like the change it undoes (``+L``)
    and would be reported as its near duplicate. Hunk offsets and git headers are dropped.
    """
    lines: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith(("+++", "---", "@@", "index ", "diff ", "new file", "deleted file", "similarity")):
            continue
        if line.startswith(("+", "-", " ")):
            body = " ".join(line[1:].split())
            if body:
                lines.append(line[0] + body)
    return lines


def similarity(a: str, b: str) -> float:
    """Overlap of two normalized patches: shared changed lines over the larger patch.

    The overlap coefficient rather than Jaccard, because a near-identical change usually differs in a
    line or two on each side and Jaccard would then fall below any useful threshold.
    """
    left, right = set(normalize_patch(a)), set(normalize_patch(b))
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), len(right))


def _patch(repo: Path | str, sha: str) -> str:
    if not gitref.valid_sha(sha):
        return ""
    text = gitref.git_out(repo, "show", "--format=", "--patch", "--no-color", sha) or ""
    return text[:DIFF_MAX_CHARS]


def near_duplicate(workspace: Path | str, problem: str, sha: str, rows: list[dict[str, Any]],
                   agent_id: str = "") -> dict[str, Any] | None:
    """The most similar recent experiment of this problem's ledger when it is above ``SIMILARITY``."""
    repo = gitref.problem_repo(Path(workspace), problem)
    if repo is None or not gitref.valid_sha(sha):
        return None
    candidate_hash = gitref.diff_hash(repo, sha)
    exps = [r for r in view.current(rows) if r.get("type") == "experiment" and r.get("sha")]
    exps.sort(key=view.order_key, reverse=True)
    patch = ""
    best: tuple[float, dict[str, Any]] = (0.0, {})
    seen = 0
    for row in exps:
        other = str(row.get("sha") or "")
        if not gitref.valid_sha(other) or view.sha_match(other, sha):
            continue
        if candidate_hash and row.get("diff_hash") == candidate_hash:
            return {"kind": "same_diff", "similarity": 1.0, "id": view.ref(row), "sha": other[:7],
                    "outcome": row.get("outcome"), "claim": row.get("claim")}
        seen += 1
        if seen > COMPARE_LIMIT:
            break
        patch = patch or _patch(repo, sha)
        other_patch = _patch(repo, other)
        if not patch or not other_patch:
            continue
        score = similarity(patch, other_patch)
        if score > best[0]:
            best = (score, row)
    if best[0] >= SIMILARITY:
        row = best[1]
        return {"kind": "similar_diff", "similarity": round(best[0], 3), "id": view.ref(row),
                "sha": str(row.get("sha") or "")[:7], "outcome": row.get("outcome"), "claim": row.get("claim")}
    return None


def duplicate_note(found: dict[str, Any] | None) -> dict[str, Any] | None:
    """The reply decoration of a near duplicate: the entry, its result and what to do next."""
    if not found:
        return None
    return {
        "near_duplicate": found,
        "note": (f"{found['id']} already tested a nearly identical change (similarity {found['similarity']}, "
                 f"outcome {found.get('outcome') or '?'}); state the difference from it in the run's reason, "
                 f"or read it with parhelion_ledger(view=case:<id>) / parhelion_recall"),
    }
