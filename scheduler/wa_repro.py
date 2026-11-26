"""Local reproducers for judge WA/RE receipts: multi-seed and tie-rich suite variants (4.1 #6).

``plan_variants`` picks the local official cases behind a failed receipt and derives variants:

- the failing judge cases come from the receipt's ``case_results`` (scheduler.receipts);
- a case-map pair names the local case (``mapped=true``). Without one, the 1-2 official cases
  nearest in time magnitude stand in; when the receipt names no failing case at all (receipts
  from before 4.1), a ``like`` hint such as ``{"shape": [2048, 2048], "mode": 0}`` picks the
  cases nearest in element count with the same mode and overrides their shape (``mapped=false``);
- SEEDS variants with seeds derived from ``submission_id``, spread over those cases, and the
  oracle's tie-rich inputs where it can express them (scheduler.wa_ties).

Variants are ``local/wa-<sid8>-<nn>`` (``sid8``: the last 8 characters of the ID, the unique part
of a judge object ID), carry ``origin="wa-repro"``, ``receipt``, ``judge_testcase_id`` and
``mapped``, and enter only the ``final`` level. ``apply`` writes them in one suite version and
records the plan in ``bench-specs/<title>/wa-repro.json``; replaying a receipt writes nothing.

``status`` derives the escape-lock state from immutable runs: ``pending`` until a formal run of
the bad SHA fails a variant (``reproduced``, with the run ID), ``fixed`` for a candidate whose
formal run passes every variant of the receipt on the same inputs (same case keys), and
``unreproduced`` when the bad SHA's own formal, final-level runs pass every variant often enough
(``unrepro_runs``, default 2) that the online verdict is more likely judge-side noise than a defect
(4.3 P24; when the run records distinguish session reuse, at least one of those runs must have it
off). ``escalation`` turns that state into the S1 decision: hold, release, refuse the bad SHA
unchanged, or pass a proven fix.
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import math
from typing import Any

from . import case_map, judge_cases, receipts, wa_ties
from . import suite as suites
from .parhelion_paths import atomic_write_json, workspace_bench_specs_dir

FAILED = ("Wrong Answer", "Runtime Error")
LOCAL_FAILURES = ("WA", "RE")
SEEDS = 12
MANIFEST = "wa-repro.json"
#: Formal, final-level runs of the bad SHA that must pass every variant before the lock is released.
UNREPRO_RUNS = 2


def manifest_path(ws, title):
    return workspace_bench_specs_dir(ws) / title / MANIFEST


def load_manifest(ws, title) -> dict[str, Any]:
    try:
        doc = json.loads(manifest_path(ws, title).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    return doc if isinstance(doc, dict) and isinstance(doc.get("receipts"), dict) else {"receipts": {}}


def sid8(sid: str) -> str:
    return str(sid)[-8:]


def derived_seed(sid: str, n: int) -> int:
    return int(hashlib.sha256(f"{sid}:{n}".encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def failing_cases(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in receipts.case_results(receipt) if r.get("status") not in ("Pass", "Accepted")]


def official_cases(ws, title) -> list[dict[str, Any]]:
    """Official rows (id, params, seed, min_us) of the newest formal run of any SHA."""
    newest = max(case_map._reports(ws, title), key=lambda d: d.get("finished_at") or "", default=None)
    return [c for c in (newest or {}).get("cases") or [] if c.get("source") == "official"]


def _elements(params: dict[str, Any]) -> int | None:
    shape = params.get("shape")
    if isinstance(shape, list) and shape and all(isinstance(n, int) and n > 0 for n in shape):
        return math.prod(shape)
    return None


def _nearest(scored: list[tuple[float, str]]) -> list[str]:
    """The nearest case, and the runner-up when it is within a factor of 2 of the same distance."""
    scored = sorted(scored)
    return [cid for dist, cid in scored[:2] if dist <= scored[0][0] + math.log(2)] if scored else []


def select_bases(ws, title, receipt, official, like=None) -> tuple[list[dict[str, Any]], str]:
    """``([{local, judge, mapped, reason}], why)``; an empty list says why nothing was chosen."""
    by_id = {c["id"]: c for c in official}
    failing = failing_cases(receipt)
    out: list[dict[str, Any]] = []
    if failing:
        doc = case_map.current(ws, title, write=False)
        pairs = {p["judge"]: p for p in doc.get("pairs") or []} if case_map.usable(doc) else {}
        sha = str(receipt.get("sha") or "")
        minima = case_map.local_minima(ws, title, sha, doc.get("pairing_key"))[0] if sha else {}
        tbest, _, _ = judge_cases.tbest_now(ws, title)
        for case in failing:
            key = case["testcase_id"]
            pair = pairs.get(key)
            if pair and pair["local"] in by_id:
                note = " (local input is another size)" if pair.get("scale_mismatch") else ""
                out.append({"local": pair["local"], "judge": key, "mapped": True,
                            "reason": f"{doc['method']} case map{note}"})
                continue
            judged = case.get("time_us") or case.get("tbest_us") or tbest.get(key)
            local = {cid: minima.get(cid) or by_id[cid].get("min_us") for cid in by_id}
            scored = [(abs(math.log(v / judged)), cid) for cid, v in local.items() if v and judged]
            for cid in _nearest(scored):
                out.append({"local": cid, "judge": key, "mapped": False,
                            "reason": f"no case-map pair; nearest in time to the judge's {judged:.3g} us"})
        if not out:
            return [], "failing judge case has no case-map pair and no time to compare"
    elif like:
        want = _elements(like)
        mode = like.get("mode")
        scored = [(abs(math.log(_elements(c["params"]) / want)), c["id"]) for c in official
                  if want and _elements(c.get("params") or {}) and "values" not in c["params"]
                  and (mode is None or c["params"].get("mode") == mode)]
        out = [{"local": cid, "judge": None, "mapped": False,
                "reason": f"receipt names no failing case; nearest to {json.dumps(like, separators=(',', ':'))}"}
               for cid in _nearest(scored)]
        if not out:
            return [], f"no official case resembles {like}"
    else:
        return [], "the receipt does not name the failing case; give the failure shape with --like"
    seen, unique = set(), []
    for base in out:
        if base["local"] not in seen:
            seen.add(base["local"])
            unique.append(base)
    return unique, ""


def plan_variants(ws, title, receipt, like=None) -> dict[str, Any]:
    sid = str(receipt["submission_id"])
    official = official_cases(ws, title)
    by_id = {c["id"]: c for c in official}
    bases, why = select_bases(ws, title, receipt, official, like)
    meta = {"origin": "wa-repro", "receipt": sid, "tier": "final"}
    variants: list[dict[str, Any]] = []
    seedable = [b for b in bases if isinstance(by_id[b["local"]].get("seed"), int)
                and "values" not in (by_id[b["local"]].get("params") or {})]
    for n in range(SEEDS if seedable else 0):
        base = seedable[n % len(seedable)]
        params = by_id[base["local"]].get("params") or {}
        override = ({"shape": list(like["shape"])} if like and like.get("shape") and "shape" in params
                    and params["shape"] != like["shape"] else {})
        seed = derived_seed(sid, n)
        variants.append({"id": f"local/wa-{sid8(sid)}-{n:02d}", "base": base["local"], "seed": seed,
                         **({"params": override} if override else {}),
                         "name": f"wa-repro {receipt.get('status')} {sid8(sid)}: case {base['local']} seed {seed}",
                         "judge_testcase_id": base["judge"], "mapped": base["mapped"], **meta})
    modes = {(by_id[b["local"]].get("params") or {}).get("mode") for b in bases} | {(like or {}).get("mode")}
    explicit = wa_ties.explicit_case(official) if bases and 0 in modes else None
    ties = []
    for i, body in enumerate(wa_ties.tie_variants(explicit, sid) if explicit else []):
        ties.append({"id": f"local/wa-{sid8(sid)}-{SEEDS + i:02d}", **body, "judge_testcase_id": bases[0]["judge"],
                     "mapped": False, **meta})
    return {"title": title, "submission_id": sid, "sha": receipt.get("sha"), "status": receipt.get("status"),
            "failing": [c["testcase_id"] for c in failing_cases(receipt)], "bases": bases, "reason": why,
            "like": like, "variants": variants, "ties": ties, "tie_base": explicit["id"] if explicit else None}


def _shared_ties(suite: dict[str, Any], tie_base: str | None) -> list[str]:
    """Tie-rich variants an earlier receipt of this problem already added (they are large)."""
    return [v["id"] for v in suite["variants"]
            if v.get("origin") == "wa-repro" and v.get("base") == tie_base and "values" in (v.get("params") or {})]


def apply(ws, title, receipt, like=None, repo=None) -> dict[str, Any]:
    """Write the receipt's variants into the suite once; a replay of the same receipt adds nothing."""
    sid = str(receipt.get("submission_id") or "")
    if not sid or receipt.get("status") not in FAILED:
        return {"applied": False, "reason": "not a WA/RE receipt"}
    folder = manifest_path(ws, title).parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".wa-repro.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = load_manifest(ws, title)
        entry = manifest["receipts"].get(sid) or {}
        if entry.get("variants"):
            return {"applied": False, "reason": "already planned", "entry": entry}
        plan = plan_variants(ws, title, receipt, like)
        suite = suites.load_suite(ws, title)
        shared = _shared_ties(suite, plan["tie_base"]) if plan["ties"] else []
        own = plan["variants"] + ([] if shared else plan["ties"])
        known = {v["id"] for v in suite["variants"]}
        new = [v for v in own if v["id"] not in known]
        if new:
            suite = suites.add_variants(ws, title, new, repo)
        ids = [v["id"] for v in own] + shared
        manifest["receipts"][sid] = {
            "sha": plan["sha"], "status": plan["status"], "failing": plan["failing"], "bases": plan["bases"],
            "like": like, "reason": plan["reason"], "variants": ids, "shared": shared,
            "suite_version": suite.get("suite_version"),
            "planned_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        atomic_write_json(manifest_path(ws, title), manifest)
    return {"applied": bool(ids), "added": [v["id"] for v in new], "entry": manifest["receipts"][sid]}


def _rows(doc: dict[str, Any], ids: set[str]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in doc.get("cases") or [] if c.get("id") in ids}


def _before(doc: dict[str, Any], as_of) -> bool:
    done = judge_cases.parse_iso(doc.get("finished_at"))
    return as_of is None or (done is not None and done < as_of)


def _formal_final(doc: dict[str, Any]) -> bool:
    """A formal run of the suite's final level; the variants only enter that level."""
    if doc.get("formal") is not True:
        return False
    return doc.get("level") in (None, "", "final")


def _reuse_session(doc: dict[str, Any]) -> bool | None:
    """The run's ACL session-reuse setting, or None for a run format that does not record it."""
    settings = doc.get("settings") if isinstance(doc.get("settings"), dict) else {}
    value = settings.get("reuse_session")
    return value if isinstance(value, bool) else None


def _all_variants_pass(doc: dict[str, Any], ids: list[str]) -> bool:
    rows = _rows(doc, set(ids))
    return len(rows) == len(ids) and all(r.get("status") == "PASS" for r in rows.values())


def status(ws, title, sid, bad_sha, cand_sha=None, as_of=None,
           unrepro_runs: int = UNREPRO_RUNS) -> dict[str, Any]:
    """``none`` / ``pending`` / ``reproduced`` / ``fixed`` / ``unreproduced`` for one receipt, from formal runs."""
    entry = load_manifest(ws, title)["receipts"].get(str(sid or "")) or {}
    ids = list(entry.get("variants") or [])
    if not ids or not bad_sha:
        return {"state": "none", "variants": [], "reason": entry.get("reason")}
    out: dict[str, Any] = {"state": "pending", "variants": ids, "bad_sha": bad_sha,
                           "submission_id": str(sid or "")}
    repro = None
    passed: list[dict[str, Any]] = []
    for doc in case_map._reports(ws, title, bad_sha):
        rows = _rows(doc, set(ids))
        failed = sorted(i for i, r in rows.items() if r.get("status") in LOCAL_FAILURES)
        if failed and _before(doc, as_of) and (repro is None or doc.get("finished_at", "") < repro[0]):
            repro = (doc.get("finished_at", ""), doc, failed)
        elif _before(doc, as_of) and _formal_final(doc) and _all_variants_pass(doc, ids):
            passed.append(doc)
    if repro is not None:
        doc, failed = repro[1], repro[2]
        keys = {i: r.get("key") for i, r in _rows(doc, set(ids)).items()}
        out.update(state="reproduced", reproduced_run=doc.get("run_id"), failing_variants=failed)
        for cand in case_map._reports(ws, title, cand_sha) if cand_sha else []:
            rows = _rows(cand, set(ids))
            if (_before(cand, as_of) and len(rows) == len(ids) and all(r.get("status") == "PASS" for r in rows.values())
                    and all(keys.get(i) in (None, r.get("key")) for i, r in rows.items())):
                out.update(state="fixed", fixed_sha=cand_sha, fixed_run=cand.get("run_id"))
                break
        return out
    # 4.3 P24: the bad SHA's own formal final runs passed every variant — the lock's exit.
    need = max(1, int(unrepro_runs))
    recorded = [d for d in passed if _reuse_session(d) is not None]
    reuse_off = [d for d in passed if _reuse_session(d) is False]
    out.update(pass_runs=len(passed), required_runs=need,
               session_reuse_recorded=bool(recorded), session_reuse_off=bool(reuse_off))
    if len(passed) >= need and (not recorded or reuse_off):
        out.update(state="unreproduced", unreproduced_runs=len(passed),
                   unreproduced_run_ids=[d.get("run_id") for d in sorted(
                       passed, key=lambda d: d.get("finished_at") or "")])
    return out


def escalation(view: dict[str, Any], sha: str, *, release: bool = True, fix_releases: bool = True,
               online_status: str = "") -> dict[str, Any]:
    """What S1 should do about the escape lock for candidate ``sha`` (4.3 P24).

    ``action`` is ``pass`` (a proven fix), ``release`` (the online verdict did not reproduce: S1
    passes, tagged EXPLORE, hint only), ``repeat`` (the bad SHA itself, refused unchanged like a
    TLE) or ``hold`` (the 4.2.1 behaviour). ``release=False`` restores the 4.2.1 hold everywhere.
    """
    bad = str(view.get("bad_sha") or "")
    sid = view.get("submission_id")
    state = str(view.get("state") or "none")
    step = next_step(view) or "reproduce the failure locally or request an exact-SHA reviewed trial"
    metrics = {"submission_id": sid, "bad_sha": bad, "repro_state": state, "next_step": step}
    if state == "fixed" and fix_releases:
        return {"action": "pass", "code": "ESCAPE_REPRO_FIXED", "reason": "ESCAPE LOCK " + step, "metrics": metrics}
    same = bool(bad and sha and (bad.startswith(sha) or sha.startswith(bad)))
    if release and state == "unreproduced":
        runs = view.get("unreproduced_runs")
        if same:
            return {"action": "repeat", "code": "ESCAPE_SHA_REPEAT",
                    "reason": (f"{sha[:7]} failed online ({online_status or 'WA/RE'}, submission {sid}) and the "
                               f"failure did not reproduce in {runs} formal final run(s); it has never passed "
                               "online, so it cannot be resubmitted unchanged. Change the kernel, or restore a "
                               "commit that passed."),
                    "metrics": {**metrics, "unreproduced_runs": runs,
                                "next_step": "change the kernel, or restore a proven Pass SHA"}}
        return {"action": "release", "code": "ESCAPE_UNREPRODUCED",
                "reason": (f"ESCAPE LOCK released for {sha[:7]}: {bad[:7]} failed online "
                           f"({online_status or 'WA/RE'}, submission {sid}) but the failure did not reproduce in "
                           f"{runs} formal final run(s); treating the online verdict as judge-side noise, the "
                           "lock is a hint only"),
                "metrics": {**metrics, "unreproduced_runs": runs, "escape_released": True}}
    reason = ("ESCAPE_LOCK: " if state == "none" else "ESCAPE_LOCK ") + step
    return {"action": "hold", "code": "ESCAPE_LOCK_ACTIVE", "reason": reason, "metrics": metrics}


def next_step(view: dict[str, Any]) -> str:
    bad = str(view.get("bad_sha") or "")[:7]
    n = len(view.get("variants") or [])
    pending = f"awaiting reproduction: run `opsb test --level final` on {bad} ({n} wa-repro variants)"
    if view.get("required_runs"):
        pending += (f"; {int(view.get('pass_runs') or 0)}/{int(view.get('required_runs'))} formal final runs "
                    "passed every variant")
    return {
        "pending": pending,
        "reproduced": (f"reproduced, awaiting a fix: {bad} fails {len(view.get('failing_variants') or [])} of {n} "
                       f"wa-repro variants (run {str(view.get('reproduced_run'))[:8]}); the fix must pass all {n} "
                       "at level final"),
        "fixed": (f"fixed: {str(view.get('fixed_sha'))[:7]} passes all {n} wa-repro variants on the inputs "
                  f"{bad} fails {len(view.get('failing_variants') or [])} of (run {str(view.get('fixed_run'))[:8]})"),
        "unreproduced": (f"not reproduced in {int(view.get('unreproduced_runs') or 0)} formal final run(s) on "
                         f"{bad}: all {n} wa-repro variants passed each time; treat the online verdict as "
                         "judge-side noise — the escape lock is downgraded to a hint"),
    }.get(view.get("state"), "")


def lock_view(ws, title, escape: dict[str, Any], as_of=None) -> dict[str, Any] | None:
    """The reproducer part of an escape lock: state, variant IDs and the next step."""
    if not escape.get("escape_lock"):
        return None
    view = status(ws, title, escape.get("escape_submission_id"), escape.get("escape_sha"), as_of=as_of)
    if view["state"] == "none":
        return {"state": "none", "next_step": view.get("reason") or "no wa-repro variants for this receipt"}
    return {"state": view["state"], "variant_ids": view["variants"], "reproduced_run": view.get("reproduced_run"),
            "failing_variants": view.get("failing_variants"),
            "unreproduced_runs": view.get("unreproduced_runs"),
            "pass_runs": view.get("pass_runs"), "required_runs": view.get("required_runs"),
            "session_reuse_off": view.get("session_reuse_off"), "next_step": next_step(view)}


def ingest(ws, row) -> dict[str, Any] | None:
    """Receipt-ingest hook: plan and write reproducers for a new WA/RE verdict (CPU only)."""
    title = row.get("title")
    if row.get("status") not in FAILED or not title or not row.get("submission_id"):
        return None
    try:
        from .case_bench import has_reference, problem_repo

        repo = problem_repo(ws, title)
        return apply(ws, title, receipts.normalize(row), repo=repo if has_reference(repo, "HEAD") else None)
    except Exception as exc:  # noqa: BLE001 - reproducers never block a receipt from being recorded
        return {"applied": False, "reason": f"{type(exc).__name__}: {exc}"}
