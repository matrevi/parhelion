"""Materialize per-contest incident cards from authoritative submission receipts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..config import (
    ConfigRepository,
    parse_iso,
    read_json,
    read_jsonl,
    workspace_state_dir,
)
from ..services.sync_reconciler import collapse_reconciled_outcomes
from .store import _is_num, _with_index_lock, triad_kb_dirs

ERRORS = {
    "Wrong Answer": "WA",
    "Runtime Error": "RE",
    "Time Limit Exceeded": "TLE",
    "Compile Error": "CE",
    "Compilation Error": "CE",
}


def sync_bugs(workspace: Path, *, noise_floor: float | None = None):
    ws = workspace.resolve()
    manifest = read_json(ws / "opsb.workspace.json", {})
    problems = manifest.get("problems") or read_json(
        ws / "bench/problems.json", []
    )
    titles = {p["title"] for p in problems if isinstance(p, dict) and p.get("title")}
    ledger = ws / "results/submit-queue-outcome.jsonl"
    if not titles or not ledger.is_file():
        return {"count": 0, "updated": False}
    scope = str(ws)
    contest = manifest.get("contest_id") or ws.name
    index = triad_kb_dirs()["bugs"] / "index.json"
    if noise_floor is None:
        noise_floor = (
            ConfigRepository.load().get("scheduler", {}).get("noise_floor_delta", 2.5)
        )
    if not _is_num(noise_floor) or noise_floor < 0:
        raise ValueError("Invalid score noise floor")

    def rebuild(doc):
        prior = {e["id"]: e for e in doc["entries"]}
        entries = [e for e in prior.values() if e.get("workspace") != scope]
        receipts = collapse_reconciled_outcomes(read_jsonl(ledger))
        report = read_json(ws / "results/scoring-evidence/board-reconciled.json", {})
        details = {
            (p["title"], a["submission_id"]): a
            for p in report.get("board", [])
            for a in p.get("attempts", [])
            if a.get("submission_id")
        }
        fixes = read_json(workspace_state_dir(ws) / "scheduler_state.json", {}).get(
            "verified_escapes", {}
        )
        receipts.sort(
            key=lambda r: str(r.get("submitted_at") or r.get("finished_at") or "")
        )
        previous, cards = {}, {}
        for r in receipts:
            title, sha, sid, status = (
                r.get(k) for k in ("title", "sha", "submission_id", "status")
            )
            if (
                title not in titles
                or not sid
                or not re.fullmatch("[a-f0-9]{40}", str(sha))
            ):
                continue
            kind = ERRORS.get(status)
            score = r.get("score")
            base = None
            delta = None
            if status == "Pass" and _is_num(score):
                key = (title, r.get("account_id"))
                base = previous.get(key)
                previous[key] = r
                # Compare frozen scores only across distinct commits under identical public anchors.
                if (
                    not base
                    or base["sha"] == sha
                    or not key[1]
                    or not r.get("public_tbest_us")
                    or r["public_tbest_us"] != base.get("public_tbest_us")
                    or re.search(r"^(restore|rollback)", str(r.get("note", "")), re.I)
                ):
                    continue
                t0, t1 = (parse_iso(x.get("submitted_at")) for x in (base, r))
                if not t0 or not t1 or not 0 < (t1 - t0).total_seconds() <= 86400:
                    continue
                delta = round(score - base["score"], 4)
                if delta >= 0 or -delta < noise_floor:
                    continue
                kind = "score-drop"
            if not kind:
                continue
            ident = hashlib.sha256(
                json.dumps([scope, contest, title, sha, kind]).encode()
            ).hexdigest()[:20]
            cid = "BUG-" + ident
            folder = ws / "results" / title / sha
            local = read_json(folder / "result.json", {})
            at = r.get("submitted_at") or r.get("finished_at")
            validation_at = parse_iso(local.get("finished_at"))
            judged_at = parse_iso(at)
            escaped = bool(
                kind in ("WA", "RE", "TLE")
                and local.get("sha") == sha
                and local.get("exit_code") == 0
                and validation_at
                and judged_at
                and validation_at <= judged_at
                and (
                    local.get("verified") is True
                    or (
                        (folder / "validation.log").is_file()
                        and "EXTENDED_PASSED"
                        in (folder / "validation.log").read_text(errors="replace")
                    )
                )
            )
            detail = details.get((title, sid), {})
            count = detail if detail.get("status") == status else r
            total, passed = count.get("cases_total"), count.get("cases_passed")
            coverage = (
                f" · {passed}/{total}"
                if type(total) is int
                and type(passed) is int
                and 0 <= passed <= total
                and total > 0
                else ""
            )
            fix = (
                (fixes.get(f"{title}:{sha}") or {})
                if kind in ("WA", "RE", "TLE")
                else {}
            )
            lesson = prior.get(cid, {}).get("lesson", "")
            summary = f"{contest} · {sha[:8]} · {kind}{coverage}"
            if escaped:
                summary += " · local pass before judge failure"
            if delta is not None:
                summary += f" · {base['score']} → {score} ({delta:+.2f})"
            if fix.get("good_sha"):
                summary += " · verified fix " + fix["good_sha"][:8]
            if lesson:
                summary += " · " + lesson
            entry = cards.setdefault(
                cid,
                {
                    "id": cid,
                    "pillar": "bugs",
                    "workspace": scope,
                    "problem": title,
                    "sha": sha,
                    "category": kind,
                    "tags": [contest, title, kind],
                    "title": f"{title} · {kind}",
                    "status": "unverified",
                    "portability": "anti-pattern",
                    "evidence": [],
                    "lesson": lesson,
                },
            )
            entry.update(summary=summary, timestamp=at, fix=fix)
            entry["evidence"].append(
                {
                    "problem": title,
                    "workspace": scope,
                    "sha": sha,
                    "verdict": "fail",
                    "recorded_at": at,
                    "submission_id": sid,
                    "note": f"{sid} · {summary}",
                    "delta_raw": delta,
                    "baseline_sha": base["sha"] if base else None,
                    "cases_total": total,
                    "cases_passed": passed,
                }
            )
        updated = sorted(entries + list(cards.values()), key=lambda e: e["id"])
        changed = updated != doc["entries"]
        doc["entries"] = updated
        return doc, {"count": len(cards), "updated": changed}

    return _with_index_lock(index, rebuild)


def annotate_bug(entry_id, lesson):
    if not lesson.strip():
        raise ValueError("Provide a prevention or reproduction note")

    def annotate(doc):
        for e in doc["entries"]:
            if e["id"] == entry_id:
                old = e.get("lesson", "")
                if old:
                    e["summary"] = e["summary"].removesuffix(" · " + old)
                e["lesson"] = lesson.strip()
                e["summary"] += " · " + e["lesson"]
                return doc, {"id": entry_id, "lesson": e["lesson"]}
        raise ValueError("Unknown bug ID")

    return _with_index_lock(triad_kb_dirs()["bugs"] / "index.json", annotate)
