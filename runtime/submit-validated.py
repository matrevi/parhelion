#!/usr/bin/env python3
"""Submit source matching a successful server run and honor server rate limits."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
JUDGE_DIR = RUNTIME_DIR / "judge"
if str(JUDGE_DIR) not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR))
from ict_cli import ICTClient  # noqa: E402
sys.path.insert(0, str(RUNTIME_DIR.parent))
from scheduler.probes import OPENING_PROBES, compile_evidence, submitted_attempts
from scheduler.parhelion_paths import atomic_write_json


def _resolve_workspace(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_ws = os.environ.get("OPSB_WORKSPACE")
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    return Path.cwd().resolve()


def _load_problems(workspace: Path) -> list[dict]:
    for cand in (workspace / "opsb.workspace.json", workspace / "bench" / "problems.json"):
        if cand.is_file():
            data = json.loads(cand.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("problems"), list):
                return data["problems"]
            if isinstance(data, list):
                return data
    raise FileNotFoundError(f"No workspace problems manifest found under {workspace}")


def source_matches(repo: Path, sha: str, files: list[dict]) -> bool:
    if not any(item.get("editable") for item in files):
        return False
    for item in files:
        if not item.get("editable"):
            continue
        data = subprocess.run(
            ["git", "-C", str(repo), "show", f"{sha}:{item['path']}"],
            capture_output=True,
            text=True,
        )
        if data.returncode or data.stdout.replace("\r\n", "\n") != item["content"].replace("\r\n", "\n"):
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title")
    parser.add_argument("--workspace", "-w", help="Contest workspace directory (defaults to OPSB_WORKSPACE or cwd)")
    parser.add_argument("--sha", help="submit this validated commit instead of the working tree")
    parser.add_argument("--project-dir", help="read sources from this checkout (for an older validated SHA)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="relax the duplicate check (deliberate rollback of an earlier source)",
    )
    args = parser.parse_args()
    _trace_path = Path(os.environ.get("OPSB_SUBMIT_TRACE") or "/tmp/opsb-submit-trace.log")

    def trace(step: str, **fields: Any) -> None:
        try:
            with _trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "pid": os.getpid(), "step": step, **fields},
                                    ensure_ascii=False) + "\n")
        except Exception:
            pass

    trace("start", argv=sys.argv[1:], python=sys.version.split()[0], cwd=os.getcwd())
    workspace = _resolve_workspace(args.workspace)
    problem = next(p for p in _load_problems(workspace) if p["title"] == args.title)
    repo = workspace / problem["path"]

    trace("problem", title=args.title, sha=args.sha,
          problem_id=str(problem.get("problem_id") if isinstance(problem, dict) else ""), path=str(repo))
    client = ICTClient(session_file=os.environ.get("JUDGE_SESSION_FILE") or None)
    trace("session_loaded", ok=bool(client.load_session()))
    if not client.load_session():
        raise RuntimeError("Judge session missing — sign in via `opsb auth login` or Parhelion Settings")
    source_dir = Path(args.project_dir) if args.project_dir else repo
    try:
        payload = client.prepare_submission(problem["problem_id"], str(source_dir))
    except Exception as exc:
        trace("prepare_failed", error=str(exc)[:400])
        raise
    trace("prepared", files=[(f["path"], len(f["content"])) for f in payload.get("files", [])],
          source_dir=str(source_dir))
    records = [json.loads(p.read_text()) for p in (workspace / "results" / args.title).glob("*/result.json")]
    records.sort(key=lambda r: r["finished_at"], reverse=True)
    editable = [dict(f, editable=True) for f in payload["files"]]
    outcomes_path = workspace / "results" / "submit-queue-outcome.jsonl"
    outcomes = [json.loads(line) for line in outcomes_path.read_text().splitlines() if line.strip()] if outcomes_path.exists() else []
    history = client._get_json(f"/api/submissions/user/{client.user_id}/problem/{problem['problem_id']}")
    outcomes.extend({"title": args.title, "submission_id": row["_id"]} for row in history if row.get("_id"))
    probe = submitted_attempts(workspace, args.title, outcomes) < OPENING_PROBES
    candidates = [
        r for r in records if (args.sha is None or r["sha"] == args.sha)
        and (r["exit_code"] == 0 or (probe and compile_evidence(
            r, workspace / "results" / args.title / r["sha"] / "validation.log"
        )[0]))
    ]
    matched = next((r for r in candidates if source_matches(repo, r["sha"], editable)), None)
    if not matched:
        raise RuntimeError("Current source does not match any successful server validation")

    directory = workspace / "results" / args.title / matched["sha"]
    desired = {f["path"]: f["content"].replace("\r\n", "\n") for f in payload["files"]}
    for entry in ([] if args.force else sorted(history, key=lambda x: x["create_time"], reverse=True)):
        previous = client.get_submission(entry["_id"])
        previous_files = {f["path"]: f["content"].replace("\r\n", "\n") for f in previous.get("files", [])}
        if all(previous_files.get(k) == v for k, v in desired.items()):
            record = dict(existing=True, submission_id=entry["_id"], status=previous.get("status"),
                          submitted_at=entry["create_time"], validated_commit=matched["sha"],
                          account_id=os.environ.get("OPSB_ACCOUNT_ID") or "primary", user_id=client.user_id)
            atomic_write_json(directory / "submission-receipt.json", record)
            print(json.dumps(record), flush=True)
            return

    manifest = [{"path": f["path"], "sha256": hashlib.sha256(f["content"].encode()).hexdigest()} for f in payload["files"]]
    (directory / "submission-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    trace("posting", bytes=len(json.dumps(payload)))
    response = client.session.post(client.BASE_URL + "/api/submissions/submit", json=payload, timeout=30)
    trace("posted", status=response.status_code, body=response.text[:600])
    if response.status_code == 429:
        try:
            body = response.json()
        except ValueError:
            body = {}
        data = body.get("data") or {}
        try:
            import sys as _sys, pathlib as _pl
            _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "cli"))
            from opsb_core.services.judge_quota import write_quota  # type: ignore

            if data.get("used") is not None:
                write_quota(workspace, used=int(data["used"]), limit=int(data.get("limit") or 50), source="429")
        except Exception:
            pass
        next_try = 1
        if "daily_limit" in response.text:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            next_try = max(1, int((tomorrow - now).total_seconds()) + 1)
        retry_after = int(data.get("retry_after_seconds") or response.headers.get("Retry-After") or next_try)
        # Never sleep here: the caller owns scheduling. Sleeping burned the caller's 90s
        # subprocess timeout and turned a rate limit into a hard "rejected" verdict.
        print(json.dumps({
            "rate_limited": True,
            "retry_after": max(1, retry_after),
            "message": str(body.get("message") or body.get("msg") or "rate limited")[:200],
        }, ensure_ascii=False), flush=True)
        sys.exit(75)
    client._check_response(response)
    sid = (response.json().get("data") or {}).get("submissionId")
    if not sid:
        raise RuntimeError("Judge response missing submissionId; reconcile history before retry")
    record = dict(submission_id=sid, validated_commit=matched["sha"],
                  submitted_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  account_id=os.environ.get("OPSB_ACCOUNT_ID") or "primary", user_id=client.user_id)
    atomic_write_json(directory / "submission-receipt.json", record)
    trace("receipt", submission_id=sid, sha=matched["sha"])
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        try:
            with Path(os.environ.get("OPSB_SUBMIT_TRACE") or "/tmp/opsb-submit-trace.log").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "step": "exit", "code": exc.code}, ensure_ascii=False) + "\n")
        except Exception:
            pass
        raise
