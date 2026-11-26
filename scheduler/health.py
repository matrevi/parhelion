"""Pre-submission mechanical health of one SHA (4.3 P24).

Stage 1 used to read only pass/fail evidence. Three of the seven online incidents the last contest
left behind were visible in numbers the platform already produced: too many template
instantiations, a bloated kernel object, and a cold-start spike that the local warmup hid
(``docs/v4.3/research/antigravity/00-SUMMARY.md`` §4.5). :func:`check` collects those numbers for
the candidate from its newest formal run and for the SHA currently on the board, so the gate can
show growth percentages and the worker can see where the headroom went.

What it reads, in order (the first source that has a field wins):

``bench-runs/<run>/report.json``
    The formal run report. A reference-scheme run records the build metrics emitted by the remote
    runner (``build_metrics``: compile seconds, template instantiations, kernel object bytes), so
    this is the primary source — the report also says whether the run reused its ACL session.
``results/<title>/<sha>/result.json``
    The formal pointer of the older ``runtime/npu-test.py`` path, which records the same fields at
    the top level (``phase_a_cpu_sec``, ``template_instantiations``, ``kernel_o_*``).

Missing data is never a hold: a field with no record in either source is left out, the check reports
``checked=false`` with a reason, and Stage 1 keeps its 4.2.1 behaviour.

Two things the gate turns into a decision:

* **warnings** — the ``elf_guard`` alerts for this SHA plus a metric that grew
  ``GROWTH_WARN_PCT`` percent or more over the board SHA's same metric. Warnings only scale the
  dispatch priority (``GateConfig.health_penalty``); they never hold.
* **judge time risk** — the judge would spend ``compile_sec * compile_factor + predicted_run_sec``
  seconds on this SHA, where ``predicted_run_sec`` is the sum of the per-case times
  ``scheduler.receipt_predict`` predicts times the judge's ``iterations`` (P22 rule snapshot;
  ``judge.iterations`` when absent), and the sum reaches ``time_margin`` of the judge's time limit.
  With no usable prediction the compile term alone is used; with no published limit (neither the
  rule snapshot nor ``judge.time_limit_sec``) the check is skipped, because a guessed limit would be
  an invented device value.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import case_map

#: A metric this many percent above the board SHA's same metric is a warning (hint only).
GROWTH_WARN_PCT = 50.0
#: Share of the judge's time limit at which the compile-plus-run estimate counts as risky.
HEALTH_TIME_MARGIN = 0.8
#: Source of the four metrics, in precedence order.
METRICS = ("compile_sec", "template_instantiations", "kernel_max_bytes", "cold_ratio")
#: Report/record field -> view field. Both source formats are tried; the first one that has it wins.
BUILD_FIELDS = (("compile_sec", "compile_sec"), ("phase_a_cpu_sec", "compile_sec"),
                ("template_instantiations", "template_instantiations"),
                ("kernel_o_max_bytes", "kernel_max_bytes"))


# ---- optional CLI modules (deployment settings, the two fast-feedback probes) ----------------------

def _cli(module: str, name: str) -> Any:
    """A CLI helper; the scheduler may run without the CLI package on ``sys.path``."""
    for prefix in ("cli.opsb_core", "opsb_core"):
        try:
            return getattr(__import__(f"{prefix}.{module}", fromlist=[name]), name)
        except (ImportError, AttributeError):
            continue
    return None


def _home() -> Path:
    override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".parhelion").resolve()


def _setting(key: str) -> Any:
    """One deployment setting from ``config.json``: the ``judge`` block, then the ``device`` block."""
    try:
        doc = json.loads((_home() / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for block in ("judge", "device"):
        values = doc.get(block) if isinstance(doc.get(block), dict) else {}
        if values.get(key) not in (None, ""):
            return values[key]
    return None


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and value == value else None


# ---- metric collection -----------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _formal_runs(ws: Path, title: str, sha: str) -> list[dict[str, Any]]:
    """Formal run reports of one SHA, newest first (``case_map._reports`` is cached)."""
    docs = [doc for doc in case_map._reports(ws, title, sha) if doc.get("formal") is True]
    docs.sort(key=lambda doc: str(doc.get("finished_at") or ""), reverse=True)
    return docs


def _from_build_metrics(doc: dict[str, Any]) -> dict[str, Any]:
    """The candidate's build metrics of one run report plus the ACL session-reuse setting."""
    out: dict[str, Any] = {}
    entries = [item for item in doc.get("build_metrics") or [] if isinstance(item, dict)]
    entry = next((item for item in entries if item.get("candidate")), entries[0] if entries else {})
    for src, dst in BUILD_FIELDS:
        number = _number(entry.get(src))
        if number is not None:
            out[dst] = number
    if not out:  # an older report may carry the npu-test fields at the top level
        for src, dst in BUILD_FIELDS:
            number = _number(doc.get(src))
            if number is not None:
                out[dst] = number
    settings = doc.get("settings") if isinstance(doc.get("settings"), dict) else {}
    reuse = settings.get("reuse_session")
    if isinstance(reuse, bool):
        out["session_reuse"] = reuse
    if doc.get("run_id"):
        out["run_id"] = doc["run_id"]
    return out


def _cold_ratio(ws: Path, title: str, sha: str) -> dict[str, Any] | None:
    """``first_iter_us / warm_median_us`` when a cold3 run recorded both (fast_feedback.cold3_probe)."""
    probe = _cli("fast_feedback.cold3_probe", "evaluate_cold3_probe")
    if probe is None:
        return None
    try:
        result = probe(ws, title, sha)
    except Exception:  # noqa: BLE001 - a probe is optional telemetry
        return None
    metrics = result.get("metrics") if isinstance(result, dict) else None
    if not isinstance(metrics, dict) or not metrics.get("has_telemetry"):
        return None
    first, warm = _number(metrics.get("first_iter_us")), _number(metrics.get("warm_median_us"))
    if not first or not warm or warm <= 0:
        return None
    return {"first_iter_us": round(first, 3), "warm_median_us": round(warm, 3),
            "ratio": round(first / warm, 3), "bimodal": bool(metrics.get("bimodal_spike_detected")),
            "source": "cold3"}


def _metrics(ws: Path, title: str, sha: str) -> dict[str, Any]:
    """The four metrics of one SHA from its newest formal run, filling gaps from ``result.json``."""
    out: dict[str, Any] = {"metrics": {}, "sources": [], "run_id": None, "session_reuse": None,
                           "skipped": ""}
    runs = _formal_runs(ws, title, sha) if sha else []
    for doc in runs:
        found = _from_build_metrics(doc)
        if found:
            out["metrics"].update({key: value for key, value in found.items() if key in METRICS})
            out["run_id"] = out["run_id"] or found.get("run_id")
            if found.get("session_reuse") is not None:
                out["session_reuse"] = found["session_reuse"]
            if out["metrics"]:
                out["sources"].append("bench-run")
                break
    record = _load_json(ws / "results" / title / sha / "result.json") if sha else {}
    if record:
        added = False
        for src, dst in BUILD_FIELDS:
            number = _number(record.get(src))
            if number is not None and dst not in out["metrics"]:
                out["metrics"][dst] = number
                added = True
        if added:
            out["sources"].append("result.json")
        out["run_id"] = out["run_id"] or record.get("run_id")
    cold = _cold_ratio(ws, title, sha) if sha else None
    if cold is not None:
        out["metrics"]["cold_ratio"] = cold["ratio"]
        out["cold"] = cold
        out["sources"].append("cold3")
    if not out["metrics"]:
        out["skipped"] = (f"no formal run record for {sha[:7]}" if sha
                          else "no SHA to check")
    return out


# ---- warnings, growth and the time limit -----------------------------------------------------------

def _growth(metrics: dict[str, Any], board: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, Any] = {}
    for key in METRICS:
        current, base = _number(metrics.get(key)), _number(board.get(key))
        out[key] = round((current / base - 1.0) * 100.0, 1) if current is not None and base else None
    return out


def _elf_alerts(ws: Path, title: str, sha: str, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """``elf_guard`` alerts for this SHA, fed with the run report's build metrics when present."""
    guard = _cli("fast_feedback.elf_guard", "evaluate_elf_guard")
    if guard is None or not sha:
        return []
    build = {"phase_a_cpu_sec": metrics.get("compile_sec"),
             "template_instantiations": metrics.get("template_instantiations"),
             "kernel_o_max_bytes": metrics.get("kernel_max_bytes")}
    try:
        result = guard(ws, title, sha, build_metrics=build)
    except Exception:  # noqa: BLE001 - a probe fault must not fail a submission
        return []
    alerts = result.get("alerts") if isinstance(result, dict) else None
    return [a for a in alerts or [] if isinstance(a, dict)]


def _warnings(view: dict[str, Any]) -> list[dict[str, Any]]:
    out = [{"code": str(a.get("code") or "ELF_GUARD"), "severity": str(a.get("severity") or "warn"),
            "metric": a.get("metric"), "value": a.get("value"), "reason": a.get("message") or ""}
           for a in view.get("elf_alerts") or []]
    for key, pct in (view.get("growth_pct") or {}).items():
        if pct is not None and pct >= GROWTH_WARN_PCT:
            out.append({"code": "HEALTH_METRIC_GROWTH", "severity": "warn", "metric": key,
                        "value": view["metrics"].get(key),
                        "reason": f"{key} is {pct:+.1f}% vs the board SHA "
                                  f"{str(view.get('board_sha') or '')[:7]}"})
    return out


def _snapshot_limit(ws: Path, title: str) -> tuple[float | None, str]:
    """The judge's own time limit from the P22 rule snapshot (seconds), and where it came from."""
    limit = _cli("services.judge_rules", "time_limit_sec")
    if limit is None:
        return None, ""
    try:
        seconds = limit(ws, title)
    except Exception:  # noqa: BLE001 - a broken snapshot falls back, it never raises
        return None, ""
    return (float(seconds), "rule-snapshot") if seconds else (None, "")


def _iterations(ws: Path, title: str) -> tuple[int, str]:
    """The judge's iteration count: the rule snapshot, then ``judge.iterations``, then 1."""
    snapshot = _cli("services.judge_rules", "iterations")
    if snapshot is not None:
        try:
            count = snapshot(ws, title)
        except Exception:  # noqa: BLE001
            count = None
        if count:
            return int(count), "rule-snapshot"
    configured = _number(_setting("iterations"))
    if configured and configured >= 1:
        return int(configured), "config"
    return 1, "default"


def _predicted_run_sec(ws: Path, title: str, sha: str, board_sha: str | None) -> tuple[float | None, str]:
    """Sum of the predicted per-case judge times (receipt_predict), or None when it cannot predict."""
    if not board_sha:
        return None, "no board SHA to predict against"
    try:
        from . import receipt_predict
        doc = receipt_predict.predict(ws, title, sha, [board_sha])
    except Exception as exc:  # noqa: BLE001 - the compile term stands alone
        return None, f"receipt_predict unavailable: {type(exc).__name__}"
    if not doc:
        return None, "no per-case prediction (no case map or judged base)"
    total_us = sum(_number(case.get("t_pred")) or 0.0
                   for case in (doc.get("cases") or {}).values() if isinstance(case, dict))
    if total_us <= 0:
        return None, "no per-case prediction (no case map or judged base)"
    return round(total_us / 1e6, 4), f"receipt_predict {doc.get('matched')}/{doc.get('timed')} cases"


def _time_risk(ws: Path, title: str, sha: str, board_sha: str | None, metrics: dict[str, Any],
               compile_factor: float, time_margin: float) -> dict[str, Any]:
    """The judge time-limit estimate; ``risk`` stays None when a factor of the formula is missing."""
    out: dict[str, Any] = {"risk": None, "skipped": "", "compile_sec": metrics.get("compile_sec"),
                           "compile_factor": compile_factor, "time_margin": time_margin,
                           "predicted_run_sec": None, "iterations": None, "limit_sec": None}
    limit, limit_source = _snapshot_limit(ws, title)
    if limit is None:
        configured = _number(_setting("time_limit_sec"))
        if configured and configured > 0:
            limit, limit_source = configured, "config"
    out.update(limit_sec=limit, limit_source=limit_source)
    if out["compile_sec"] is None:
        out["skipped"] = "no compile seconds recorded for this SHA"
        return out
    if limit is None:
        out["skipped"] = ("judge publishes no time limit and judge.time_limit_sec is unset; "
                          "risk not evaluated")
        return out
    iterations, iterations_source = _iterations(ws, title)
    predicted, note = _predicted_run_sec(ws, title, sha, board_sha)
    out.update(predicted_run_sec=predicted, iterations=iterations, iterations_source=iterations_source,
               prediction=note)
    compile_term = float(out["compile_sec"]) * compile_factor
    required = compile_term + (predicted or 0.0) * iterations
    budget = time_margin * limit
    out.update(compile_term_sec=round(compile_term, 3), required_sec=round(required, 3),
               budget_sec=round(budget, 3), risk=bool(required >= budget),
               detail=(f"compile {float(out['compile_sec']):.1f}s x {compile_factor:.2f} + "
                       f"run {round((predicted or 0.0) * iterations, 3):.3f}s = {required:.2f}s vs "
                       f"{time_margin:.2f} x {limit:.1f}s limit"))
    return out


# ---- entry point -----------------------------------------------------------------------------------

def check(workspace: Path | str, title: str, sha: str, board_sha: str | None = None, *,
          compile_factor: float = 1.0, time_margin: float = HEALTH_TIME_MARGIN) -> dict[str, Any]:
    """The S1 health view of ``sha`` against ``board_sha`` (see the module docstring).

    ``board_sha`` may be empty on a zeroed board; the check then reports no growth and no time
    prediction against a board, and the missing pieces are named in ``skipped``.
    """
    ws = Path(workspace).expanduser().resolve()
    title, sha = str(title), str(sha or "")
    factor, margin = _number(compile_factor) or 1.0, _number(time_margin) or HEALTH_TIME_MARGIN
    found = _metrics(ws, title, sha)
    board = _metrics(ws, title, str(board_sha or "")) if board_sha else {"metrics": {}}
    metrics = found["metrics"]
    view: dict[str, Any] = {
        "checked": bool(metrics), "sha": sha, "board_sha": (str(board_sha) if board_sha else None),
        "metrics": metrics, "board_metrics": board.get("metrics") or {},
        "sources": found["sources"], "run_id": found.get("run_id"),
        "session_reuse": found.get("session_reuse"),
        "growth_pct": _growth(metrics, board.get("metrics") or {}),
        "warnings": [], "elf_alerts": [], "time_risk": {}, "skipped": found["skipped"],
        "thresholds": {"growth_warn_pct": GROWTH_WARN_PCT, "time_margin": margin},
    }
    if not metrics:
        return view
    view["elf_alerts"] = _elf_alerts(ws, title, sha, metrics)
    view["warnings"] = _warnings(view)
    view["time_risk"] = _time_risk(ws, title, sha, board_sha, metrics, factor, margin)
    view["status"] = ("redline" if any(a.get("severity") == "redline" for a in view["elf_alerts"])
                      else "warn" if view["warnings"] else "ok")
    return view


def next_step(view: dict[str, Any] | None) -> str:
    """The worker-facing next step of a judge-time-risk hold."""
    risk = (view or {}).get("time_risk") or {}
    detail = risk.get("detail") or "the compile-plus-run estimate reaches the judge's time limit"
    return f"{detail}; reduce instantiations or compile volume, or send it as intent=probe"
