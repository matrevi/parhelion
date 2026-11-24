"""Reconcile local submission/board state against the judge's own history.

Append judge-backed corrections keyed by submission ID and preserve each queue claim.

Usage:
    python -m opsb_core.services.submit_reconcile --workspace <ws> [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any, Iterable

from .judge_score_fetch import resolve_score_and_times, scan_ranking_pages
from .sync_reconciler import collapse_reconciled_outcomes

RESULT_TERMINAL = {"Pass", "Wrong Answer", "Runtime Error", "Compile Error", "Time Limit Exceeded"}


def _head_sha(repo: pathlib.Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=5).strip()
    except Exception:
        return ""


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def local_receipts(workspace: pathlib.Path) -> dict[str, dict[str, Any]]:
    """submission_id -> {title, sha} from local receipts."""
    out: dict[str, dict[str, Any]] = {}
    results = workspace / "results"
    if not results.is_dir():
        return out
    for rfile in results.glob("*/*/submission-receipt.json"):
        try:
            doc = json.loads(rfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = str(doc.get("submission_id") or "")
        if sid:
            out[sid] = {
                "title": rfile.parents[1].name,
                "sha": str(doc.get("validated_commit") or ""),
                "submitted_at": str(doc.get("submitted_at") or ""),
                "account_id": str(doc.get("account_id") or ""),
            }
    return out


def build_boards(
    problems: Iterable[dict[str, Any]],
    histories: dict[str, list[dict[str, Any]]],
    receipts: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (board_rows, virtual_rows) from judge histories.

    `histories[title]` is the judge's submission list for that problem; each entry must
    carry `_id`, `status`, `score` (or `finalScore`) and optionally `create_time`.
    """
    board, virtual = [], []
    for p in problems:
        title = str(p.get("title") or "")
        if not title:
            continue
        entries = histories.get(title) or []
        scored = []
        for e in entries:
            sid = str(e.get("_id") or "")
            score = e.get("score", e.get("finalScore"))
            try:
                score_f = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_f = None
            rec = receipts.get(sid) or {}
            scored.append({
                "submission_id": sid,
                "status": str(e.get("status") or ""),
                "score": score_f,
                "judge_at": str(e.get("create_time") or ""),
                "sha": rec.get("sha") or "",
                "sha_matches_head": False,
                "account_id": rec.get("account_id") or "primary",
                "cases_passed": e.get("cases_passed"),
                "cases_total": e.get("cases_total"),
            })
        if not scored:
            virtual.append({"title": title, "reason": "judge history empty", "local_score": None})
            continue
        best = max(scored, key=lambda r: (r["score"] or 0.0))
        board.append({
            "title": title,
            "problem_id": str(p.get("problem_id") or ""),
            "submissions": len(scored),
            "passed": any(a["status"] == "Pass" for a in scored),
            "best_score": best["score"],
            "best_submission_id": best["submission_id"],
            "best_sha": best["sha"],
            "best_status": best["status"],
            "attempts": scored,
        })
        if best["sha"] and not receipts.get(best["submission_id"]):
            virtual.append({"title": title, "reason": "judge row has no local receipt", "score": best["score"]})
    return board, virtual


def prune_queue(workspace: pathlib.Path, heads: dict[str, str]) -> dict[str, Any]:
    """Keep one pending row per problem, and only if its sha is the current HEAD."""
    queue_path = workspace / "results" / "submit-queue.jsonl"
    rows = _read_jsonl(queue_path)
    kept, dropped = [], []
    seen: set[str] = set()
    for r in rows:
        title = str(r.get("title") or "")
        sha = str(r.get("sha") or "")
        head = heads.get(title, "")
        if not title or title in seen:
            dropped.append({"title": title, "sha": sha[:7], "why": "duplicate or empty"})
            continue
        if head and sha and not head.startswith(sha[:7]):
            dropped.append({"title": title, "sha": sha[:7], "why": f"stale sha (head {head[:7]})"})
            continue
        seen.add(title)
        kept.append(r)
    return {"kept": kept, "dropped": dropped, "before": len(rows)}


def _match_manifest(workspace: pathlib.Path, title: str, detail: dict) -> str:
    files = {f["path"]: hashlib.sha256(f["content"].encode()).hexdigest()
             for f in detail.get("files") or [] if isinstance(f.get("content"), str)}
    matches = []
    for manifest in (workspace / "results" / title).glob("*/submission-manifest.json"):
        items = json.loads(manifest.read_text())
        if items and all(files.get(item["path"]) == item["sha256"] for item in items):
            matches.append(manifest.parent.name)
    return matches[0] if len(matches) == 1 else ""


def run(workspace: pathlib.Path, dry_run: bool = False) -> dict[str, Any]:
    from .judge_quota import beijing_date, write_quota

    meta = json.loads((workspace / "opsb.workspace.json").read_text(encoding="utf-8"))
    problems = [p for p in (meta.get("problems") or []) if p.get("title")]
    heads = {p["title"]: _head_sha(workspace / str(p.get("path") or p["title"])) for p in problems}

    receipts = local_receipts(workspace)
    histories: dict[str, list[dict[str, Any]]] = {}
    client = None
    try:
        judge_dir = pathlib.Path(__file__).resolve().parents[3] / "runtime" / "judge"
        if str(judge_dir) not in sys.path:
            sys.path.insert(0, str(judge_dir))
        from ict_cli import ICTClient  # type: ignore

        client = ICTClient()
        if not client.load_session():
            client = None
    except Exception:
        client = None
    if client is not None:
        for p in problems:
            pid = str(p.get("problem_id") or "")
            if not pid:
                continue
            entries = client._get_json(f"/api/submissions/user/{client.user_id}/problem/{pid}")
            if not isinstance(entries, list):
                raise ValueError(f"Unexpected judge history for {p['title']}")
            for entry in entries:
                sid = entry["_id"]
                status = entry.get("status")
                if status not in RESULT_TERMINAL:
                    continue
                detail = client.get_submission(sid)
                ranking = scan_ranking_pages(
                    lambda page: client._get_json(f"/api/problems/{pid}/ranking?page={page}&size=100"), sid
                ) if status == "Pass" else None
                entry["score"], _, _ = resolve_score_and_times(detail, ranking, status)
                cases = detail.get("result") or []
                entry["cases_total"] = len(cases)
                entry["cases_passed"] = sum(case.get("testcase_status") == "Pass" for case in cases)
                if sid not in receipts:
                    receipts[sid] = {"sha": _match_manifest(workspace, p["title"], detail)}
            histories[p["title"]] = entries

    # Daily quota: count the judge's submissions whose create_time falls on today's
    # Beijing date (this is what the UI must show as used/limit).
    try:
        today = beijing_date()
        used_today = 0
        for entries in histories.values():
            for e in entries:
                stamp = str(e.get("create_time") or "")
                try:
                    ts = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d") == today:
                    used_today += 1
        if not dry_run and client is not None:
            write_quota(workspace, used=used_today, source="judge-history")
        report_quota = {"used": used_today, "beijing_date": today}
    except Exception:
        report_quota = {}
    board, virtual = build_boards(problems, histories, receipts)
    for row in board:
        head = heads.get(row["title"], "")
        for att in row["attempts"]:
            att["sha_matches_head"] = bool(head and att.get("sha") and head.startswith(str(att["sha"])[:7]))
    rows = _read_jsonl(workspace / "results" / "submit-queue.jsonl")

    report = {
        "workspace": str(workspace),
        "problems": len(problems),
        "judge_reachable": client is not None,
        "board": board,
        "virtual": virtual,
        "queue": {"before": len(rows), "kept": len(rows), "dropped": []},
        "dry_run": dry_run,
        "quota": report_quota,
    }
    if not dry_run:
        if client is None:
            raise RuntimeError("Sign in to the judge before reconciling")
        results = workspace / "results"
        ev = results / "scoring-evidence"
        ev.mkdir(parents=True, exist_ok=True)
        outcome_path = results / "submit-queue-outcome.jsonl"
        added = 0
        with (results / ".submit-queue-io.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing = collapse_reconciled_outcomes(_read_jsonl(outcome_path))
            known = {r["submission_id"]: r for r in existing if r.get("submission_id")}
            with outcome_path.open("a", encoding="utf-8") as fh:
                for row in board:
                    for att in row["attempts"]:
                        sid = att["submission_id"]
                        if not sid or att["status"] not in RESULT_TERMINAL:
                            continue
                        prev = known.get(sid, {})
                        sha = att["sha"] or (prev.get("sha") if prev.get("source") != "judge-reconcile" else "") or ""
                        score = att["score"]
                        if score is None and prev.get("source") != "judge-reconcile":
                            score = prev.get("score")
                        if prev and (prev.get("status"), prev.get("score"), prev.get("sha")) == (att["status"], score, sha) and prev.get("source") != "judge-reconcile":
                            continue
                        record = {**prev, "title": row["title"], "sha": sha, "status": att["status"],
                                  "score": score, "submission_id": sid, "submitted_at": att["judge_at"],
                                  "finished_at": prev.get("finished_at") or att["judge_at"],
                                  "account_id": prev.get("account_id") or att["account_id"],
                                  "cases_passed": att["cases_passed"], "cases_total": att["cases_total"],
                                  "note": f"Judge · {att['cases_passed']}/{att['cases_total']} cases",
                                  "source": "judge-history"}
                        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                        added += 1
            report["history_backfilled"] = added
            from scheduler.parhelion_paths import atomic_write_json
            atomic_write_json(ev / "board-reconciled.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", "-w", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = run(pathlib.Path(args.workspace).expanduser().resolve(), dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"problems={report['problems']} judge_reachable={report['judge_reachable']} queue {report['queue']['before']}->{report['queue']['kept']}")
        for row in report["board"]:
            print(f"  {row['title']:<26} passed={row['passed']} best={row['best_score']} sha={str(row['best_sha'])[:7]} status={row['best_status']}")
        for v in report["virtual"]:
            print(f"  VIRTUAL {v['title']}: {v['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
