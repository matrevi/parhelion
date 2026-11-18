"""Zero-noise contest workspace scaffolder."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import utc_now
from .breakthrough_service import scaffold_breakthrough_dir
from .templates_md import DESIGN_MD, PROBLEM_AGENTS_MD, PROBLEM_SPEC_MD, SCORE_HISTORY_MD, WORKSPACE_AGENTS_MD


def _slugify(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum() or ch == "-").strip("-") or "op"


def scaffold_workspace(
    target_dir: Path,
    contest_title: str = "KernelC Operator Optimization Contest",
    contest_id: str = "",
    git_owner: str = "winbeau",
    repo_prefix: str = "accel-ops",
    npu_host: str = "npu",
    problems: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target_dir = target_dir.expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    if not problems:
        problems = [{"title": "SampleCustomOp", "difficulty": 2, "sdk_version": "9.1.0"}]

    normalized = []
    for p in problems:
        title = p["title"]
        slug = p.get("slug") or _slugify(title)
        diff = int(p.get("difficulty", 2))
        repo_slug = p.get("repo_slug") or (f"{repo_prefix}-{slug}" if repo_prefix else slug)
        normalized.append(
            {
                "title": title,
                "slug": slug,
                "repo_slug": repo_slug,
                "problem_id": p.get("problem_id", ""),
                "difficulty": diff,
                "sdk_version": p.get("sdk_version", "9.1.0"),
                "path": title,
            }
        )

    ws_meta = {
        "schema_version": 1,
        "layout": "clean-agents-first",
        "created_at": utc_now().isoformat(),
        "title": contest_title,
        "contest_id": contest_id,
        "git_owner": git_owner,
        "repo_prefix": repo_prefix,
        "npu_ssh_host": npu_host,
        "problems": normalized,
    }

    (target_dir / "opsb.workspace.json").write_text(json.dumps(ws_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (target_dir / ".gitignore").write_text(".opsb/\n.DS_Store\n__pycache__/\n*.pyc\nbuild_out/\nPROF_*\n*.svg\nsession.json\n", encoding="utf-8")
    (target_dir / "AGENTS.md").write_text(WORKSPACE_AGENTS_MD.format(contest_title=contest_title), encoding="utf-8")

    results_dir = target_dir / "results"
    results_dir.mkdir(exist_ok=True)
    bt_dir = scaffold_breakthrough_dir(target_dir)
    if not (results_dir / "score-snapshot.json").exists():
        snap = {p["title"]: {"rank": None, "status": "no-submission", "score": 0.0, "public_times_us": []} for p in normalized}
        (results_dir / "score-snapshot.json").write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")

    created_dirs: list[str] = []
    for p in normalized:
        p_dir = target_dir / p["path"]
        for sub in ("op_host", "op_kernel", "tests"):
            (p_dir / sub).mkdir(parents=True, exist_ok=True)
        (p_dir / "AGENTS.md").write_text(
            PROBLEM_AGENTS_MD.format(
                problem=p["title"],
                difficulty=p["difficulty"],
                git_owner=git_owner,
                repo_slug=p["repo_slug"],
                npu_host=npu_host,
            ),
            encoding="utf-8",
        )
        if not (p_dir / "PROBLEM.md").exists():
            (p_dir / "PROBLEM.md").write_text(
                PROBLEM_SPEC_MD.format(
                    problem=p["title"],
                    problem_id=p["problem_id"] or "TBD",
                    difficulty=p["difficulty"],
                    sdk_version=p["sdk_version"],
                ),
                encoding="utf-8",
            )
        if not (p_dir / "DESIGN.md").exists():
            (p_dir / "DESIGN.md").write_text(DESIGN_MD.format(problem=p["title"]), encoding="utf-8")
        if not (p_dir / "SCORE_HISTORY.md").exists():
            (p_dir / "SCORE_HISTORY.md").write_text(SCORE_HISTORY_MD.format(problem=p["title"]), encoding="utf-8")
        created_dirs.append(str(p_dir))

    return {
        "workspace": str(target_dir),
        "manifest": str(target_dir / "opsb.workspace.json"),
        "agents_md": str(target_dir / "AGENTS.md"),
        "problem_count": len(normalized),
        "problem_dirs": created_dirs,
    }
