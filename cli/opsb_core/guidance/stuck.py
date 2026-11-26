"""P30 stuck detection: one class per live seat from data that already exists (no new probes).

Inputs are plain rows so the daemon tick and the replay of a past sprint run the same code:

``exps``    the seat's current ``experiment`` ledger rows, oldest first. The platform writes one per
            settled test run (``ledger.rows.test_entries``): outcome, level, speedup against the
            baseline SHA, timed cases (``per_case_us``) and failing cases (``scope.cases``);
``gates``   the platform ``hypothesis`` rows a submission request wrote (``ekey`` ``gate:<sha>``);
``errors``  ``{test_run_id: compile error signature}`` read from the test registry's error text;
``gap``     ``guidance.gap.triage`` of the problem: per-case ratio to tbest and weighted headroom.

Classes, first match wins:

``build_loop``      the last ``build_loop_runs`` runs failed to compile, or the last two failed with the
                    same compiler error;
``wa_loop``         one case WA/RE in ``wa_loop_runs`` runs that included it, with no pass in between;
``scale_mismatch``  ``scale_min_outliers`` of the ``scale_top_cases`` largest-headroom cases are at least
                    ``scale_ratio`` x tbest, while the last ``scale_recent_runs`` timed runs all moved less
                    than ``scale_small_change``;
``plateau``         the last ``plateau_runs`` timed runs stayed inside ``plateau_noise`` of the local best,
                    or ``plateau_minutes`` passed without a new local best over ``plateau_min_runs`` runs;
``no_candidate``    ``no_candidate_minutes`` without a passing standard/final run or a submission request.

A local best is kept per baseline SHA (speedups against different baselines do not compare); the first
passing run on a new baseline counts as a new best, because the baseline only moves when the board does.
"""
from __future__ import annotations

import datetime
import hashlib
import math
import re
from typing import Any, Iterable

CLASSES = ("build_loop", "wa_loop", "scale_mismatch", "plateau", "no_candidate")
FAILING = ("WA", "RE")
PASSING = ("better", "neutral", "noise", "worse")
FORMAL = ("standard", "final")
ERROR_LINE = re.compile(r"(?:^|\s)(?:fatal\s+)?error\b[:\s]", re.I)
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:[\w.+-]*/)+([\w.+-]+)")
_NUM = re.compile(r"\d+")


def parse_ts(value: Any) -> datetime.datetime | None:
    try:
        ts = datetime.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)


def _iso(ts: datetime.datetime | None) -> str | None:
    return ts.isoformat(timespec="seconds") if ts else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


def _actual(exp: dict[str, Any]) -> dict[str, Any]:
    return exp.get("actual_outcome") if isinstance(exp.get("actual_outcome"), dict) else {}


def level_of(exp: dict[str, Any]) -> str:
    return str(_actual(exp).get("level") or "")


def speedup_of(exp: dict[str, Any]) -> float | None:
    return _num(_actual(exp).get("speedup"))


def failing_cases(exp: dict[str, Any]) -> set[str]:
    scope = exp.get("scope") if isinstance(exp.get("scope"), dict) else {}
    return {str(c) for c in scope.get("cases") or []}


def timed_cases(exp: dict[str, Any]) -> set[str]:
    per_case = exp.get("per_case_us") if isinstance(exp.get("per_case_us"), dict) else {}
    return {str(c) for c in per_case}


def compile_signature(text: Any) -> str:
    """A stable short id of the first compiler error line: paths cut to the file name, numbers to ``#``."""
    for line in str(text or "").splitlines():
        if ERROR_LINE.search(line):
            norm = _NUM.sub("#", _PATH.sub(r"\1", " ".join(line.split())))
            return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:10] + " " + norm[:120]
    return ""


def _hit(cls: str, signal: str, since: datetime.datetime | None, **evidence: Any) -> dict[str, Any]:
    return {"class": cls, "signal": signal, "since": _iso(since), "evidence": evidence}


def build_loop(exps: list[dict[str, Any]], errors: dict[str, str], s: dict[str, Any]) -> dict[str, Any] | None:
    run: list[dict[str, Any]] = []
    for exp in reversed(exps):
        if exp.get("outcome") != "compile_fail":
            break
        run.append(exp)
    if not run:
        return None
    sigs = [errors.get(str(e.get("test_run_id") or ""), "") for e in run]
    same = len(run) >= 2 and sigs[0] and sigs[0] == sigs[1]
    if len(run) < int(s["build_loop_runs"]) and not same:
        return None
    signature, _, error = (sigs[0] or "").partition(" ")
    signal = (f"{len(run)} builds in a row failed" + (" with the same compiler error" if same else ""))
    return _hit("build_loop", signal, parse_ts(run[-1].get("ts")), runs=len(run), same_error=bool(same),
                signature=signature, error=(error or str(run[0].get("why") or ""))[:160],
                ids=[str(e.get("id")) for e in reversed(run)][-5:],
                last_ok_sha=_last_sha(exps, lambda e: e.get("outcome") not in (None, "compile_fail")))


def _last_sha(exps: Iterable[dict[str, Any]], pred: Any) -> str:
    return next((str(e.get("sha") or "") for e in reversed(list(exps)) if pred(e)), "")


def wa_loop(exps: list[dict[str, Any]], s: dict[str, Any]) -> dict[str, Any] | None:
    best: tuple[int, str, list[dict[str, Any]]] = (0, "", [])
    cases = sorted({c for e in exps for c in failing_cases(e)})
    for case in cases:
        run: list[dict[str, Any]] = []
        for exp in reversed(exps):
            if exp.get("outcome") in FAILING and case in failing_cases(exp):
                run.append(exp)
            elif case in timed_cases(exp) and case not in failing_cases(exp):
                break  # a run that timed the case passed it
        if len(run) > best[0]:
            best = (len(run), case, run)
    count, case, run = best
    if count < int(s["wa_loop_runs"]):
        return None
    return _hit("wa_loop", f"case {case} failed (WA/RE) in {count} runs in a row", parse_ts(run[-1].get("ts")),
                case=case, runs=count, detail=str(run[0].get("why") or "")[:200],
                ids=[str(e.get("id")) for e in reversed(run)][-5:],
                last_pass_sha=_last_sha(exps, lambda e: case in timed_cases(e) and case not in failing_cases(e)))


def progress(exps: list[dict[str, Any]], s: dict[str, Any]) -> dict[str, Any]:
    """Local-best bookkeeping: per-baseline best speedup, the new-best events and the runs since the last."""
    margin = float(s.get("plateau_noise") or 0.03)
    best: dict[str, float] = {}
    events: list[dict[str, Any]] = []
    timed: list[dict[str, Any]] = []
    for exp in exps:
        value = speedup_of(exp)
        if value is None or exp.get("outcome") not in PASSING:
            continue
        base = str(exp.get("base_sha") or "")
        timed.append(exp)
        if base not in best or value > best[base] * (1.0 + margin):
            events.append({"id": exp.get("id"), "ts": exp.get("ts"), "speedup": value, "base": base,
                           "sha": exp.get("sha")})
        best[base] = max(value, best.get(base, value))
    last = events[-1] if events else None
    after = [e for e in timed if last is None or str(e.get("ts") or "") > str(last.get("ts") or "")]
    return {"events": events, "last": last, "timed": timed, "since_best": after,
            "best": best.get(str(last.get("base"))) if last else None}


def plateau(exps: list[dict[str, Any]], now: datetime.datetime, start: datetime.datetime | None,
            s: dict[str, Any]) -> dict[str, Any] | None:
    prog = progress(exps, s)
    last, after = prog["last"], prog["since_best"]
    anchor = parse_ts(last.get("ts")) if last else start
    best, noise = prog["best"], float(s["plateau_noise"])
    runs = int(s["plateau_runs"])
    if best and len(after) >= runs:
        window = after[-runs:]
        same_base = [e for e in window if str(e.get("base_sha") or "") == str(last.get("base"))]
        if len(same_base) == len(window) and all(abs(speedup_of(e) / best - 1.0) <= noise for e in window):
            return _hit("plateau", f"last {runs} timed runs within ±{noise:.0%} of the local best {best:.3f}x",
                        anchor, runs=len(after), best=round(best, 4), rule="noise_band")
    if anchor is None:
        return None
    minutes = (now - anchor).total_seconds() / 60.0
    if minutes >= float(s["plateau_minutes"]) and len(after) >= int(s["plateau_min_runs"]):
        return _hit("plateau", f"{int(minutes)} min and {len(after)} timed runs without a new local best",
                    anchor, runs=len(after), minutes=int(minutes),
                    best=round(best, 4) if best else None, rule="minutes")
    return None


def scale_mismatch(exps: list[dict[str, Any]], gap: dict[str, Any] | None, s: dict[str, Any]) -> dict[str, Any] | None:
    cases = [c for c in (gap or {}).get("cases") or [] if _num(c.get("ratio")) is not None]
    if not cases:
        return None
    top = sorted(cases, key=lambda c: (-float(c.get("headroom_w") or 0.0), str(c.get("case"))))
    top = top[: int(s["scale_top_cases"])]
    outliers = [c for c in top if float(c["ratio"]) >= float(s["scale_ratio"])]
    if len(outliers) < int(s["scale_min_outliers"]):
        return None
    recent = [e for e in exps if speedup_of(e) is not None and e.get("outcome") in PASSING]
    recent = recent[-int(s["scale_recent_runs"]):]
    small = float(s["scale_small_change"])
    if len(recent) < int(s["scale_recent_runs"]) or any(abs(speedup_of(e) - 1.0) >= small for e in recent):
        return None
    worst = max(outliers, key=lambda c: float(c["ratio"]))
    return _hit("scale_mismatch",
                f"{len(outliers)} of the top {len(top)} gap cases are >= {s['scale_ratio']}x tbest "
                f"(worst {worst['case']} {float(worst['ratio']):.0f}x) while the last {len(recent)} runs moved "
                f"< {small:.0%}", parse_ts(recent[0].get("ts")),
                outliers=[c["case"] for c in outliers], worst=worst["case"], ratio=round(float(worst["ratio"]), 1),
                recent=[round(speedup_of(e), 4) for e in recent])


def no_candidate(exps: list[dict[str, Any]], gates: list[dict[str, Any]], now: datetime.datetime,
                 start: datetime.datetime | None, s: dict[str, Any]) -> dict[str, Any] | None:
    stamps = [parse_ts(e.get("ts")) for e in exps
              if level_of(e) in FORMAL and e.get("outcome") in ("better", "neutral", "noise")]
    stamps += [parse_ts(g.get("ts")) for g in gates]
    last = max([t for t in stamps if t] or [None], key=lambda t: t or datetime.datetime.min.replace(
        tzinfo=datetime.timezone.utc))
    anchor = max([t for t in (last, start) if t] or [None],
                 key=lambda t: t or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
    if anchor is None:
        return None
    minutes = (now - anchor).total_seconds() / 60.0
    if minutes < float(s["no_candidate_minutes"]):
        return None
    what = "since the last passing formal run" if last and last >= (start or last) else "since the seat started"
    return _hit("no_candidate", f"{int(minutes)} min without a submittable candidate ({what})", anchor,
                minutes=int(minutes), runs=len([e for e in exps if (parse_ts(e.get("ts")) or anchor) > anchor]))


def classify(exps: Iterable[dict[str, Any]], *, now: datetime.datetime, start: datetime.datetime | None = None,
             gates: Iterable[dict[str, Any]] = (), errors: dict[str, str] | None = None,
             gap: dict[str, Any] | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The seat's stuck class (``{class, signal, since, evidence}``), or None when it is not stuck."""
    from . import settings as _settings
    s = settings or _settings()
    rows = sorted((e for e in exps if e.get("type", "experiment") == "experiment"),
                  key=lambda e: str(e.get("ts") or ""))
    gate_rows = list(gates)
    for found in (build_loop(rows, errors or {}, s), wa_loop(rows, s), scale_mismatch(rows, gap, s),
                  plateau(rows, now, start, s), no_candidate(rows, gate_rows, now, start, s)):
        if found:
            return found
    return None
