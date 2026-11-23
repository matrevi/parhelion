"""ELF & compile-time redline guard (`evaluate_elf_guard`)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import (
    DEFAULT_ELF_THRESHOLDS,
    _first_num,
    _load_fast_tools_settings,
    _load_run_artifacts,
    _parse_validation_perf_safe,
)


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
    total = (max(len(tiling_keys), macro_calls) + explicit_inst) if (tiling_keys or macro_calls or explicit_inst) else template_defs
    return int(total), counts


def _append_elf_alerts(
    alerts: list[dict[str, Any]],
    eff_thresh: dict[str, float | int],
    *,
    o_max_text: float | int | None,
    o_max_bytes: float | int | None,
    build_sec: float | int | None,
    tmpl_count: float | int | None,
) -> None:
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


def evaluate_elf_guard(
    workspace: Path,
    title: str,
    sha: str | None = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect bloated `.o`/`.text` (>32KB I-cache tax), slow compile times, and template explosion."""
    ws = Path(workspace).expanduser().resolve()
    art = _load_run_artifacts(ws, title, sha=sha, require_passing=False, allow_build_logs=True)
    rj, log_text, repo_dir = art["result_json"], art["log_text"], art["repo_dir"]

    eff_thresh: dict[str, float | int] = dict(DEFAULT_ELF_THRESHOLDS)
    cfg_ft = _load_fast_tools_settings()
    for src in (cfg_ft.get("elf_guard"), thresholds):
        if isinstance(src, dict):
            for k, v in src.items():
                if k in eff_thresh and isinstance(v, (int, float)):
                    eff_thresh[k] = v

    parsed = _parse_validation_perf_safe(log_text)
    kbs = dict(parsed.get("kernel_binary_stats") or {})

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
    _append_elf_alerts(
        alerts,
        eff_thresh,
        o_max_text=o_max_text,
        o_max_bytes=o_max_bytes,
        build_sec=build_sec,
        tmpl_count=tmpl_count,
    )
    status = "redline" if any(a["severity"] == "redline" for a in alerts) else ("warn" if any(a["severity"] == "warn" for a in alerts) else "ok")
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
        "title": art["title"],
        "sha": art["sha"],
        "status": status,
        "metrics": metrics,
        "thresholds": eff_thresh,
        "alerts": alerts,
    }
