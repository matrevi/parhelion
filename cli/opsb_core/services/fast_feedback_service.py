"""Parhelion v3 Fast-Feedback Tools (`elf_guard`, `cold3_probe`, `pmu_torch_card`, `bench_scaffold`).

Pure Python 3 stdlib + `opsb_core.config` / `scheduler.estimator` helpers.
Runs 100% offline on logged build/validation artifacts without live SSH calls.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from .. import config as _cfg_mod
from ..config import SUPERVISOR_ROOT, read_json, utc_now, workspace_id
from .contest_service import resolve_workspace_meta

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
    "fp32": "float32",
    "f32": "float32",
    "float32": "float32",
    "fp16": "float16",
    "f16": "float16",
    "half": "float16",
    "float16": "float16",
    "bf16": "bfloat16",
    "bfloat16": "bfloat16",
    "i32": "int32",
    "int32": "int32",
    "i64": "int64",
    "int64": "int64",
    "i8": "int8",
    "int8": "int8",
    "u8": "uint8",
    "uint8": "uint8",
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


def _parse_validation_perf_safe(log_text: str) -> dict[str, Any]:
    """Use `scheduler.estimator.parse_validation_perf` with safe fallback."""
    if not log_text:
        return {
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
    try:
        if str(SUPERVISOR_ROOT) not in sys.path:
            sys.path.insert(0, str(SUPERVISOR_ROOT))
        from scheduler.estimator import parse_validation_perf  # type: ignore

        return parse_validation_perf(log_text)
    except Exception:
        return {
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

    if not res_dir.is_dir():
        return {
            "title": canonical_title,
            "repo_dir": repo_dir,
            "sha": sha or "",
            "run_dir": None,
            "result_json": {},
            "log_text": "",
            "log_path": None,
        }

    def _inspect_dir(rdir: Path) -> dict[str, Any] | None:
        json_names = ("result.json", "result-fast.json", "result-build.json") if allow_build_logs else ("result.json", "result-fast.json")
        log_names = ("validation.log", "validation-fast.log", "build.log") if allow_build_logs else ("validation.log", "validation-fast.log")
        rj: dict[str, Any] = {}
        rj_mtime = 0.0
        for jn in json_names:
            jp = rdir / jn
            if jp.is_file():
                data = read_json(jp, {})
                if isinstance(data, dict):
                    rj = data
                    rj_mtime = jp.stat().st_mtime
                    break
        log_text = ""
        log_path: Path | None = None
        log_mtime = 0.0
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
        # Rank sort key: finished_at ISO if present, else mtime
        ts_str = str(rj.get("finished_at") or "")
        return {
            "sha": rdir.name,
            "run_dir": rdir,
            "result_json": rj,
            "log_text": log_text,
            "log_path": log_path,
            "passed": passed,
            "has_full_val": (rdir / "validation.log").is_file() or (rdir / "result.json").is_file(),
            "sort_key": (ts_str, max(rj_mtime, log_mtime)),
        }

    chosen: dict[str, Any] | None = None
    if sha:
        target_low = sha.strip().lower()
        exact = res_dir / sha.strip()
        if exact.is_dir():
            chosen = _inspect_dir(exact)
        if chosen is None:
            matches = [
                d for d in sorted(res_dir.iterdir())
                if d.is_dir() and d.name.lower().startswith(target_low)
            ]
            for mdir in matches:
                cand = _inspect_dir(mdir)
                if cand is not None:
                    chosen = cand
                    break
    else:
        candidates: list[dict[str, Any]] = []
        for d in res_dir.iterdir():
            if not d.is_dir():
                continue
            info = _inspect_dir(d)
            if info is not None:
                candidates.append(info)
        if candidates:
            if require_passing:
                passing = [c for c in candidates if c["passed"] and c["has_full_val"]]
                if not passing:
                    passing = [c for c in candidates if c["passed"]]
                pool = passing or candidates
            else:
                pool = candidates
            pool.sort(key=lambda c: (1 if c["has_full_val"] else 0, c["sort_key"]), reverse=True)
            chosen = pool[0]

    if chosen is None:
        return {
            "title": canonical_title,
            "repo_dir": repo_dir,
            "sha": sha or "",
            "run_dir": None,
            "result_json": {},
            "log_text": "",
            "log_path": None,
        }

    return {
        "title": canonical_title,
        "repo_dir": repo_dir,
        "sha": chosen["sha"],
        "run_dir": chosen["run_dir"],
        "result_json": chosen["result_json"],
        "log_text": chosen["log_text"],
        "log_path": chosen["log_path"],
    }


def _scan_op_kernel_template_instantiations(repo_dir: Path) -> tuple[int, dict[str, int]]:
    """Static fallback scan of `op_kernel/` for tiling-key branches and template instantiations."""
    op_kernel_dir = repo_dir / "op_kernel"
    if not op_kernel_dir.is_dir():
        return 0, {}

    tiling_keys: set[str] = set()
    macro_calls = 0
    explicit_inst = 0
    template_defs = 0

    for fpath in sorted(op_kernel_dir.rglob("*")):
        if not fpath.is_file() or fpath.suffix.lower() not in (".h", ".hpp", ".cpp", ".cc", ".cxx", ".c"):
            continue
        try:
            txt = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Strip comments to avoid false hits
        txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
        txt = re.sub(r"//.*?$", "", txt, flags=re.M)

        for m in re.finditer(r"\b(?:TILING_KEY_IS|REGISTER_TILING_KEY)\s*\(\s*([^)]+)\)", txt):
            tiling_keys.add(re.sub(r"\s+", "", m.group(1)))
            macro_calls += 1

        macro_calls += len(re.findall(r"\b(?:IMPL_OP|EXEC_KERNEL|DISPATCH_KERNEL|INSTANTIATE_KERNEL)\s*\(", txt))
        explicit_inst += len(re.findall(r"(?m)^\s*template\s+(?:class|struct|void|__global__|__aicore__)\b", txt))
        template_defs += len(re.findall(r"\btemplate\s*<", txt))

    counts = {
        "tiling_keys": len(tiling_keys),
        "macro_calls": macro_calls,
        "explicit_instantiations": explicit_inst,
        "template_decls": template_defs,
    }
    if tiling_keys or macro_calls or explicit_inst:
        total = max(len(tiling_keys), macro_calls) + explicit_inst
    else:
        total = template_defs
    return int(total), counts


# ---------------------------------------------------------------------------
# 2.1 elf_guard
# ---------------------------------------------------------------------------

def evaluate_elf_guard(
    workspace: Path,
    title: str,
    sha: str | None = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect bloated `.o`/`.text` (>32KB I-cache tax), slow compile times, and template explosion."""
    ws = Path(workspace).expanduser().resolve()
    art = _load_run_artifacts(ws, title, sha=sha, require_passing=False, allow_build_logs=True)
    canonical_title = art["title"]
    resolved_sha = art["sha"]
    rj = art["result_json"]
    log_text = art["log_text"]
    repo_dir = art["repo_dir"]

    # Merge thresholds: defaults <- config.json -> explicit override
    eff_thresh: dict[str, float | int] = dict(DEFAULT_ELF_THRESHOLDS)
    cfg_ft = _load_fast_tools_settings()
    if isinstance(cfg_ft.get("elf_guard"), dict):
        for k, v in cfg_ft["elf_guard"].items():
            if k in eff_thresh and isinstance(v, (int, float)):
                eff_thresh[k] = v
    if isinstance(thresholds, dict):
        for k, v in thresholds.items():
            if k in eff_thresh and isinstance(v, (int, float)):
                eff_thresh[k] = v

    parsed = _parse_validation_perf_safe(log_text)
    kbs = dict(parsed.get("kernel_binary_stats") or {})

    def _first_num(*vals: Any) -> float | int | None:
        fallback_zero: float | int | None = None
        for v in vals:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if v > 0:
                    return v
                if fallback_zero is None:
                    fallback_zero = v
        return fallback_zero

    o_count = _first_num(rj.get("kernel_o_count"), kbs.get("count"))
    o_max_bytes = _first_num(rj.get("kernel_o_max_bytes"), kbs.get("max_bytes"))
    o_sum_bytes = _first_num(rj.get("kernel_o_sum_bytes"), kbs.get("sum_bytes"))
    o_max_text = _first_num(rj.get("kernel_o_max_text_bytes"), kbs.get("max_text_bytes"))
    build_sec = _first_num(rj.get("phase_a_cpu_sec"), kbs.get("phase_a_cpu_sec"))
    if build_sec is None and log_text:
        m_b = re.search(r"(?:BUILD_ONLY_PASSED|Phase-A CPU build failed in)\s*\(?([\d.]+)s", log_text)
        if m_b:
            try:
                build_sec = float(m_b.group(1))
            except ValueError:
                pass

    tmpl_count = _first_num(
        rj.get("template_instantiations"),
        rj.get("template_instantiation_count"),
        kbs.get("template_instantiations"),
        kbs.get("template_instantiation_count"),
    )
    tmpl_source = "compiled_stats" if tmpl_count is not None else "none"
    static_scan_detail: dict[str, int] = {}
    if tmpl_count is None:
        scanned_n, static_scan_detail = _scan_op_kernel_template_instantiations(repo_dir)
        if scanned_n > 0 or (repo_dir / "op_kernel").is_dir():
            tmpl_count = scanned_n
            tmpl_source = "op_kernel_scan"

    alerts: list[dict[str, Any]] = []

    if o_max_text is not None:
        if o_max_text >= eff_thresh["fail_text_bytes"]:
            alerts.append({
                "code": "ICACHE_TEXT_BLOAT",
                "severity": "redline",
                "metric": "kernel_o_max_text_bytes",
                "value": int(o_max_text),
                "threshold": int(eff_thresh["fail_text_bytes"]),
                "message": (
                    f"Kernel .text size {int(o_max_text)}B ({o_max_text / 1024:.1f} KB) exceeds "
                    f"{int(eff_thresh['fail_text_bytes']) // 1024} KB L1 I-Cache redline -> triggers cold-kernel stall."
                ),
                "suggested_kb_tags": ["noinline", "elf-diet", "icache-32kb"],
            })
        elif o_max_text >= eff_thresh["warn_text_bytes"]:
            alerts.append({
                "code": "ICACHE_TEXT_WARN",
                "severity": "warn",
                "metric": "kernel_o_max_text_bytes",
                "value": int(o_max_text),
                "threshold": int(eff_thresh["warn_text_bytes"]),
                "message": (
                    f"Kernel .text size {int(o_max_text)}B ({o_max_text / 1024:.1f} KB) is >= "
                    f"{int(eff_thresh['warn_text_bytes']) // 1024} KB (75% of 32 KB I-Cache)."
                ),
                "suggested_kb_tags": ["noinline", "elf-diet", "icache-32kb"],
            })

    if o_max_bytes is not None:
        if o_max_bytes >= eff_thresh["fail_o_max_bytes"]:
            alerts.append({
                "code": "ELF_OBJECT_BLOAT",
                "severity": "redline",
                "metric": "kernel_o_max_bytes",
                "value": int(o_max_bytes),
                "threshold": int(eff_thresh["fail_o_max_bytes"]),
                "message": (
                    f"Max kernel .o size {int(o_max_bytes)}B ({o_max_bytes / 1024:.1f} KB) exceeds "
                    f"{int(eff_thresh['fail_o_max_bytes']) // 1024} KB redline."
                ),
                "suggested_kb_tags": ["elf-diet", "tiling-key-prune"],
            })
        elif o_max_bytes >= eff_thresh["warn_o_max_bytes"]:
            alerts.append({
                "code": "ELF_OBJECT_WARN",
                "severity": "warn",
                "metric": "kernel_o_max_bytes",
                "value": int(o_max_bytes),
                "threshold": int(eff_thresh["warn_o_max_bytes"]),
                "message": (
                    f"Max kernel .o size {int(o_max_bytes)}B ({o_max_bytes / 1024:.1f} KB) exceeds "
                    f"{int(eff_thresh['warn_o_max_bytes']) // 1024} KB warning threshold."
                ),
                "suggested_kb_tags": ["elf-diet", "tiling-key-prune"],
            })

    if build_sec is not None:
        if build_sec >= eff_thresh["fail_build_sec"]:
            alerts.append({
                "code": "COMPILE_TIME_TLE_RISK",
                "severity": "redline",
                "metric": "phase_a_cpu_sec",
                "value": round(float(build_sec), 2),
                "threshold": float(eff_thresh["fail_build_sec"]),
                "message": (
                    f"Phase-A CPU build time {float(build_sec):.1f}s exceeds "
                    f"{float(eff_thresh['fail_build_sec']):.0f}s redline (420s judge wall-clock TLE risk)."
                ),
                "suggested_kb_tags": ["template-prune", "compile-speed"],
            })
        elif build_sec >= eff_thresh["warn_build_sec"]:
            alerts.append({
                "code": "COMPILE_TIME_WARN",
                "severity": "warn",
                "metric": "phase_a_cpu_sec",
                "value": round(float(build_sec), 2),
                "threshold": float(eff_thresh["warn_build_sec"]),
                "message": (
                    f"Phase-A CPU build time {float(build_sec):.1f}s exceeds "
                    f"{float(eff_thresh['warn_build_sec']):.0f}s warning threshold."
                ),
                "suggested_kb_tags": ["template-prune", "compile-speed"],
            })

    if tmpl_count is not None:
        if tmpl_count >= eff_thresh["fail_templates"]:
            alerts.append({
                "code": "TEMPLATE_EXPLOSION",
                "severity": "redline",
                "metric": "template_instantiation_count",
                "value": int(tmpl_count),
                "threshold": int(eff_thresh["fail_templates"]),
                "message": (
                    f"Template / tiling-key instantiation count {int(tmpl_count)} exceeds "
                    f"{int(eff_thresh['fail_templates'])} redline (severe compile & ELF bloat risk)."
                ),
                "suggested_kb_tags": ["template-prune", "tiling-key-prune", "compile-speed"],
            })
        elif tmpl_count >= eff_thresh["warn_templates"]:
            alerts.append({
                "code": "TEMPLATE_COUNT_WARN",
                "severity": "warn",
                "metric": "template_instantiation_count",
                "value": int(tmpl_count),
                "threshold": int(eff_thresh["warn_templates"]),
                "message": (
                    f"Template / tiling-key instantiation count {int(tmpl_count)} exceeds "
                    f"{int(eff_thresh['warn_templates'])} warning threshold."
                ),
                "suggested_kb_tags": ["template-prune", "tiling-key-prune"],
            })

    if any(a["severity"] == "redline" for a in alerts):
        status = "redline"
    elif any(a["severity"] == "warn" for a in alerts):
        status = "warn"
    else:
        status = "ok"

    has_telemetry = any(x is not None for x in (o_count, o_max_bytes, o_max_text, build_sec))
    metrics = {
        "kernel_o_count": int(o_count) if o_count is not None else None,
        "kernel_o_max_bytes": int(o_max_bytes) if o_max_bytes is not None else None,
        "kernel_o_sum_bytes": int(o_sum_bytes) if o_sum_bytes is not None else None,
        "kernel_o_max_text_bytes": int(o_max_text) if o_max_text is not None else None,
        "phase_a_cpu_sec": round(float(build_sec), 2) if build_sec is not None else None,
        "template_instantiation_count": int(tmpl_count) if tmpl_count is not None else None,
        "template_source": tmpl_source,
        "static_scan": static_scan_detail,
        "largest_objects": kbs.get("files") if isinstance(kbs.get("files"), dict) else {},
        "has_telemetry": has_telemetry,
    }
    return {
        "title": canonical_title,
        "sha": resolved_sha,
        "status": status,
        "metrics": metrics,
        "thresholds": eff_thresh,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# 2.2 cold3_probe
# ---------------------------------------------------------------------------

def _extract_cold3_samples(log_text: str, rj: dict[str, Any]) -> dict[str, Any]:
    """Parse COLD3_STATS, COLD_START_STATS, COLD3_CASE, and per-case 3-run samples."""
    cold_start_stats: dict[str, Any] = {}
    cold3_stats: dict[str, Any] = {}
    explicit_3runs: dict[str, list[float]] = {}
    first_iters: dict[str, float] = {}
    medians: dict[str, float] = {}
    mins: dict[str, float] = {}
    maxs: dict[str, float] = {}

    def _to_float_list(val: Any) -> list[float]:
        if not isinstance(val, list):
            return []
        out: list[float] = []
        for x in val:
            if isinstance(x, (int, float)) and not isinstance(x, bool) and float(x) > 0:
                out.append(float(x))
        return out

    def _ingest_case_item(cname: str, item: dict[str, Any]) -> None:
        if not cname or ":" in cname or cname.startswith("__"):
            return
        if item.get("mode") == "torch" or item.get("source") == "torch":
            return
        if isinstance(item.get("ops"), dict) and item["ops"]:
            sub = next(iter(item["ops"].values()))
            if isinstance(sub, dict):
                merged = dict(sub)
                merged.setdefault("median_us", item.get("median_us"))
                merged.setdefault("min_us", item.get("min_us"))
                merged.setdefault("max_us", item.get("max_us"))
                merged.setdefault("first_iter_us", item.get("first_iter_us") or item.get("cold_us"))
                merged.setdefault("cold3_runs", item.get("cold3_runs") or item.get("runs_us") or item.get("runs"))
                item = merged

        runs = (
            _to_float_list(item.get("cold3_runs"))
            or _to_float_list(item.get("runs_us"))
            or _to_float_list(item.get("runs"))
            or _to_float_list(item.get("raw_runs"))
        )
        if not runs:
            dur = _to_float_list(item.get("durations_us")) or _to_float_list(item.get("all_us"))
            if len(dur) >= 3 and dur != sorted(dur):
                runs = dur[:3]
        if not runs and all(isinstance(item.get(k), (int, float)) for k in ("run1_us", "run2_us", "run3_us")):
            r1, r2, r3 = float(item["run1_us"]), float(item["run2_us"]), float(item["run3_us"])
            if r1 > 0 and r2 > 0 and r3 > 0:
                runs = [r1, r2, r3]
        if len(runs) >= 3:
            explicit_3runs[cname] = runs[:3]
            first_iters[cname] = runs[0]
            mins[cname] = min(runs[:3])
            maxs[cname] = max(runs[:3])
            medians.setdefault(cname, statistics.median(runs[:3]))

        fi = item.get("first_iter_us") or item.get("cold_us")
        if isinstance(fi, (int, float)) and float(fi) > 0:
            first_iters[cname] = float(fi)
        med = item.get("median_us") or item.get("kernel_median_us")
        if isinstance(med, (int, float)) and float(med) > 0:
            medians[cname] = float(med)
        mn = item.get("min_us") or item.get("kernel_min_us")
        if isinstance(mn, (int, float)) and float(mn) > 0:
            mins[cname] = float(mn)
        mx = item.get("max_us") or item.get("kernel_max_us")
        if isinstance(mx, (int, float)) and float(mx) > 0:
            maxs[cname] = float(mx)

    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("COLD3_STATS "):
            try:
                obj = json.loads(line[len("COLD3_STATS "):].strip())
                if isinstance(obj, dict):
                    cold3_stats = obj
                    per_case = obj.get("per_case") or {}
                    if isinstance(per_case, dict):
                        for cname, val in per_case.items():
                            if isinstance(val, list):
                                fl = _to_float_list(val)
                                if len(fl) >= 3:
                                    explicit_3runs[str(cname)] = fl[:3]
                            elif isinstance(val, dict):
                                _ingest_case_item(str(cname), val)
            except ValueError:
                pass
            continue
        if line.startswith("COLD3_CASE "):
            try:
                obj = json.loads(line[len("COLD3_CASE "):].strip())
                if isinstance(obj, dict):
                    cname = str(obj.get("case") or obj.get("label") or "")
                    _ingest_case_item(cname, obj)
            except ValueError:
                pass
            continue
        if line.startswith("COLD_START_STATS "):
            try:
                obj = json.loads(line[len("COLD_START_STATS "):].strip())
                if isinstance(obj, dict):
                    cold_start_stats = obj
            except ValueError:
                pass
            continue
        for prefix in ("KPERF_JSON ", "KERNEL_JSON ", "KERNEL_PERF ", "PERF "):
            if line.startswith(prefix):
                try:
                    payload = json.loads(line[len(prefix):].strip())
                except ValueError:
                    break
                items = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
                for it in items:
                    if isinstance(it, dict):
                        cname = str(it.get("case") or it.get("label") or "")
                        _ingest_case_item(cname, it)
                break
        else:
            m_ms = re.match(r"^MS_PROF\s+case=(\S+)(.*)$", line)
            m_kp = re.match(r"^KPERF\s+(\S+)(.*)$", line) if not m_ms else None
            if m_ms or m_kp:
                m_row = m_ms or m_kp
                assert m_row is not None
                cname = m_row.group(1)
                rest = m_row.group(2)
                if ":" not in cname and not cname.startswith("__"):
                    m_med = re.search(r"\bmedian=\s*([\d.]+)", rest)
                    m_mn = re.search(r"\bmin=\s*([\d.]+)", rest)
                    m_mx = re.search(r"\bmax=\s*([\d.]+)", rest)
                    m_fi = re.search(r"\bfirst(?:_iter)?=\s*([\d.]+)", rest)
                    if m_med:
                        medians[cname] = float(m_med.group(1))
                    if m_mn:
                        mins[cname] = float(m_mn.group(1))
                    if m_mx:
                        maxs[cname] = float(m_mx.group(1))
                    if m_fi:
                        first_iters[cname] = float(m_fi.group(1))
            elif line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        cname = str(obj.get("case") or obj.get("label") or "")
                        _ingest_case_item(cname, obj)
                except ValueError:
                    pass

    # Supplement from result.json if absent from log
    if isinstance(rj.get("cold3_stats"), dict):
        for k, v in rj["cold3_stats"].items():
            cold3_stats.setdefault(k, v)
    if isinstance(cold3_stats.get("per_case"), dict):
        for cname, val in cold3_stats["per_case"].items():
            if cname not in explicit_3runs:
                if isinstance(val, list):
                    fl = _to_float_list(val)
                    if len(fl) >= 3:
                        explicit_3runs[str(cname)] = fl[:3]
                elif isinstance(val, dict):
                    _ingest_case_item(str(cname), val)

    return {
        "cold_start_stats": cold_start_stats,
        "cold3_stats": cold3_stats,
        "explicit_3runs": explicit_3runs,
        "first_iters": first_iters,
        "medians": medians,
        "mins": mins,
        "maxs": maxs,
    }


def evaluate_cold3_probe(
    workspace: Path,
    title: str,
    sha: str | None = None,
) -> dict[str, Any]:
    """Quantify true un-warmed 3-run cold-start latency and 10-core DVFS bimodal spikes."""
    ws = Path(workspace).expanduser().resolve()
    art = _load_run_artifacts(ws, title, sha=sha, require_passing=False, allow_build_logs=False)
    canonical_title = art["title"]
    resolved_sha = art["sha"]
    rj = art["result_json"]
    log_text = art["log_text"]

    eff_thresh = dict(DEFAULT_COLD3_THRESHOLDS)
    cfg_ft = _load_fast_tools_settings()
    if isinstance(cfg_ft.get("cold3_probe"), dict):
        for k, v in cfg_ft["cold3_probe"].items():
            if k in eff_thresh and isinstance(v, (int, float)):
                eff_thresh[k] = float(v)

    extracted = _extract_cold3_samples(log_text, rj)
    c3s = extracted["cold3_stats"]
    css = extracted["cold_start_stats"]
    explicit_3runs: dict[str, list[float]] = extracted["explicit_3runs"]
    first_iters: dict[str, float] = extracted["first_iters"]
    medians: dict[str, float] = extracted["medians"]
    mins: dict[str, float] = extracted["mins"]
    maxs: dict[str, float] = extracted["maxs"]

    spike_ratio_thr = eff_thresh["spike_ratio"]
    spike_diff_thr = eff_thresh["spike_diff_us"]

    spike_cases: list[dict[str, Any]] = []
    per_case_means: list[float] = []
    per_case_spreads: list[float] = []
    per_case_firsts: list[float] = []

    if explicit_3runs:
        for cname, runs in sorted(explicit_3runs.items()):
            r1, r2, r3 = runs[0], runs[1], runs[2]
            per_case_firsts.append(r1)
            per_case_means.append((r1 + r2 + r3) / 3.0)
            per_case_spreads.append(max(runs) - min(runs))
            if r3 > 0 and (r1 / r3) >= spike_ratio_thr and (r1 - r3) >= spike_diff_thr:
                spike_cases.append({
                    "case": cname,
                    "run1_us": round(r1, 3),
                    "run2_us": round(r2, 3),
                    "run3_us": round(r3, 3),
                    "ratio": round(r1 / r3, 3),
                    "diff_us": round(r1 - r3, 3),
                })

    # Compute summary metrics cleanly across explicit 3-run and fallback sources
    first_iter_us: float | None = None
    if isinstance(c3s.get("first_iter_us"), (int, float)):
        first_iter_us = float(c3s["first_iter_us"])
    elif per_case_firsts:
        first_iter_us = statistics.median(per_case_firsts)
    elif first_iters:
        first_iter_us = statistics.median(first_iters.values())
    elif isinstance(css.get("first_iter_us"), (int, float)):
        first_iter_us = float(css["first_iter_us"])
    elif isinstance(rj.get("first_iter_us"), (int, float)):
        first_iter_us = float(rj["first_iter_us"])
    elif maxs:
        first_iter_us = statistics.median(maxs.values())

    warm_median_us: float | None = None
    if isinstance(c3s.get("warm_median_us"), (int, float)):
        warm_median_us = float(c3s["warm_median_us"])
    elif medians:
        warm_median_us = statistics.median(medians.values())
    elif explicit_3runs:
        warm_median_us = statistics.median(min(r[1], r[2]) for r in explicit_3runs.values())
    elif mins:
        warm_median_us = statistics.median(mins.values())

    cold_3run_mean_us: float | None = None
    if isinstance(c3s.get("cold_3run_mean_us"), (int, float)):
        cold_3run_mean_us = float(c3s["cold_3run_mean_us"])
    elif isinstance(rj.get("cold_3run_mean_us"), (int, float)):
        cold_3run_mean_us = float(rj["cold_3run_mean_us"])
    elif per_case_means:
        cold_3run_mean_us = statistics.median(per_case_means)
    elif first_iter_us is not None and warm_median_us is not None:
        cold_3run_mean_us = (first_iter_us + 2.0 * warm_median_us) / 3.0

    cold_tax_us: float | None = None
    if cold_3run_mean_us is not None and warm_median_us is not None:
        cold_tax_us = round(cold_3run_mean_us - warm_median_us, 3)

    cold_spread_median_us: float | None = None
    if isinstance(c3s.get("cold_spread_median_us"), (int, float)):
        cold_spread_median_us = float(c3s["cold_spread_median_us"])
    elif per_case_spreads:
        cold_spread_median_us = statistics.median(per_case_spreads)
    elif isinstance(css.get("cold_spread_median_us"), (int, float)):
        cold_spread_median_us = float(css["cold_spread_median_us"])
    elif isinstance(rj.get("cold_spread_median_us"), (int, float)):
        cold_spread_median_us = float(rj["cold_spread_median_us"])
    else:
        spreads = [maxs[c] - mins[c] for c in maxs if c in mins and maxs[c] >= mins[c]]
        if spreads:
            cold_spread_median_us = statistics.median(spreads)

    # Check fallback per-case spike when no explicit 3-run samples were logged
    if not explicit_3runs:
        for cname in sorted(set(first_iters) | set(maxs)):
            r1 = first_iters.get(cname) or maxs.get(cname)
            r3 = mins.get(cname) or medians.get(cname)
            if r1 and r3 and r3 > 0 and (r1 / r3) >= spike_ratio_thr and (r1 - r3) >= spike_diff_thr:
                r2 = medians.get(cname, r3)
                spike_cases.append({
                    "case": cname,
                    "run1_us": round(r1, 3),
                    "run2_us": round(r2, 3),
                    "run3_us": round(r3, 3),
                    "ratio": round(r1 / r3, 3),
                    "diff_us": round(r1 - r3, 3),
                })

    bimodal_spike_detected = bool(
        c3s.get("bimodal_spike_detected")
        or spike_cases
        or (
            not explicit_3runs
            and first_iter_us is not None
            and warm_median_us is not None
            and warm_median_us > 0
            and (first_iter_us / warm_median_us) >= spike_ratio_thr
            and (first_iter_us - warm_median_us) >= spike_diff_thr
        )
    )

    alerts: list[dict[str, Any]] = []
    if bimodal_spike_detected:
        top_sc = spike_cases[0] if spike_cases else None
        detail = (
            f"case={top_sc['case']} ({top_sc['run1_us']}us -> {top_sc['run3_us']}us, {top_sc['ratio']}x)"
            if top_sc
            else f"first_iter={first_iter_us:.2f}us vs warm={warm_median_us:.2f}us"
        )
        alerts.append({
            "code": "DVFS_BIMODAL_SPIKE",
            "severity": "redline",
            "message": f"10-core DVFS / cold-start bimodal spike detected: {detail}.",
            "suggested_kb_tags": ["dvfs-10core-spike", "zero-tpipe", "direct-gm-tiling"],
        })

    if cold_tax_us is not None:
        if cold_tax_us >= eff_thresh["redline_cold_tax_us"]:
            alerts.append({
                "code": "COLD_LAUNCH_TAX_HIGH",
                "severity": "redline",
                "message": f"Cold 3-run tax +{cold_tax_us:.3f} us exceeds +{eff_thresh['redline_cold_tax_us']:.1f} us redline.",
                "suggested_kb_tags": ["zero-tpipe", "direct-gm-tiling", "elf-diet"],
            })
        elif cold_tax_us >= eff_thresh["warn_cold_tax_us"]:
            alerts.append({
                "code": "COLD_LAUNCH_TAX_WARN",
                "severity": "warn",
                "message": f"Cold 3-run tax +{cold_tax_us:.3f} us exceeds +{eff_thresh['warn_cold_tax_us']:.1f} us warning threshold.",
                "suggested_kb_tags": ["zero-tpipe", "direct-gm-tiling"],
            })

    rel_spread = (
        (cold_spread_median_us / max(1.0, warm_median_us))
        if (cold_spread_median_us is not None and warm_median_us is not None and warm_median_us > 0)
        else 1.0
    )
    if cold_spread_median_us is not None and not bimodal_spike_detected and rel_spread >= 0.12:
        if cold_spread_median_us >= eff_thresh["redline_spread_us"]:
            alerts.append({
                "code": "COLD_SPREAD_HIGH",
                "severity": "redline",
                "message": f"Cold-to-warm spread {cold_spread_median_us:.3f} us exceeds {eff_thresh['redline_spread_us']:.1f} us redline.",
                "suggested_kb_tags": ["dvfs-10core-spike", "half-core-1-20-40"],
            })
        elif cold_spread_median_us >= eff_thresh["warn_spread_us"]:
            alerts.append({
                "code": "COLD_SPREAD_WARN",
                "severity": "warn",
                "message": f"Cold-to-warm spread {cold_spread_median_us:.3f} us exceeds {eff_thresh['warn_spread_us']:.1f} us warning threshold.",
                "suggested_kb_tags": ["dvfs-10core-spike"],
            })

    if any(a["severity"] == "redline" for a in alerts):
        status = "redline"
    elif any(a["severity"] == "warn" for a in alerts):
        status = "warn"
    else:
        status = "ok"

    has_telemetry = any(
        v is not None for v in (first_iter_us, cold_3run_mean_us, warm_median_us, cold_spread_median_us)
    )
    metrics = {
        "first_iter_us": round(first_iter_us, 3) if first_iter_us is not None else None,
        "cold_3run_mean_us": round(cold_3run_mean_us, 3) if cold_3run_mean_us is not None else None,
        "warm_median_us": round(warm_median_us, 3) if warm_median_us is not None else None,
        "cold_tax_us": cold_tax_us,
        "cold_spread_median_us": round(cold_spread_median_us, 3) if cold_spread_median_us is not None else None,
        "bimodal_spike_detected": bimodal_spike_detected,
        "explicit_cold3_cases": len(explicit_3runs),
        "spike_cases": spike_cases[:8],
        "has_telemetry": has_telemetry,
    }
    return {
        "title": canonical_title,
        "sha": resolved_sha,
        "status": status,
        "metrics": metrics,
        "first_iter_us": metrics["first_iter_us"],
        "cold_3run_mean_us": metrics["cold_3run_mean_us"],
        "warm_median_us": metrics["warm_median_us"],
        "cold_tax_us": metrics["cold_tax_us"],
        "cold_spread_median_us": metrics["cold_spread_median_us"],
        "bimodal_spike_detected": bimodal_spike_detected,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# 2.3 pmu_torch_card
# ---------------------------------------------------------------------------

def _to_ratio(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f / 100.0 if f > 1.5 else f
    if isinstance(val, str):
        s = val.strip()
        is_pct = s.endswith("%")
        s = s.rstrip("%").strip()
        try:
            f = float(s)
        except ValueError:
            return None
        if is_pct or f > 1.5:
            return f / 100.0
        return f
    return None


def _extract_pmu_and_torch(log_text: str) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    """Extract markdown-table timings/torch refs + per-case & aggregate PMU pipe ratios."""
    extra_ours: dict[str, float] = {}
    extra_torch: dict[str, float] = {}
    per_case_pmu: dict[str, dict[str, float]] = {}

    vec_vals: list[float] = []
    mte2_vals: list[float] = []
    mte3_vals: list[float] = []
    scalar_vals: list[float] = []
    bank_vals: list[float] = []

    def _ingest_pmu_dict(d: dict[str, Any], cname: str = "") -> None:
        if not isinstance(d, dict):
            return
        # Check nested `pipe` / `pmu` / `metrics` dicts first
        sub_dicts: list[dict[str, Any]] = [d]
        for sub_key in ("pipe", "pmu", "metrics", "PipeUtilization"):
            if isinstance(d.get(sub_key), dict):
                sub_dicts.append(d[sub_key])

        rec: dict[str, float] = {}
        for sd in sub_dicts:
            for key, dest, bucket_list in (
                (("aiv_vec_ratio", "vec_ratio", "pipe_v", "PIPE_V"), "aiv_vec_ratio", vec_vals),
                (("aiv_mte2_ratio", "mte2_ratio", "MTE2"), "aiv_mte2_ratio", mte2_vals),
                (("aiv_mte3_ratio", "mte3_ratio", "MTE3"), "aiv_mte3_ratio", mte3_vals),
                (("aiv_scalar_ratio", "scalar_ratio", "aiv_scalar_time_ratio", "Scalar"), "aiv_scalar_ratio", scalar_vals),
                (
                    (
                        "aiv_ub_bank_conflict",
                        "bank_conflict_ratio",
                        "bank_conflict",
                        "BankConflict",
                        "ResourceConflictRatio",
                        "resource_conflict_ratio",
                        "vec_bankgroup_conflict_ratio",
                        "vec_bank_conflict_ratio",
                    ),
                    "bank_conflict_ratio",
                    bank_vals,
                ),
            ):
                for alias in key:
                    if alias in sd:
                        rv = _to_ratio(sd[alias])
                        if rv is not None and rv >= 0.0:
                            rec[dest] = rv
                            bucket_list.append(rv)
                            break
        if cname and rec:
            per_case_pmu[cname] = rec

    md_cols: list[str] | None = None
    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Markdown comparison tables like `| case | dtype | ... | med_us | ... | torch_med | ... |`
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                continue
            low_cells = [c.lower() for c in cells]
            if "case" in low_cells and any(k in low_cells for k in ("med_us", "ours_us", "median_us", "torch_med", "torch_us")):
                md_cols = low_cells
                continue
            if md_cols and len(cells) >= len(md_cols):
                row_map = dict(zip(md_cols, cells))
                cname = row_map.get("case", "")
                if cname and ":" not in cname and not cname.startswith("__"):
                    for ok in ("med_us", "ours_us", "median_us", "custom_us"):
                        if ok in row_map:
                            try:
                                v = float(row_map[ok].rstrip("us"))
                                if v > 0:
                                    extra_ours.setdefault(cname, v)
                                    break
                            except ValueError:
                                pass
                    for tk in ("torch_med", "torch_us", "tau_torch_us", "torch_min"):
                        if tk in row_map:
                            try:
                                tv = float(row_map[tk].rstrip("us"))
                                if tv > 0:
                                    extra_torch.setdefault(cname, tv)
                                    break
                            except ValueError:
                                pass
                continue
        else:
            md_cols = None

        # JSON payloads carrying PMU pipe counters or inline torch values
        for prefix in ("PERF ", "KPERF_JSON ", "KERNEL_JSON ", "KERNEL_PERF ", "PROF_SUMMARY ", "PMU_JSON ", "PROFILER_PMU "):
            if line.startswith(prefix):
                try:
                    payload = json.loads(line[len(prefix):].strip())
                except ValueError:
                    break
                items = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if it.get("mode") == "torch" or it.get("source") == "torch":
                        continue
                    cname = str(it.get("case") or it.get("label") or "")
                    if isinstance(it.get("torch_us"), (int, float)) and float(it["torch_us"]) > 0 and cname:
                        extra_torch.setdefault(cname, float(it["torch_us"]))
                    if isinstance(it.get("tau_torch_us"), (int, float)) and float(it["tau_torch_us"]) > 0 and cname:
                        extra_torch.setdefault(cname, float(it["tau_torch_us"]))
                    _ingest_pmu_dict(it, cname)
                    if isinstance(it.get("ops"), dict):
                        for op_val in it["ops"].values():
                            if isinstance(op_val, dict):
                                _ingest_pmu_dict(op_val, cname)
                break
        else:
            # Inline MS_PROF ... torch=.. or key=value PMU lines
            m_case = re.search(r"\bcase=(\S+)", line)
            cname = m_case.group(1) if m_case else ""
            m_t = re.search(r"\b(?:torch_us|tau_torch_us|torch)=([\d.]+)", line)
            if m_t and cname:
                try:
                    tv = float(m_t.group(1))
                    if tv > 0:
                        extra_torch.setdefault(cname, tv)
                except ValueError:
                    pass
            pmu_inline: dict[str, Any] = {}
            for pat, key in (
                (r"\b(?:aiv_vec_ratio|vec_ratio|pipe_v|PIPE_V)=([\d.]+%?)", "aiv_vec_ratio"),
                (r"\b(?:aiv_mte2_ratio|mte2_ratio|MTE2)=([\d.]+%?)", "aiv_mte2_ratio"),
                (r"\b(?:aiv_mte3_ratio|mte3_ratio|MTE3)=([\d.]+%?)", "aiv_mte3_ratio"),
                (r"\b(?:aiv_scalar_ratio|scalar_ratio|Scalar)=([\d.]+%?)", "aiv_scalar_ratio"),
                (
                    r"\b(?:aiv_ub_bank_conflict|bank_conflict_ratio|bank_conflict|ResourceConflictRatio|BankConflict)=([\d.]+%?)",
                    "bank_conflict_ratio",
                ),
            ):
                m_k = re.search(pat, line)
                if m_k:
                    pmu_inline[key] = m_k.group(1)
            if pmu_inline:
                _ingest_pmu_dict(pmu_inline, cname)

    def _mean_or_none(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    pmu_summary = {
        "aiv_vec_ratio": _mean_or_none(vec_vals),
        "aiv_mte2_ratio": _mean_or_none(mte2_vals),
        "aiv_mte3_ratio": _mean_or_none(mte3_vals),
        "aiv_scalar_ratio": _mean_or_none(scalar_vals),
        "bank_conflict_ratio": _mean_or_none(bank_vals),
        "aiv_ub_bank_conflict": _mean_or_none(bank_vals),
        "samples": max(len(vec_vals), len(mte2_vals), len(mte3_vals), len(scalar_vals), len(bank_vals)),
        "per_case": per_case_pmu,
    }
    return extra_ours, extra_torch, pmu_summary


def _classify_bottleneck(pmu: dict[str, Any], cold3: dict[str, Any], bucket_rows: list[dict[str, Any]]) -> str:
    bank = pmu.get("bank_conflict_ratio")
    if isinstance(bank, (int, float)) and bank >= 0.15:
        return "bank-conflict"

    vec = pmu.get("aiv_vec_ratio")
    mte2 = pmu.get("aiv_mte2_ratio")
    mte3 = pmu.get("aiv_mte3_ratio")
    scalar = pmu.get("aiv_scalar_ratio")
    pipes = {
        "mte2-bound": float(mte2) if isinstance(mte2, (int, float)) else None,
        "mte3-bound": float(mte3) if isinstance(mte3, (int, float)) else None,
        "vec-bound": float(vec) if isinstance(vec, (int, float)) else None,
        "scalar-bound": float(scalar) if isinstance(scalar, (int, float)) else None,
    }
    active = {k: v for k, v in pipes.items() if v is not None}

    cold_tax = cold3.get("cold_tax_us")
    cold_spike = bool(cold3.get("bimodal_spike_detected"))
    has_cold_issue = cold_spike or (isinstance(cold_tax, (int, float)) and cold_tax >= 1.5)

    if not active:
        if has_cold_issue:
            return "cold-launch-tax"
        if bucket_rows and all((b.get("ours_us") or 0) <= 6.0 for b in bucket_rows) and any((b.get("ratio_vs_torch") or 0) > 1.15 for b in bucket_rows):
            return "cold-launch-tax"
        return "unknown"

    top_name, top_val = max(active.items(), key=lambda kv: kv[1])
    if has_cold_issue and top_val < 0.25:
        return "cold-launch-tax"

    # Scalar bottlenecks dominate KernelC even at ~0.35 when vector/MTE pipes sit low
    if pipes["scalar-bound"] is not None and pipes["scalar-bound"] >= 0.35:
        other_max = max((v for k, v in active.items() if k != "scalar-bound"), default=0.0)
        if pipes["scalar-bound"] >= other_max or (pipes["scalar-bound"] >= 0.45 and (pipes["vec-bound"] or 0.0) < 0.25):
            return "scalar-bound"

    if top_val >= 0.35:
        return top_name

    # Moderate clear winner (>= 1.4x second highest and >= 0.22)
    sorted_vals = sorted(active.values(), reverse=True)
    second_val = sorted_vals[1] if len(sorted_vals) > 1 else 0.0
    if top_val >= 0.22 and top_val >= max(0.22, second_val * 1.4):
        return top_name

    return "balanced"


def _load_perf_spec_buckets(workspace: Path, title: str, repo_dir: Path) -> dict[str, dict[str, Any]]:
    """Read bucket metadata (`stratum`, `tau_torch_us`/`torch_ref_us`, `prior_w`) from spec JSON."""
    ws = workspace.expanduser().resolve()
    candidates = [
        _bench_specs_dir(ws) / title / "cases_perf.json",
        repo_dir / "bench" / "cases_perf.json",
    ]
    out: dict[str, dict[str, Any]] = {}
    for path in candidates:
        doc = read_json(path, None)
        if not isinstance(doc, dict):
            continue
        for key in ("public_anchors", "hidden_buckets", "buckets"):
            arr = doc.get(key)
            if not isinstance(arr, list):
                continue
            for item in arr:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                bid = str(item["id"])
                if bid not in out:
                    out[bid] = dict(item)
    return out


def build_pmu_torch_card(
    workspace: Path,
    title: str,
    sha: str | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """Produce a concise machine + markdown card combining `ours/torch` ratios with PMU counters."""
    ws = Path(workspace).expanduser().resolve()
    art = _load_run_artifacts(ws, title, sha=sha, require_passing=True, allow_build_logs=False)
    canonical_title = art["title"]
    resolved_sha = art["sha"]
    repo_dir = art["repo_dir"]
    log_text = art["log_text"]

    parsed = _parse_validation_perf_safe(log_text)
    extra_ours, extra_torch, pmu_summary = _extract_pmu_and_torch(log_text)

    ours_map: dict[str, float] = {}
    for k, v in (parsed.get("profiler_cases") or {}).items():
        if isinstance(v, (int, float)) and v > 0:
            ours_map[str(k)] = float(v)
    for k, v in extra_ours.items():
        ours_map.setdefault(k, v)

    torch_map: dict[str, float] = {}
    for k, v in (parsed.get("torch_cases") or {}).items():
        if isinstance(v, (int, float)) and v > 0:
            torch_map[str(k)] = float(v)
    for k, v in extra_torch.items():
        torch_map.setdefault(k, v)

    spec_buckets = _load_perf_spec_buckets(ws, canonical_title, repo_dir)
    cal_doc = read_json(ws / "results" / canonical_title / "calibration.json", {})
    cal_buckets: dict[str, dict[str, Any]] = {}
    if isinstance(cal_doc, dict):
        for cb in cal_doc.get("buckets") or []:
            if isinstance(cb, dict) and cb.get("id"):
                cal_buckets[str(cb["id"])] = cb

    bucket_rows: list[dict[str, Any]] = []
    for bid, ours_us in sorted(ours_map.items()):
        sb = spec_buckets.get(bid, {})
        cb = cal_buckets.get(bid, {})
        torch_us = (
            torch_map.get(bid)
            or sb.get("tau_torch_us")
            or sb.get("torch_ref_us")
            or sb.get("torch_us")
            or sb.get("tbest_us")
        )
        torch_f = float(torch_us) if isinstance(torch_us, (int, float)) and float(torch_us) > 0 else None
        ratio = round(ours_us / torch_f, 4) if torch_f else None

        w_hat_raw = cb.get("learned_w")
        w_hat = float(w_hat_raw) if isinstance(w_hat_raw, (int, float)) and float(w_hat_raw) > 0 else None
        if ratio is not None:
            if w_hat is not None:
                headroom_score = round(w_hat * max(0.0, ratio - 0.8), 6)
            else:
                headroom_score = round(ratio, 6)
        else:
            headroom_score = round(w_hat, 6) if w_hat is not None else 0.0

        stratum = str(cb.get("stratum") or sb.get("stratum") or "M_MID_UB")
        bucket_rows.append({
            "bucket": bid,
            "stratum": stratum,
            "ours_us": round(ours_us, 3),
            "torch_us": round(torch_f, 3) if torch_f is not None else None,
            "ratio_vs_torch": ratio,
            "w_hat": round(w_hat, 5) if w_hat is not None else None,
            "headroom_score": headroom_score,
            "pmu": pmu_summary["per_case"].get(bid, {}),
        })

    bucket_rows.sort(
        key=lambda r: (
            float(r["headroom_score"]),
            float(r["ratio_vs_torch"] or 0.0),
            float(r["ours_us"]),
        ),
        reverse=True,
    )
    top_buckets = bucket_rows[: max(1, int(top_n))]

    elf_verdict = evaluate_elf_guard(ws, canonical_title, sha=resolved_sha or sha)
    cold3_verdict = evaluate_cold3_probe(ws, canonical_title, sha=resolved_sha or sha)
    bottleneck = _classify_bottleneck(pmu_summary, cold3_verdict, bucket_rows)

    suggested_tags: list[str] = [bottleneck] if bottleneck not in ("unknown", "balanced") else []
    for a in [*elf_verdict.get("alerts", []), *cold3_verdict.get("alerts", [])]:
        for t in a.get("suggested_kb_tags") or []:
            if t not in suggested_tags:
                suggested_tags.append(t)

    # Build concise <= 25-line markdown_card optimized for Harness prompt injection
    sha7 = resolved_sha[:7] if resolved_sha else "none"
    em = elf_verdict["metrics"]
    text_kb = f"{em['kernel_o_max_text_bytes'] / 1024:.1f}KB" if em.get("kernel_o_max_text_bytes") is not None else "—"
    obj_kb = f"{em['kernel_o_max_bytes'] / 1024:.1f}KB" if em.get("kernel_o_max_bytes") is not None else "—"
    tmpl_s = str(em["template_instantiation_count"]) if em.get("template_instantiation_count") is not None else "—"
    c3m = cold3_verdict["metrics"]
    c1 = f"{c3m['first_iter_us']:.2f}us" if c3m.get("first_iter_us") is not None else "—"
    c3 = f"{c3m['cold_3run_mean_us']:.2f}us" if c3m.get("cold_3run_mean_us") is not None else "—"
    cw = f"{c3m['warm_median_us']:.2f}us" if c3m.get("warm_median_us") is not None else "—"

    def _fmt_ratio(v: float | None) -> str:
        return f"{v:.3f}" if isinstance(v, (int, float)) else "—"

    lines: list[str] = [
        f"### PMU & Torch Card: {canonical_title} ({sha7})",
        f"- **Bottleneck**: `{bottleneck}` | **ELF**: `{elf_verdict['status']}` (.text={text_kb}, .o={obj_kb}, tmpl={tmpl_s}) | **Cold3**: `{cold3_verdict['status']}` (1st={c1}, 3run={c3}, warm={cw})",
        f"- **PMU**: vec={_fmt_ratio(pmu_summary['aiv_vec_ratio'])} | mte2={_fmt_ratio(pmu_summary['aiv_mte2_ratio'])} | mte3={_fmt_ratio(pmu_summary['aiv_mte3_ratio'])} | scalar={_fmt_ratio(pmu_summary['aiv_scalar_ratio'])} | bank={_fmt_ratio(pmu_summary['bank_conflict_ratio'])}",
    ]

    if top_buckets:
        lines.append("| Bucket | Stratum | Ours | Torch | Ours/Torch | w_hat | Headroom |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        max_rows = min(len(top_buckets), 8)
        for row in top_buckets[:max_rows]:
            t_s = f"{row['torch_us']:.2f}us" if row["torch_us"] is not None else "—"
            r_s = f"{row['ratio_vs_torch']:.2f}x" if row["ratio_vs_torch"] is not None else "—"
            w_s = f"{row['w_hat']:.3f}" if row["w_hat"] is not None else "—"
            lines.append(
                f"| `{row['bucket']}` | {row['stratum']} | {row['ours_us']:.2f}us | {t_s} | {r_s} | {w_s} | {row['headroom_score']:.4f} |"
            )
    else:
        lines.append("- No per-bucket profiler timings found yet in validation logs.")

    all_alerts = [*elf_verdict.get("alerts", []), *cold3_verdict.get("alerts", [])]
    for al in all_alerts[:3]:
        if len(lines) >= 24:
            break
        lines.append(f"- **[{al['severity'].upper()}] {al['code']}**: {al['message']}")
    if suggested_tags and len(lines) < 25:
        lines.append(f"- **KB Tags**: {', '.join(suggested_tags[:6])}")

    markdown_card = "\n".join(lines[:25])

    return {
        "title": canonical_title,
        "sha": resolved_sha,
        "runtime_soc": parsed.get("runtime_soc"),
        "dominant_bottleneck": bottleneck,
        "pmu": pmu_summary,
        "prof_summary": parsed.get("prof_summary") or {},
        "buckets": bucket_rows,
        "top_buckets": top_buckets,
        "elf_guard": {
            "status": elf_verdict["status"],
            "metrics": elf_verdict["metrics"],
            "alerts": elf_verdict["alerts"],
        },
        "cold3": {
            "status": cold3_verdict["status"],
            "metrics": cold3_verdict["metrics"],
            "alerts": cold3_verdict["alerts"],
        },
        "suggested_kb_tags": suggested_tags,
        "markdown_card": markdown_card,
    }


# ---------------------------------------------------------------------------
# 2.4 bench_scaffold
# ---------------------------------------------------------------------------

def _normalize_dtypes(dtypes: list[str] | None) -> list[str]:
    if not dtypes:
        return ["float32", "float16", "bfloat16"]
    out: list[str] = []
    for dt in dtypes:
        norm = _DTYPE_ALIASES.get(str(dt).strip().lower(), str(dt).strip().lower())
        if norm and norm not in out:
            out.append(norm)
    return out or ["float32", "float16", "bfloat16"]


def _dtype_bytes(dtype: str) -> int:
    if dtype in ("float16", "bfloat16"):
        return 2
    if dtype in ("int8", "uint8"):
        return 1
    if dtype == "int64":
        return 8
    return 4


def _build_family_perf_buckets(operator_family: str, dtypes: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate stratified buckets across all 5 strata + unaligned tail widths (17, 31, 63, 127)."""
    dt0 = dtypes[0]
    dt1 = dtypes[1 % len(dtypes)]
    dt2 = dtypes[2 % len(dtypes)]

    family_shapes: dict[str, dict[str, list[tuple[str, list[int], str, str]]]] = {
        "elementwise": {
            "PUBLIC": [
                ("pub_elem_small", [16, 256], dt0, "Public elementwise small tensor"),
                ("pub_elem_mid", [256, 1024], dt1, "Public elementwise mid UB tile"),
            ],
            "S_SMALL_CORE": [
                ("s_elem_tail17", [8, 17], dt0, "Small-core unaligned width=17"),
                ("s_elem_tail31", [16, 31], dt1, "Small-core unaligned width=31"),
            ],
            "M_MID_UB": [
                ("m_elem_tail63", [256, 63], dt2, "Mid-UB unaligned width=63"),
                ("m_elem_vec4k", [128, 4096], dt1, "Mid-UB aligned vectorized tile"),
            ],
            "L_HBM_TILED": [
                ("l_elem_hbm_16k", [256, 16384], dt0, "Large HBM bandwidth-bound 16M-byte sweep"),
                ("l_elem_hbm_bf16", [512, 8192], dt2, "Large HBM bfloat16/mixed-dtype sweep"),
            ],
            "X_EXTREME": [
                ("x_elem_tail127", [17, 127], dt2, "Extreme unaligned prime shape (17, 127)"),
                ("x_elem_odd4097", [3, 4097], dt0, "Extreme odd prime width=4097"),
            ],
        },
        "reduction": {
            "PUBLIC": [
                ("pub_red_256x512", [256, 512], dt0, "Public 2D inner reduction"),
                ("pub_red_64x1024", [64, 1024], dt1, "Public 2D reduction half precision"),
            ],
            "S_SMALL_CORE": [
                ("s_red_4x17", [4, 17], dt0, "Small-core reduction width=17"),
                ("s_red_31x31", [31, 31], dt2, "Small-core square prime (31, 31)"),
            ],
            "M_MID_UB": [
                ("m_red_64x63", [64, 63], dt1, "Mid-UB unaligned inner width=63"),
                ("m_red_512x512", [512, 512], dt2, "Mid-UB multi-core row reduction"),
            ],
            "L_HBM_TILED": [
                ("l_red_256x16384", [256, 16384], dt0, "Large HBM two-pass long-row reduction"),
                ("l_red_4096x1024", [4096, 1024], dt1, "Large HBM many-row reduction"),
            ],
            "X_EXTREME": [
                ("x_red_3x127", [3, 127], dt2, "Extreme 3-row width=127 cross-core check"),
                ("x_red_outer_4097x31", [4097, 31], dt0, "Extreme tall outer reduction (4097, 31)"),
            ],
        },
        "transpose": {
            "PUBLIC": [
                ("pub_tr_256x256", [256, 256], dt0, "Public 2D square transpose"),
                ("pub_tr_64x128", [64, 128], dt1, "Public 2D fp16/mixed transpose"),
            ],
            "S_SMALL_CORE": [
                ("s_tr_17x31", [17, 31], dt0, "Small-core odd transpose (17, 31)"),
                ("s_tr_31x63", [31, 63], dt1, "Small-core unaligned transpose (31, 63)"),
            ],
            "M_MID_UB": [
                ("m_tr_128x127", [128, 127], dt2, "Mid-UB unaligned tail width=127"),
                ("m_tr_16x64x64", [16, 64, 64], dt1, "Mid-UB 3D middle-axis permutation"),
            ],
            "L_HBM_TILED": [
                ("l_tr_1024x1024", [1024, 1024], dt0, "Large HBM bank-conflict-prone 1024x1024"),
                ("l_tr_32x256x256", [32, 256, 256], dt2, "Large HBM 3D high-bandwidth transpose"),
            ],
            "X_EXTREME": [
                ("x_tr_8x17x31x63", [8, 17, 31, 63], dt2, "Extreme 4D all-prime-tail transpose"),
                ("x_tr_3x4097", [3, 4097], dt0, "Extreme skinny stride-4097 transpose"),
            ],
        },
        "gather_scatter": {
            "PUBLIC": [
                ("pub_gs_1024x128", [1024, 128], dt0, "Public gather/scatter aligned table"),
                ("pub_gs_512x256", [512, 256], dt1, "Public gather/scatter half precision"),
            ],
            "S_SMALL_CORE": [
                ("s_gs_32x17", [32, 17], dt0, "Small-core unaligned slice width=17"),
                ("s_gs_64x31", [64, 31], dt2, "Small-core unaligned slice width=31"),
            ],
            "M_MID_UB": [
                ("m_gs_1024x63", [1024, 63], dt1, "Mid-UB unaligned slice width=63"),
                ("m_gs_2048x256", [2048, 256], dt2, "Mid-UB dense-index update"),
            ],
            "L_HBM_TILED": [
                ("l_gs_16384x512", [16384, 512], dt0, "Large HBM random-access table"),
                ("l_gs_8192x1024", [8192, 1024], dt1, "Large HBM wide embedding gather/scatter"),
            ],
            "X_EXTREME": [
                ("x_gs_4097x127", [4097, 127], dt2, "Extreme prime rows=4097 tail=127"),
                ("x_gs_dup_hot_31", [4097, 31], dt0, "Extreme high-contention duplicate-index update"),
            ],
        },
        "pad_slice": {
            "PUBLIC": [
                ("pub_ps_4x16x16", [4, 16, 16], dt0, "Public 3D pad/slice baseline"),
                ("pub_ps_8x64x64", [8, 64, 64], dt1, "Public 3D half-precision pad/slice"),
            ],
            "S_SMALL_CORE": [
                ("s_ps_1x3x17", [1, 3, 17], dt0, "Small-core narrow inner width=17"),
                ("s_ps_2x17x31", [2, 17, 31], dt2, "Small-core prime dims (2, 17, 31)"),
            ],
            "M_MID_UB": [
                ("m_ps_1x3x224x63", [1, 3, 224, 63], dt2, "Mid-UB 4D vision tail width=63"),
                ("m_ps_16x64x128", [16, 64, 128], dt1, "Mid-UB 3D asymmetric pad/slice"),
            ],
            "L_HBM_TILED": [
                ("l_ps_32x128x512", [32, 128, 512], dt0, "Large HBM 3D bulk copy"),
                ("l_ps_16x256x1024", [16, 256, 1024], dt1, "Large HBM 3D wide-row pad/slice"),
            ],
            "X_EXTREME": [
                ("x_ps_2x5x17x31x127", [2, 5, 17, 31, 127], dt2, "Extreme 5D circular/reflect width=127"),
                ("x_ps_neg_crop_31", [8, 64, 31], dt0, "Extreme negative-pad crop width=31"),
            ],
        },
        "sequence_dp": {
            "PUBLIC": [
                ("pub_dp_t50_c32", [50, 16, 32], dt0, "Public sequence DP T=50 C=32"),
                ("pub_dp_t100_c64", [100, 16, 64], dt1, "Public sequence DP T=100 C=64"),
            ],
            "S_SMALL_CORE": [
                ("s_dp_t17_c17", [17, 4, 17], dt0, "Small-core prime sequence T=17 C=17"),
                ("s_dp_t31_c31", [31, 8, 31], dt2, "Small-core prime sequence T=31 C=31"),
            ],
            "M_MID_UB": [
                ("m_dp_t63_c127", [63, 16, 127], dt2, "Mid-UB unaligned T=63 C=127"),
                ("m_dp_t128_c512", [128, 16, 512], dt1, "Mid-UB medium vocab sequence"),
            ],
            "L_HBM_TILED": [
                ("l_dp_t256_c4096", [256, 16, 4096], dt0, "Large HBM large vocab=4096"),
                ("l_dp_t512_c1024", [512, 8, 1024], dt1, "Large HBM long horizon T=512"),
            ],
            "X_EXTREME": [
                ("x_dp_b1_t300_c63", [300, 1, 63], dt0, "Extreme B=1 single-core dependency chain"),
                ("x_dp_ti_eq_si_127", [127, 8, 127], dt2, "Extreme zero-blank slack T==S==127"),
            ],
        },
    }

    spec = family_shapes[operator_family]
    # Hidden priors matching `scheduler.calibration.HIDDEN_STRATA_PRIORS`
    hidden_weights = {
        "S_SMALL_CORE": 0.30,
        "M_MID_UB": 0.35,
        "L_HBM_TILED": 0.20,
        "X_EXTREME": 0.15,
    }

    public_anchors: list[dict[str, Any]] = []
    for bid, shape, dtype, desc in spec["PUBLIC"]:
        numel = 1
        for d in shape:
            numel *= d
        io_bytes = numel * _dtype_bytes(dtype) * 2
        roof = round(max(3.4, 3.35 + io_bytes / (724e9 * 1e-6)), 3)
        torch_est = round(max(roof * 1.35, 4.5), 3)
        public_anchors.append({
            "id": bid,
            "stratum": "PUBLIC",
            "dtype": dtype,
            "shape": shape,
            "io_bytes": io_bytes,
            "roofline_us": roof,
            "tau_torch_us": torch_est,
            "torch_ref_us": torch_est,
            "weight_pts": round(10.0 / len(spec["PUBLIC"]), 2),
            "description": desc,
            "source": "bench_scaffold",
        })

    hidden_buckets: list[dict[str, Any]] = []
    for stratum in ("S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME"):
        entries = spec[stratum]
        per_w = round(hidden_weights[stratum] / len(entries), 6)
        max_w = 0.04 if stratum == "X_EXTREME" else 0.22
        for bid, shape, dtype, desc in entries:
            numel = 1
            for d in shape:
                numel *= d
            io_bytes = numel * _dtype_bytes(dtype) * 2
            roof = round(max(3.4, 3.35 + io_bytes / (724e9 * 1e-6)), 3)
            torch_est = round(max(roof * 1.4, 4.8), 3)
            hidden_buckets.append({
                "id": bid,
                "stratum": stratum,
                "dtype": dtype,
                "shape": shape,
                "io_bytes": io_bytes,
                "roofline_us": roof,
                "tau_torch_us": torch_est,
                "torch_ref_us": torch_est,
                "prior_w": per_w,
                "max_w": max_w,
                "description": desc,
                "source": "bench_scaffold",
            })

    return public_anchors, hidden_buckets


def _build_family_correctness_cases(operator_family: str, dtypes: list[str]) -> list[dict[str, Any]]:
    """Generate adversarial correctness cases (`empty/tiny`, `NaN/Inf/subnormal`, `odd-prime`, `max-broadcast`)."""
    edge_map = {
        "elementwise": ["scalar_broadcast", "1d_singleton_broadcast"],
        "reduction": ["reduce_axis_last_and_0", "keepdim_toggle"],
        "transpose": ["non_contiguous_perm", "inner_dim_swap"],
        "gather_scatter": ["duplicate_indices", "unsorted_boundary_indices"],
        "pad_slice": ["negative_pad_crop", "asymmetric_reflect_circular"],
        "sequence_dp": ["ti_equals_si", "zero_length_or_b1_single_core"],
    }
    edge_attrs = edge_map[operator_family]
    return [
        {
            "id": f"scaffold_{operator_family}_empty_tiny",
            "geometry": "G0_DEGEN",
            "distribution": "D0_STD",
            "shapes": [[0], [1], [1, 1]],
            "dtypes": dtypes,
            "edge_attrs": ["empty_zero_elem", "scalar_tiny", *edge_attrs[:1]],
            "seeds": [11, 29],
            "verified": False,
            "source": "bench_scaffold",
        },
        {
            "id": f"scaffold_{operator_family}_nan_inf_subnormal",
            "geometry": "G4_DTYPE_CROSS",
            "distribution": "D4_ULP_INF",
            "shapes": [[4, 17], [16, 63]],
            "dtypes": dtypes,
            "edge_attrs": ["NaN", "Inf", "-Inf", "subnormal", "0.0", "-0.0"],
            "seeds": [13, 37],
            "verified": False,
            "source": "bench_scaffold",
        },
        {
            "id": f"scaffold_{operator_family}_overflow_cancel",
            "geometry": "G2_CORE_TILE",
            "distribution": "D1_OVERFLOW",
            "shapes": [[40, 127], [41, 127]],
            "dtypes": dtypes,
            "edge_attrs": ["core_boundary_40_41", "fp16_65504_overflow", "catastrophic_cancel_1000_plus_eps"],
            "seeds": [17, 43],
            "verified": False,
            "source": "bench_scaffold",
        },
        {
            "id": f"scaffold_{operator_family}_noncontig_odd_prime",
            "geometry": "G1_ODD_ALIGN",
            "distribution": "D2_SUBNORM",
            "shapes": [[3, 17], [5, 31], [7, 63], [11, 127]],
            "dtypes": dtypes,
            "edge_attrs": ["non_contiguous_strided", "unaligned_prime_tails_17_31_63_127"],
            "seeds": [19, 53],
            "verified": False,
            "source": "bench_scaffold",
        },
        {
            "id": f"scaffold_{operator_family}_max_broadcast",
            "geometry": "G3_HIGH_RANK",
            "distribution": "D3_CANCEL",
            "shapes": [[2, 1, 17, 1, 31], [1, 5, 1, 63, 1]],
            "dtypes": dtypes,
            "edge_attrs": ["max_rank_5d_broadcast", *edge_attrs],
            "seeds": [23, 61],
            "verified": False,
            "source": "bench_scaffold",
        },
    ]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def scaffold_bench_specs(
    workspace: Path,
    title: str,
    operator_family: str = "elementwise",
    dtypes: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Generate/append canonical stratified `cases_perf.json` + `cases_correctness.json` (strictly append-only)."""
    fam = (operator_family or "elementwise").strip().lower()
    if fam not in VALID_OPERATOR_FAMILIES:
        raise ValueError(
            f"Unsupported operator_family '{operator_family}'. Must be one of: {', '.join(VALID_OPERATOR_FAMILIES)}"
        )

    norm_dtypes = _normalize_dtypes(dtypes)
    ws = Path(workspace).expanduser().resolve()
    canonical_title, _ = _resolve_problem_info(ws, title)
    target_dir = _bench_specs_dir(ws) / canonical_title
    perf_path = target_dir / "cases_perf.json"
    corr_path = target_dir / "cases_correctness.json"

    cand_public, cand_hidden = _build_family_perf_buckets(fam, norm_dtypes)
    cand_corr = _build_family_correctness_cases(fam, norm_dtypes)

    # Inspect existing perf spec without mutating any existing items
    existing_perf_doc = read_json(perf_path, None) if perf_path.is_file() else None
    existing_public_ids: set[str] = set()
    existing_hidden_ids: set[str] = set()
    if isinstance(existing_perf_doc, dict):
        for item in existing_perf_doc.get("public_anchors") or []:
            if isinstance(item, dict) and item.get("id"):
                existing_public_ids.add(str(item["id"]))
        for item in existing_perf_doc.get("hidden_buckets") or []:
            if isinstance(item, dict) and item.get("id"):
                existing_hidden_ids.add(str(item["id"]))
        for item in existing_perf_doc.get("buckets") or []:
            if isinstance(item, dict) and item.get("id"):
                existing_hidden_ids.add(str(item["id"]))
    all_existing_perf_ids = existing_public_ids | existing_hidden_ids

    missing_public = [b for b in cand_public if b["id"] not in all_existing_perf_ids]
    missing_hidden = [b for b in cand_hidden if b["id"] not in all_existing_perf_ids]
    missing_perf_ids = [b["id"] for b in (*missing_public, *missing_hidden)]

    # Inspect existing correctness spec without mutating any existing items
    existing_corr_doc = read_json(corr_path, None) if corr_path.is_file() else None
    existing_corr_ids: set[str] = set()
    if isinstance(existing_corr_doc, dict):
        for item in existing_corr_doc.get("cases") or []:
            if isinstance(item, dict) and item.get("id"):
                existing_corr_ids.add(str(item["id"]))
    missing_corr = [c for c in cand_corr if c["id"] not in existing_corr_ids]
    missing_corr_ids = [c["id"] for c in missing_corr]

    perf_existed = isinstance(existing_perf_doc, dict)
    corr_existed = isinstance(existing_corr_doc, dict)

    if apply:
        if perf_existed:
            # Strictly append-only: preserve every existing bucket object verbatim
            doc_p = dict(existing_perf_doc)
            if missing_public:
                pub_list = list(doc_p.get("public_anchors") or [])
                pub_list.extend(missing_public)
                doc_p["public_anchors"] = pub_list
            if missing_hidden:
                if "hidden_buckets" in doc_p or "buckets" not in doc_p:
                    hid_list = list(doc_p.get("hidden_buckets") or [])
                    hid_list.extend(missing_hidden)
                    doc_p["hidden_buckets"] = hid_list
                else:
                    b_list = list(doc_p.get("buckets") or [])
                    b_list.extend(missing_hidden)
                    doc_p["buckets"] = b_list
            if missing_public or missing_hidden:
                doc_p["scaffold_appended_at"] = utc_now().isoformat()
                _atomic_write_json(perf_path, doc_p)
        else:
            new_perf_doc = {
                "problem": canonical_title,
                "version": "2.0",
                "append_only": True,
                "operator_family": fam,
                "protocol": {
                    "profiler": "profiler_single_session",
                    "warmup": 2,
                    "repeat": 5,
                    "sync_per_iter": True,
                    "metric": "task_duration_median_us",
                },
                "hardware_roofline": {
                    "soc": "soc-a",
                    "hbm_bw_gbps": 724.0,
                    "aiv_cores": 40,
                    "launch_floor_us": 3.35,
                },
                "strata_priors": {
                    "PUBLIC": 0.10,
                    "S_SMALL_CORE": 0.27,
                    "M_MID_UB": 0.315,
                    "L_HBM_TILED": 0.18,
                    "X_EXTREME": 0.135,
                },
                "public_anchors": cand_public,
                "hidden_buckets": cand_hidden,
                "scaffolded_at": utc_now().isoformat(),
            }
            _atomic_write_json(perf_path, new_perf_doc)

        if corr_existed:
            if missing_corr:
                doc_c = dict(existing_corr_doc)
                cases_list = list(doc_c.get("cases") or [])
                cases_list.extend(missing_corr)
                doc_c["cases"] = cases_list
                doc_c["scaffold_appended_at"] = utc_now().isoformat()
                _atomic_write_json(corr_path, doc_c)
        else:
            new_corr_doc = {
                "problem": canonical_title,
                "version": "1.0",
                "operator_family": fam,
                "sentinel_guard": {
                    "prefill_pattern": "0x5A_NaN_canary",
                    "head_guard_bytes": 32,
                    "tail_guard_bytes": 32,
                    "require_fp64_oracle": True,
                },
                "geometry_layers": ["G0_DEGEN", "G1_ODD_ALIGN", "G2_CORE_TILE", "G3_HIGH_RANK", "G4_DTYPE_CROSS", "G5_EDGE_ATTR"],
                "distribution_layers": ["D0_STD", "D1_OVERFLOW", "D2_SUBNORM", "D3_CANCEL", "D4_ULP_INF", "D5_ESCAPE_REG"],
                "cases": cand_corr,
                "scaffolded_at": utc_now().isoformat(),
            }
            _atomic_write_json(corr_path, new_corr_doc)

    return {
        "title": canonical_title,
        "operator_family": fam,
        "dtypes": norm_dtypes,
        "applied": bool(apply),
        "target_dir": str(target_dir),
        "cases_perf_path": str(perf_path),
        "cases_correctness_path": str(corr_path),
        "perf_existed": perf_existed,
        "correctness_existed": corr_existed,
        "existing_perf_count": len(all_existing_perf_ids),
        "existing_correctness_count": len(existing_corr_ids),
        "appended_perf_ids": missing_perf_ids if apply else [],
        "pending_perf_ids": [] if apply else missing_perf_ids,
        "appended_correctness_ids": missing_corr_ids if apply else [],
        "pending_correctness_ids": [] if apply else missing_corr_ids,
        "strata_covered": ["PUBLIC", "S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME"],
    }
