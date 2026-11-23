"""Shared constants, path resolvers, artifact loaders, and parsers for Fast-Feedback tools."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import config as _cfg_mod
from ..config import SUPERVISOR_ROOT, read_json, workspace_id
from ..services.contest_service import resolve_workspace_meta

DEFAULT_ELF_THRESHOLDS: dict[str, float | int] = {
    "warn_text_bytes": 24576,
    "fail_text_bytes": 32768,
    "warn_o_max_bytes": 98304,
    "fail_o_max_bytes": 147456,
    "warn_build_sec": 90.0,
    "fail_build_sec": 150.0,
    "warn_templates": 48,
    "fail_templates": 72,
}

DEFAULT_COLD3_THRESHOLDS: dict[str, float] = {
    "warn_cold_tax_us": 0.8,
    "redline_cold_tax_us": 2.0,
    "warn_spread_us": 1.0,
    "redline_spread_us": 2.0,
    "spike_ratio": 1.45,
    "spike_diff_us": 1.5,
}

VALID_OPERATOR_FAMILIES: tuple[str, ...] = (
    "elementwise",
    "reduction",
    "transpose",
    "gather_scatter",
    "pad_slice",
    "sequence_dp",
)

_DTYPE_ALIASES: dict[str, str] = {
    "fp32": "float32", "f32": "float32", "float32": "float32",
    "fp16": "float16", "f16": "float16", "half": "float16", "float16": "float16",
    "bf16": "bfloat16", "bfloat16": "bfloat16",
    "i32": "int32", "int32": "int32",
    "i64": "int64", "int64": "int64",
    "i8": "int8", "int8": "int8",
    "u8": "uint8", "uint8": "uint8",
}

_EMPTY_PERF_PARSE: dict[str, Any] = {
    "cases": {},
    "profiler_cases": {},
    "profiler_min_cases": {},
    "profiler_max_cases": {},
    "profiler_first_iter_cases": {},
    "profiler_block_dims": {},
    "kernel_binary_stats": {},
    "cold_start_stats": {},
    "torch_cases": {},
    "prof_kernel_stats": {},
    "prof_summary": {},
    "runtime_soc": None,
    "source": "none",
}


def _parhelion_home() -> Path:
    """Resolve Parhelion home dynamically so PARHELION_HOME/OPSB_HOME test overrides work."""
    env_home = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return Path(_cfg_mod.PARHELION_HOME).expanduser().resolve()


def _bench_specs_dir(workspace: Path | str) -> Path:
    return _parhelion_home() / "workspaces" / workspace_id(workspace) / "bench-specs"


def _load_fast_tools_settings() -> dict[str, Any]:
    cfg = read_json(_parhelion_home() / "config.json", {})
    if isinstance(cfg, dict) and isinstance(cfg.get("fast_tools"), dict):
        return dict(cfg["fast_tools"])
    return {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _first_num(*vals: Any) -> float | int | None:
    fallback_zero: float | int | None = None
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if v > 0:
                return v
            if fallback_zero is None:
                fallback_zero = v
    return fallback_zero


def _to_float_list(val: Any) -> list[float]:
    if not isinstance(val, list):
        return []
    return [float(x) for x in val if isinstance(x, (int, float)) and not isinstance(x, bool) and float(x) > 0]


def _to_ratio(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f / 100.0 if f > 1.5 else f
    if isinstance(val, str):
        s = val.strip()
        is_pct = s.endswith("%")
        try:
            f = float(s.rstrip("%").strip())
        except ValueError:
            return None
        return f / 100.0 if (is_pct or f > 1.5) else f
    return None


def _parse_validation_perf_safe(log_text: str) -> dict[str, Any]:
    """Use `scheduler.estimator.parse_validation_perf` with safe fallback."""
    if not log_text:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _EMPTY_PERF_PARSE.items()}
    try:
        if str(SUPERVISOR_ROOT) not in sys.path:
            sys.path.insert(0, str(SUPERVISOR_ROOT))
        from scheduler.estimator import parse_validation_perf  # type: ignore

        return parse_validation_perf(log_text)
    except Exception:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _EMPTY_PERF_PARSE.items()}


def _resolve_problem_info(workspace: Path, title: str) -> tuple[str, Path]:
    ws = workspace.expanduser().resolve()
    meta = resolve_workspace_meta(ws)
    for prob in meta.get("problems") or []:
        pt = str(prob.get("title") or "")
        if pt.lower() == title.lower():
            rel = str(prob.get("path") or pt)
            cand_repo = ws / rel
            if not cand_repo.exists() and (ws / "bench" / pt).exists():
                cand_repo = ws / "bench" / pt
            return pt, cand_repo

    results_root = ws / "results"
    if results_root.is_dir():
        for d in results_root.iterdir():
            if d.is_dir() and d.name.lower() == title.lower():
                pt = d.name
                cand_repo = (ws / "bench" / pt) if (ws / "bench" / pt).is_dir() else (ws / pt)
                return pt, cand_repo

    cand_repo = (ws / "bench" / title) if (ws / "bench" / title).is_dir() else (ws / title)
    return title, cand_repo


def _repo_git_head(repo_dir: Path) -> str | None:
    if not (repo_dir / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if proc.returncode == 0:
            head = proc.stdout.strip()
            if head:
                return head
    except Exception:
        pass
    return None


def _load_run_artifacts(
    workspace: Path,
    title: str,
    sha: str | None = None,
    require_passing: bool = False,
    allow_build_logs: bool = False,
) -> dict[str, Any]:
    """Locate `results/<Title>/<SHA>/` and read result JSON + validation/build logs."""
    ws = workspace.expanduser().resolve()
    canonical_title, repo_dir = _resolve_problem_info(ws, title)
    res_dir = ws / "results" / canonical_title
    empty = {
        "title": canonical_title,
        "repo_dir": repo_dir,
        "sha": sha or "",
        "sha_source": "none",
        "run_dir": None,
        "result_json": {},
        "log_text": "",
        "log_path": None,
    }
    if not res_dir.is_dir():
        return empty

    json_names = ("result.json", "result-fast.json", "result-build.json") if allow_build_logs else ("result.json", "result-fast.json")
    log_names = ("validation.log", "validation-fast.log", "build.log") if allow_build_logs else ("validation.log", "validation-fast.log")

    def _inspect_dir(rdir: Path) -> dict[str, Any] | None:
        rj: dict[str, Any] = {}
        rj_mtime = 0.0
        for jn in json_names:
            jp = rdir / jn
            if jp.is_file():
                data = read_json(jp, {})
                if isinstance(data, dict):
                    rj, rj_mtime = data, jp.stat().st_mtime
                    break
        log_text, log_path, log_mtime = "", None, 0.0
        for ln in log_names:
            lp = rdir / ln
            if lp.is_file():
                log_path = lp
                log_text = lp.read_text(encoding="utf-8", errors="replace")
                log_mtime = lp.stat().st_mtime
                break
        if not rj and log_path is None:
            return None
        exit_code = rj.get("exit_code")
        passed = (exit_code == 0) or (
            exit_code is None and ("EXTENDED_PASSED" in log_text or "BUILD_ONLY_PASSED" in log_text)
        )
        return {
            "sha": rdir.name,
            "run_dir": rdir,
            "result_json": rj,
            "log_text": log_text,
            "log_path": log_path,
            "passed": passed,
            "has_full_val": (rdir / "validation.log").is_file() or (rdir / "result.json").is_file(),
            "sort_key": (str(rj.get("finished_at") or ""), max(rj_mtime, log_mtime)),
        }

    chosen: dict[str, Any] | None = None
    sha_source = "none"
    if sha:
        target_low = sha.strip().lower()
        exact = res_dir / sha.strip()
        if exact.is_dir():
            chosen = _inspect_dir(exact)
        if chosen is None:
            for mdir in sorted(res_dir.iterdir()):
                if mdir.is_dir() and mdir.name.lower().startswith(target_low):
                    cand = _inspect_dir(mdir)
                    if cand is not None:
                        chosen = cand
                        break
        if chosen is not None:
            sha_source = "explicit"
    else:
        candidates = [info for d in res_dir.iterdir() if d.is_dir() and (info := _inspect_dir(d)) is not None]
        head_sha = _repo_git_head(repo_dir)
        if head_sha and (res_dir / head_sha).is_dir():
            head_cand = _inspect_dir(res_dir / head_sha)
            if head_cand is not None and (
                not require_passing or head_cand["passed"] or not any(c["passed"] for c in candidates)
            ):
                chosen = head_cand
                sha_source = "git_head"
        if chosen is None and candidates:
            if require_passing:
                passing = [c for c in candidates if c["passed"] and c["has_full_val"]] or [c for c in candidates if c["passed"]]
                pool = passing or candidates
            else:
                pool = candidates
            pool.sort(key=lambda c: (1 if c["has_full_val"] else 0, c["sort_key"]), reverse=True)
            chosen = pool[0]
            sha_source = "latest_mtime"

    if chosen is None:
        return empty
    return {
        "title": canonical_title,
        "repo_dir": repo_dir,
        "sha": chosen["sha"],
        "sha_source": sha_source,
        "run_dir": chosen["run_dir"],
        "result_json": chosen["result_json"],
        "log_text": chosen["log_text"],
        "log_path": chosen["log_path"],
    }
