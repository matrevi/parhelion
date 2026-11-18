"""Zero-noise contest workspace scaffolder v2 (with stratified bench/ & frozen SHA-256 oracle)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import utc_now
from .breakthrough_service import scaffold_breakthrough_dir
from .templates_md import DESIGN_MD, PROBLEM_AGENTS_MD, PROBLEM_SPEC_MD, SCORE_HISTORY_MD, WORKSPACE_AGENTS_MD


def _slugify(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum() or ch == "-").strip("-") or "op"


def scaffold_operator_bench(op_dir: Path, title: str) -> dict[str, Any]:
    bench_dir = op_dir / "bench"
    bench_dir.mkdir(parents=True, exist_ok=True)

    ref_py = bench_dir / "reference.py"
    if not ref_py.exists():
        ref_py.write_text(
            f'"""Frozen FP64/exact golden oracle for {title}. DO NOT MODIFY ONCE LOCKED."""\n'
            "import numpy as np\n\n\n"
            "def golden_oracle(inputs: dict) -> dict:\n"
            "    raise NotImplementedError('Implement exact specification oracle before locking SHA-256')\n",
            encoding="utf-8",
        )
    ref_hash = hashlib.sha256(ref_py.read_bytes()).hexdigest()

    corr_path = bench_dir / "cases_correctness.json"
    if not corr_path.exists():
        corr_spec = {
            "schema_version": 2,
            "operator": title,
            "canary_fill": "0x5A_NaN_32B_guard",
            "axes": {
                "geometry": ["G0_DEGEN", "G1_ODD_ALIGN", "G2_CORE_TILE", "G3_HIGH_RANK", "G4_DTYPE_CROSS", "G5_EDGE_ATTR"],
                "distribution": ["D0_STD", "D1_OVERFLOW", "D2_SUBNORM", "D3_CANCEL", "D4_ULP_INF", "D5_ESCAPE_REPRO"],
            },
            "cases": [
                {"id": "g0_d0_scalar", "geometry": "G0_DEGEN", "distribution": "D0_STD", "shape": [1], "dtype": "float32", "verified": True},
                {"id": "g1_d3_odd33", "geometry": "G1_ODD_ALIGN", "distribution": "D3_CANCEL", "shape": [5, 33], "dtype": "bfloat16", "verified": True},
                {"id": "g2_d1_tile41", "geometry": "G2_CORE_TILE", "distribution": "D1_OVERFLOW", "shape": [41, 4097], "dtype": "float16", "verified": True},
            ],
        }
        corr_path.write_text(json.dumps(corr_spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    perf_path = bench_dir / "cases_perf.json"
    if not perf_path.exists():
        perf_spec = {
            "schema_version": 2,
            "operator": title,
            "append_only": True,
            "protocol": {"session": "single_profiler", "warmup": 2, "repeat": 5, "sync_per_iter": True},
            "strata_priors": {
                "PUBLIC": 0.10,
                "S_SMALL_CORE": 0.27,
                "M_MID_UB": 0.315,
                "L_HBM_TILED": 0.18,
                "X_EXTREME": 0.135,
            },
            "buckets": [
                {"id": "P1", "stratum": "PUBLIC", "shape": [16, 256], "dtype": "float32"},
                {"id": "S1", "stratum": "S_SMALL_CORE", "shape": [8, 512], "dtype": "float16"},
                {"id": "M1", "stratum": "M_MID_UB", "shape": [64, 4096], "dtype": "bfloat16"},
                {"id": "L1", "stratum": "L_HBM_TILED", "shape": [256, 16384], "dtype": "float16"},
                {"id": "X1", "stratum": "X_EXTREME", "shape": [3, 4097], "dtype": "float32", "max_weight": 0.04},
            ],
        }
        perf_path.write_text(json.dumps(perf_spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {"bench_dir": str(bench_dir), "reference_sha256": ref_hash}


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

    results_dir = target_dir / "results"
    results_dir.mkdir(exist_ok=True)
    scaffold_breakthrough_dir(target_dir)
    if not (results_dir / "score-snapshot.json").exists():
        snap = {p["title"]: {"rank": None, "status": "no-submission", "score": 0.0, "public_times_us": []} for p in normalized}
        (results_dir / "score-snapshot.json").write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")

    created_dirs: list[str] = []
    frozen_oracles: dict[str, str] = {}
    for p in normalized:
        p_dir = target_dir / p["path"]
        for sub in ("op_host", "op_kernel", "bench"):
            (p_dir / sub).mkdir(parents=True, exist_ok=True)
        b_info = scaffold_operator_bench(p_dir, p["title"])
        frozen_oracles[p["title"]] = b_info["reference_sha256"]
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

    ws_meta = {
        "schema_version": 2,
        "layout": "clean-agents-first",
        "created_at": utc_now().isoformat(),
        "title": contest_title,
        "contest_id": contest_id,
        "git_owner": git_owner,
        "repo_prefix": repo_prefix,
        "npu_ssh_host": npu_host,
        "problems": normalized,
        "frozen_oracles": frozen_oracles,
    }
    (target_dir / "opsb.workspace.json").write_text(json.dumps(ws_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (target_dir / ".gitignore").write_text(".opsb/\n.DS_Store\n__pycache__/\n*.pyc\nbuild_out/\nPROF_*\n*.svg\nsession.json\n", encoding="utf-8")
    (target_dir / "AGENTS.md").write_text(WORKSPACE_AGENTS_MD.format(contest_title=contest_title), encoding="utf-8")

    return {
        "workspace": str(target_dir),
        "manifest": str(target_dir / "opsb.workspace.json"),
        "agents_md": str(target_dir / "AGENTS.md"),
        "problem_count": len(normalized),
        "problem_dirs": created_dirs,
    }
