"""Reconcile local submission/board state against the judge's own history.

There is no end-to-end test for this pipeline, so the judge history plus our JSONL
feedback log are the only ground truth. This tool rebuilds the board from that ground
truth, reports "virtual" rows (local score with no submission id), and prunes the queue to
one HEAD-sha row per problem.

Usage:
    python -m opsb_core.services.submit_reconcile --workspace <ws> [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any, Iterable

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
                "account_id": rec.get("account_id") or "",
            })
        if not scored:
            virtual.append({"title": title, "reason": "judge history empty", "local_score": None})
            continue
        best = max(scored, key=lambda r: (r["score"] or 0.0))
        board.append({
            "title": title,
            "problem_id": str(p.get("problem_id") or ""),
            "submissions": len(scored),
            "passed": (best["score"] or 0.0) > 0,
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


def run(workspace: pathlib.Path, dry_run: bool = False) -> dict[str, Any]:
    from ..commands.admin_cmd import _parse_problem_spec  # noqa: F401  (kept for parity)

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
            try:
                entries = client._get_json(
                    f"/api/submissions/user/{client.user_id}/problem/{pid}"
                ) or []
            except Exception:
                entries = []
            # The list endpoint omits the score; fetch details for accepted submissions so
            # the board shows the judge's number instead of a local estimate.
            for e in entries[:6]:
                if str(e.get("status")) not in RESULT_TERMINAL:
                    continue
                try:
                    detail = client.get_submission(str(e.get("_id") or "")) or {}
                except Exception:
                    continue
                cases = detail.get("result") or []
                # The judge stores per-case time vs best_time; the score is derived:
                # 2 pts per case scaled by best_time/time, so a slow-but-correct kernel
                # still scores partially. This is the only real number we can trust.
                total = 0.0
                passed = 0
                ratios = []
                for case in cases:
                    if str(case.get("testcase_status")) != "Pass":
                        continue
                    passed += 1
                    t = float(case.get("time") or 0.0)
                    bt = float(case.get("best_time") or 0.0)
                    ratio = 1.0 if (t <= 0 or bt <= 0) else min(1.0, bt / t)
                    ratios.append(round(ratio, 4))
                    total += 2.0 * ratio
                e["score"] = round(total, 2)
                e["cases_passed"] = passed
                e["cases_total"] = len(cases)
                e["case_ratios"] = ratios
                e["valid"] = detail.get("valid")
            histories[p["title"]] = entries

    # Daily quota: count the judge's submissions whose create_time falls on today's
    # Beijing date (this is what the UI must show as used/limit).
    try:
        from .judge_quota import beijing_date, write_quota  # type: ignore

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
        write_quota(workspace, used=used_today, source="judge-history")
        report_quota = {"used": used_today, "beijing_date": today}
    except Exception:
        report_quota = {}
    board, virtual = build_boards(problems, histories, receipts)
    for row in board:
        head = heads.get(row["title"], "")
        for att in row["attempts"]:
            att["sha_matches_head"] = bool(head and att.get("sha") and head.startswith(str(att["sha"])[:7]))
    pruned = prune_queue(workspace, heads)

    report = {
        "workspace": str(workspace),
        "problems": len(problems),
        "judge_reachable": client is not None,
        "board": board,
        "virtual": virtual,
        "queue": {"before": pruned["before"], "kept": len(pruned["kept"]), "dropped": pruned["dropped"]},
        "dry_run": dry_run,
        "quota": report_quota,
    }
    if not dry_run:
        ev = workspace / "results" / "scoring-evidence"
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "board-reconciled.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if pruned["dropped"]:
            backup = workspace / "results" / f"submit-queue.backup-{hashlib.sha1(json.dumps(pruned['dropped']).encode()).hexdigest()[:8]}.jsonl"
            backup.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in pruned["dropped"]), encoding="utf-8")
        (workspace / "results" / "submit-queue.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in pruned["kept"]), encoding="utf-8"
        )
        # Backfill history rows for submissions the judge knows about but our queue never
        # recorded (submitted outside the dispatcher, or receipts lost). Without this the
        # history shows only local interceptions and every real attempt looks scoreless.
        try:
            outcome_path = workspace / "results" / "submit-queue-outcome.jsonl"
            existing = _read_jsonl(outcome_path)
            known = {str(r.get("submission_id")) for r in existing if r.get("submission_id")}
            added = 0
            with outcome_path.open("a", encoding="utf-8") as fh:
                for row in board:
                    for att in row.get("attempts", []):
                        sid = str(att.get("submission_id") or "")
                        if not sid or sid in known:
                            continue
                        fh.write(json.dumps({
                            "title": row["title"],
                            "sha": row.get("best_sha") or "",
                            "status": att.get("status"),
                            "score": att.get("score"),
                            "cases_passed": att.get("cases_passed"),
                            "cases_total": att.get("cases_total"),
                            "submission_id": sid,
                            "finished_at": att.get("judge_at") or "",
                            "note": f"judge backfill · {att.get('cases_passed')}/{att.get('cases_total')} cases",
                            "source": "judge-reconcile",
                        }, ensure_ascii=False) + "\n")
                        known.add(sid)
                        added += 1
            report["history_backfilled"] = added
        except Exception:
            report["history_backfilled"] = 0
        try:
            from ...scheduler.feedback_log import log_feedback  # type: ignore

            log_feedback(workspace, "reconcile", board=board, virtual=virtual, queue=report["queue"])
        except Exception:
            try:
                sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
                from scheduler.feedback_log import log_feedback  # type: ignore

                log_feedback(workspace, "reconcile", board=board, virtual=virtual, queue=report["queue"])
            except Exception:
                pass
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
