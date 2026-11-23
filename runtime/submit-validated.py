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

RUNTIME_DIR = Path(__file__).resolve().parent
JUDGE_DIR = RUNTIME_DIR / "judge"
if str(JUDGE_DIR) not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR))
from ict_cli import ICTClient  # noqa: E402


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
    workspace = _resolve_workspace(args.workspace)
    problem = next(p for p in _load_problems(workspace) if p["title"] == args.title)
    repo = workspace / problem["path"]

    client = ICTClient(session_file=os.environ.get("JUDGE_SESSION_FILE") or None)
    if not client.load_session():
        raise RuntimeError("Judge session missing — sign in via `opsb auth login` or Parhelion Settings")
    source_dir = Path(args.project_dir) if args.project_dir else repo
    payload = client.prepare_submission(problem["problem_id"], str(source_dir))
    records = [json.loads(p.read_text()) for p in (workspace / "results" / args.title).glob("*/result.json")]
    records.sort(key=lambda r: r["finished_at"], reverse=True)
    editable = [dict(f, editable=True) for f in payload["files"]]
    candidates = [r for r in records if r["exit_code"] == 0 and (args.sha is None or r["sha"] == args.sha)]
    matched = next((r for r in candidates if source_matches(repo, r["sha"], editable)), None)
    if not matched:
        raise RuntimeError("Current source does not match any successful server validation")

    history = client._get_json(f"/api/submissions/user/{client.user_id}/problem/{problem['problem_id']}")
    desired = {f["path"]: f["content"].replace("\r\n", "\n") for f in payload["files"]}
    for entry in ([] if args.force else sorted(history, key=lambda x: x["create_time"], reverse=True)):
        previous = client.get_submission(entry["_id"])
        previous_files = {f["path"]: f["content"].replace("\r\n", "\n") for f in previous.get("files", [])}
        if all(previous_files.get(k) == v for k, v in desired.items()):
            print(
                json.dumps(
                    {
                        "existing": True,
                        "submission_id": entry["_id"],
                        "ID": previous["ID"],
                        "status": previous["status"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return

    directory = workspace / "results" / args.title / matched["sha"]
    manifest = [{"path": f["path"], "sha256": hashlib.sha256(f["content"].encode()).hexdigest()} for f in payload["files"]]
    (directory / "submission-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for _ in range(4):
        response = client.session.post(client.BASE_URL + "/api/submissions/submit", json=payload, timeout=30)
        if response.status_code == 429:
            try:
                message = str(response.json().get("message", ""))
            except ValueError:
                message = ""
            if "daily_limit" in str(response.text):
                raise RuntimeError("daily submission limit reached; next window at 00:00 Beijing time")
            delay = response.headers.get("Retry-After")
            if not delay:
                match = re.search(r"(\d+)\s*(?:s|seconds?)", message)
                delay = match[1] if match else "180"
            delay_int = min(600, max(1, int(delay))) + 1
            print(f"Rate limited; retry after {delay_int} seconds", flush=True)
            deadline = time.monotonic() + delay_int
            while time.monotonic() < deadline:
                time.sleep(min(30, deadline - time.monotonic()))
            continue
        client._check_response(response)
        sid = response.json().get("data", {}).get("submissionId")
        if not sid:
            raise RuntimeError("Submission response has no ID; query history before retrying")
        record = dict(
            submission_id=sid,
            validated_commit=matched["sha"],
            submitted_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            account_id=os.environ.get("OPSB_ACCOUNT_ID") or "primary",
            user_id=client.user_id,
        )
        (directory / "submission-receipt.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record), flush=True)
        return
    raise RuntimeError("Server rate limit persisted after four attempts")


if __name__ == "__main__":
    main()
