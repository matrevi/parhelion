"""The platform report entry point and its two per-case views (4.2 D).

``tool_parhelion_report`` validates the view and the caller's role, then dispatches: ``per_case``
and ``diff`` live here because they compare stored runs of one problem, while ``history``,
``queue``, ``board``, ``bottleneck`` and ``env`` live in ``report_views``. Both modules share the
shaping helpers of ``report_views`` (byte budget, rounding, report documents). A worker reads its
own problem only; ``board`` and ``env`` also serve workers. Hard budgets: ``per_case``/``diff`` 4KB,
``env`` 1KB; list views page with ``offset``/``limit`` or ``cursor``.

``view="help"`` carries the per-column meanings that used to sit in the tool description (4.2 J-2):
the schema of a tool travels in every model call, a reply travels only when asked for. The answer is
about 3.5 KB, still inside ``mcp.output_cap_bytes`` (default 5 KB), so it never spills.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config as _cfg
from .report_views import (  # noqa: F401 - the helpers are re-exported for the module's callers
    BIG,
    ENV_BYTES,
    LIMIT_MAX,
    RULES_BYTES,
    _board,
    _bottleneck,
    _bytes,
    _clip,
    _doc,
    _env,
    _history,
    _limit,
    _num,
    _queue,
    _resolve,
    _round,
    _rules,
    _sha7,
    _trim,
)

VIEWS = ("per_case", "diff", "history", "queue", "board", "bottleneck", "rules", "env", "help")
PER_CASE_COLUMNS = ("case", "ours_us", "tbest_us", "gap_pct", "speedup", "band_pct", "verdict", "noisy",
                    "judge", "mapped", "local", "timer", "passed", "exit_code", "mismatch_n", "strict_fp32",
                    "cal", "wa", "run")
DIFF_COLUMNS = ("case", "ratio", "lo", "hi", "n", "method", "timer", "bootstrap", "verdict")

# What ``view="help"`` answers: the meaning of every row column and of the list views. It lives here
# instead of in the MCP schema because the schema is sent with every model call (4.2 J-2).
HELP: dict[str, dict[str, Any]] = {
    "columns": {
        "per_case": {
            "case": "judge case id",
            "ours_us": "our per-case time, us",
            "tbest_us": "best known time on the same generation",
            "gap_pct": "percent above tbest",
            "speedup": "ratio against the base SHA from the state card, 4 decimals",
            "band_pct": "noise band, percent",
            "verdict": "paired verdict: improved, regressed or indistinguishable",
            "noisy": "the verdict sits inside the noise band",
            "judge": "judge case id the row is compared as",
            "mapped": "a local case maps to the judge case",
            "local": "local case id",
            "timer": "event or profiler",
            "passed": "local run passed",
            "exit_code": "local run exit code",
            "mismatch_n": "mismatched elements the local run reported",
            "strict_fp32": "run setting",
            "cal": "per-case calibration label",
            "wa": "reproducer state of the newest WA/RE receipt",
            "run": "short run id",
        },
        "diff": {
            "case": "case id",
            "ratio": "base time divided by candidate time",
            "lo": "low end of the interval on ratio",
            "hi": "high end of the interval on ratio",
            "n": "samples per side",
            "method": "interval method",
            "timer": "event or profiler",
            "bootstrap": "[lo, hi] of a stored paired bootstrap",
            "verdict": "improved, regressed or indistinguishable",
        },
        "history": {
            "sid": "submission id",
            "sha": "submitted commit",
            "status": "official status text",
            "score": "official score",
            "beat_board": "score above the board when it was submitted",
            "at": "submission time",
            "note": "receipt note",
            "cases": "[case, ours_us, tbest_us, ratio], up to 20 rows",
            "mean_ratio": "mean of the per-case ratios",
        },
    },
    "views": {
        "per_case": "one row per case, defaults sha=HEAD and base=the same-generation board SHA",
        "diff": "cases above come from a stored paired report or from separate-run minima with a Welch interval",
        "history": "problems[] carries the judged count, best judged score and the latest receipt",
        "queue": "same source as `opsb queue list --json`; items carry id, kind, title, sha, badge, raw_status, "
                 "current/estimated/actual/delta score, gate lamps, note/detail/next_step, can_approve/skip/restore; "
                 "never an account field",
        "board": "cached board score, rank, total_teams, frontier top1/top5, observed_at, per-case tbest when a "
                 "problem is named, and the team ranking; reads the cache only, no network",
        "bottleneck": "category and limited_resource_pct from the PMU card, top_metrics (3 buckets), fixes "
                      "(KB ids and summaries), raw report/log/run_dir paths",
        "rules": "the judge's own rule for one problem; rule carries score_mode, use_baseline, "
                 "ranking_submission_mode, iterations, judge_revision, testcases{total, by_type, public, hidden} "
                 "and time_limit (only when Judge publishes one) plus fetched_at; formula is how a case and then "
                 "the problem are scored; tbest.cases/changed_at/changed_cases/observed_at say when the times "
                 "being scored against last moved; noise.sigma_hat/df/scope is this problem's judge noise or the "
                 "contest-wide estimate; flips counts same-source verdict changes (Pass<->WA/TLE) as judge noise; "
                 "split gives public and hidden points of the best and the board receipt "
                 "with verified{rows, public_cases, max_err}, and is absent when no receipt reproduces its own "
                 "judge score. Name a problem (a worker reads only its own) or omit it for the table "
                 "(columns: title, mode, cases, sigma_hat, fetched_at); <=2KB",
        "env": "platform version and root, daemon liveness, workspace/results/state dirs and free disk, <=1KB; "
               "no account, session or credential field",
    },
}


def _help() -> dict[str, Any]:
    return {"view": "help", **HELP}


# ---------------------------------------------------------------- caller and documents

def _caller(args: dict[str, Any], workspace: Path | str) -> tuple[str, str]:
    """``(role, problem)`` of the call; a worker is bound to the problem on its bus record."""
    from .handlers_ledger import problem_of

    worker = bool(args.get("_worker_request"))
    agent = str(args.get("_caller_agent_id") or "").strip()
    asked = str(args.get("problem") or "").strip()
    if not worker:
        return "orchestrator", asked
    if not agent:
        raise PermissionError("A bound worker identity is required")
    mine = problem_of(Path(workspace), agent)
    if not mine or (asked and asked.lower() != mine.lower()):
        raise PermissionError(f"You are staffed on {mine or 'no problem'}; a worker reads only its own report")
    return "worker", mine


def _docs(workspace: Path | str, title: str, sha: str = "") -> list[dict[str, Any]]:
    """Every report document of this problem; a legacy run keeps the minima from ``device_us``."""
    roots = [Path(workspace) / "results" / title, _cfg.workspace_data_dir(workspace)]
    patterns = [f"{sha or '*'}/bench-runs/*/report.json", "bench-runs/*/report.json",
                "bench-runs/*/*/report.json"]
    seen: set[Path] = set()
    docs: list[dict[str, Any]] = []
    for root in roots:
        for pattern in patterns:
            for path in sorted(root.glob(pattern)) if root.is_dir() else []:
                fresh = path not in seen and path.is_file()
                seen.add(path)
                doc = _doc(path) if fresh else {}
                for case in (doc.get("cases") or []) if isinstance(doc, dict) else []:
                    if not isinstance(case, dict) or case.get("min_us") is not None:
                        continue
                    samples = [value for value in (case.get("device_us") or []) if _num(value)]
                    if samples:
                        case["min_us"] = min(samples)
                if doc and (not sha or str(doc.get("sha") or "").lower().startswith(sha.lower())):
                    docs.append(doc)
    return docs


def _best(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return max(docs, key=lambda d: (d.get("formal") is True, str(d.get("finished_at") or "")), default={})


def _cases_of(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(c.get("id")): c for c in doc.get("cases") or [] if isinstance(c, dict) and c.get("id")}


# ---------------------------------------------------------------- per-case evidence

def _local_metrics(workspace: Path | str, title: str, sha: str, base: str) -> tuple[dict[str, Any], dict[str, Any]]:
    _cfg.ensure_scheduler_path()
    from scheduler import verdicts

    after, before = _best(_docs(workspace, title, sha)), _best(_docs(workspace, title, base))
    rows_before, out = _cases_of(before), {}
    for case_id, case in _cases_of(after).items():
        prior, status, bad = rows_before.get(case_id), str(case.get("status") or ""), case.get("mismatches")
        timer = str(case.get("timing_source") or after.get("timing_source") or "")
        out[case_id] = {
            "run": _sha7(after.get("run_id")),
            "timer": "profiler" if "profiler" in timer else "event" if timer else "",
            "exit_code": case.get("exit_code"),
            "passed": True if status == "PASS" else False if status in ("WA", "RE", "TLE", "CE", "SKIP") else None,
            "mismatch_n": len(bad) if isinstance(bad, list) else bad if type(bad) is int else None,
            "verdict": verdicts.paired(prior, case, before_source=before.get("timing_source"),
                                       after_source=after.get("timing_source")) if prior else None,
        }
    return out, after


def _wa_flags(workspace: Path | str, title: str, head: str, receipts: list[dict[str, Any]]) -> dict[str, Any]:
    _cfg.ensure_scheduler_path()
    from scheduler import wa_repro

    try:
        planned = wa_repro.load_manifest(Path(workspace), title).get("receipts") or {}
    except Exception:
        planned = {}
    for row in reversed(receipts):
        sid = str(row.get("submission_id") or "")
        if sid not in planned:
            continue
        state = wa_repro.status(Path(workspace), title, sid, str(row.get("sha") or ""), cand_sha=head or None)
        judges = {str(b.get("judge")): str(state.get("state") or "")
                  for b in planned[sid].get("bases") or [] if b.get("judge")}
        locals_ = {str(b.get("local")): str(state.get("state") or "")
                   for b in planned[sid].get("bases") or [] if b.get("local")}
        return {"receipt": wa_repro.sid8(sid), "state": state.get("state"),
                "variants": len(state.get("variants") or []), "judges": judges, "locals": locals_}
    return {}


def _per_case(workspace: Path | str, problem: str, args: dict[str, Any]) -> dict[str, Any]:
    from ..services import state_card_ext

    _cfg.ensure_scheduler_path()
    from scheduler import case_calibration, case_map

    ws, title = Path(workspace), str(problem)
    data = state_card_ext.data(ws, title, str(args.get("_caller_agent_id") or "") or None)
    head = _resolve(workspace, title, args.get("sha")) or str(data.get("head_sha") or "")
    base = _resolve(workspace, title, args.get("base")) or str(data.get("base_sha") or "")
    pair = state_card_ext.speedups(ws, title, head, base) if head and base and head != base else {}
    local_of = {str(p.get("judge")): str(p.get("local"))
                for p in (case_map.current(ws, title, write=False).get("pairs") or []) if p.get("judge")}
    try:  # the per-case calibration label lives in the stored records; an absent map is not fatal
        calibration = case_calibration.table(ws, title).get("cases") or {}
    except Exception:
        calibration = {}
    metrics, after = _local_metrics(ws, title, head, base)
    wa = _wa_flags(workspace, title, head, state_card_ext.receipts_rows(ws, title))
    strict = bool((after.get("settings") or {}).get("strict_fp32"))
    rows = []
    for row in data.get("cases") or []:
        judge = str(row.get("judge") or "")
        local = local_of.get(judge)
        case = metrics.get(local) or {}
        verdict = case.get("verdict") or {}
        rows.append([
            row.get("case"), row.get("ours_us"), row.get("tbest_us"),
            round(float(row["gap_pct"]) * 100, 2) if row.get("gap_pct") is not None else None,
            _round((pair or {}).get("cases", {}).get(local), 4) if local else None,
            round(float(verdict["band"]) * 100, 2) if verdict.get("band") is not None else None,
            verdict.get("verdict") or "unknown", verdict.get("noisy"), judge, bool(local), local,
            case.get("timer") or "", case.get("passed"), case.get("exit_code"), case.get("mismatch_n"), strict,
            (calibration.get(local) or {}).get("label") if local else None,
            (wa.get("judges") or {}).get(judge, "") or (wa.get("locals") or {}).get(local or "", ""),
            case.get("run"),
        ])
    out = {
        "view": "per_case", "problem": title, "sha": _sha7(head), "base": _sha7(base),
        "head": _sha7(data.get("head_sha")), "speedup_confidence": (pair or {}).get("confidence"),
        "wa_state": wa.get("state"), "wa_receipt": wa.get("receipt"), "strict_fp32": strict,
        "columns": PER_CASE_COLUMNS, "cases": rows,
    }
    return _trim(out, "cases", BIG)


def _verdict_of(ci: dict[str, Any]) -> str:
    if (_num(ci.get("lo")) or 0.0) > 1.0:
        return "improved"
    return "regressed" if (_num(ci.get("hi")) or 2.0) < 1.0 else "indistinguishable"


def _diff(workspace: Path | str, problem: str, args: dict[str, Any]) -> dict[str, Any]:
    from ..ledger.gitref import git_out, problem_repo
    from ..services import state_card_ext

    _cfg.ensure_scheduler_path()
    from scheduler import paired_timing, timing

    ws, title = Path(workspace), str(problem)
    cand = _resolve(workspace, title, args.get("sha")) or state_card_ext.head_sha(ws, title)
    base = _resolve(workspace, title, args.get("base")) or state_card_ext.base_of(ws, title)
    if not cand or not base or cand == base:
        raise ValueError("diff needs two different SHAs: pass sha and base, or run a formal test first")
    paired = paired_timing.load(ws, title, cand, base)
    rows: list[list[Any]] = []
    source, geomean = "separate-runs", None
    if paired:
        source = "paired-report"
        summary = (paired.get("digest") or {}).get("paired")
        geomean = _round((summary or {}).get("geomean_ratio"), 4) if isinstance(summary, dict) else None
        for case in paired.get("cases") or []:
            if not isinstance(case, dict):
                continue
            ci, boot = case.get("pair") or {}, case.get("pair_bootstrap") or {}
            if not isinstance(ci, dict):
                continue
            boot = boot if isinstance(boot, dict) else {}
            rows.append([
                case.get("id"), _round(ci.get("ratio"), 4), _round(ci.get("lo"), 4), _round(ci.get("hi"), 4),
                ci.get("n"), ci.get("method"), ci.get("source") or "event",
                [_round(boot.get("lo"), 4), _round(boot.get("hi"), 4)] if boot.get("lo") is not None else None,
                _verdict_of(ci),
            ])
    else:
        docs = {side: _docs(ws, title, sha) for side, sha in (("cand", cand), ("base", base))}
        minima = {side: {src: timing.best_minima(docs[side], src) for src in timing.SOURCES} for side in docs}
        chosen = timing.common_source(minima["cand"], minima["base"])
        if chosen:
            timer, cand_min, base_min = chosen
            after_map, before_map = _cases_of(_best(docs["cand"])), _cases_of(_best(docs["base"]))

            def samples(case: dict[str, Any], src: str) -> list[float]:
                values = case.get(f"{src}_us") or (case.get("device_us") if src == "event" else None)
                return [float(v) for v in values or [] if _num(v) and float(v) > 0]

            for case_id in sorted(set(cand_min) & set(base_min)):
                ci = paired_timing.unpaired_interval(
                    samples(after_map.get(case_id) or {}, timer),
                    samples(before_map.get(case_id) or {}, timer)) or {}
                rows.append([case_id, _round(base_min[case_id] / cand_min[case_id], 4),
                             _round(ci.get("lo"), 4), _round(ci.get("hi"), 4), ci.get("n"),
                             ci.get("method") or "minima", timer, None, _verdict_of(ci)])
    repo = problem_repo(ws, title)
    output = (git_out(repo, "diff", "--stat", base, cand) if repo else "") or ""
    stat = [_clip(line, 160) for line in output.strip().splitlines() if line.strip()][:12]
    out = {"view": "diff", "problem": title, "sha": _sha7(cand), "base": _sha7(base), "source": source,
           "geomean": geomean, "git": {"stat": stat}, "columns": DIFF_COLUMNS, "cases": rows}
    out = _trim(out, "cases", BIG, floor=0)
    while _bytes(out) > BIG and out["git"]["stat"]:
        out["git"]["stat"].pop()
    return out


def tool_parhelion_report(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Read one platform report view; ``mcp.tools`` already fixed which roles may call it."""
    view = str(args.get("view") or "").strip().lower()
    if view not in VIEWS:
        raise ValueError("view must be one of " + ", ".join(VIEWS))
    if view == "help":  # role- and problem-independent: it documents the other views
        return _help()
    ws = Path(workspace).expanduser().resolve()
    role, problem = _caller(args, ws)
    if view == "env":
        return _env(ws)
    if view in ("board", "history", "queue", "rules"):
        return {"board": _board, "history": _history, "queue": _queue, "rules": _rules}[view](
            ws, problem, args, role)
    if not problem:
        raise PermissionError("the problem of this call is unknown: pass problem or use a bound worker")
    return {"per_case": _per_case, "diff": _diff, "bottleneck": _bottleneck}[view](ws, problem, args)
