"""A/B paired execution: candidate and baseline builds alternate inside one device job (4.1 route #2).

Separate runs of two SHAs see different host states (the event timer jumps between modes from
run to run), so their per-case minima differ by more than the change being measured. A paired
job prepares both builds on the CPU, queues once, and alternates the two executables: each
round visits every case in a rotated order and runs candidate (A) and baseline (B) back to
back, A first on even rounds and B first on odd ones, so the sequence reads A B A B ... and
every case's samples spread over the whole job. Round r of a case yields the pair (A_r, B_r).

Each (round, case, arm) is one broker case with its own executable; warmups run in round 0.
The broker keeps one persistent session per executable, so switching arms costs nothing.
"""

from pathlib import Path

ARMS = ("A", "B")


def schedule(indices, rounds):
    """Deterministic ``[(round, case_index, arm)]``: rotated case order, alternating arm order."""
    order = list(indices)
    steps = []
    for r in range(max(0, int(rounds))):
        shift = r % len(order) if order else 0
        visit = order[shift:] + order[:shift]
        pair = ("B", "A") if r % 2 == 1 else ("A", "B")
        for index in visit:
            steps.extend((r, index, arm) for arm in pair)
    return steps


def _paths(part, arm, kind):
    return part.get(kind if arm == "A" else "base_" + kind) or []


def broker_cases(plans, rounds, executables):
    """Broker cases for every ready plan, and ``{broker index: (plan index, arm, round)}``.

    Untimed plans run once per arm (round 0). ``executables`` maps arm to the sample binary.
    """
    timed = [p["progress"]["index"] for p in plans if p["timed"]]
    once = [p["progress"]["index"] for p in plans if not p["timed"]]
    by_index = {p["progress"]["index"]: p for p in plans}
    steps = schedule(timed, rounds) + [(0, index, arm) for index in once for arm in ARMS]
    cases, mapping = [], {}
    for number, (r, index, arm) in enumerate(steps, 1):
        plan = by_index[index]
        parts = [{"warm": [str(p) for p in _paths(part, arm, "warm")] if r == 0 else [],
                  "runs": [str(_paths(part, arm, "runs")[r])]} for part in plan["parts"]]
        cases.append({"index": number, "id": f"{plan['progress']['id']}:{arm}{r}", "timed": bool(plan["timed"]),
                      "warmup": len(parts[0]["warm"]) if parts else 0, "parts": parts,
                      "executable": str(executables[arm])})
        mapping[number] = (index, arm, r)
    return cases, mapping


def record(plan, arm, r, msg):
    """Fold one broker result of a paired step into ``plan["arms"][arm]``."""
    slot = plan.setdefault("arms", {}).setdefault(arm, {"samples": {}, "cold": [], "profile": {}, "profile_cold": []})
    if msg.get("event") == "profile":
        if "samples" in msg:
            slot["profile"][r] = sum(msg["samples"]) if msg["samples"] else None
            slot["profile_cold"].extend(msg.get("cold") or [])
        else:
            slot["profile_error"] = str(msg.get("error") or "no profile")
        return
    if msg["status"] != "PASS":
        slot.setdefault("error", {"status": msg["status"], "detail": msg.get("detail", "")})
        return
    samples = msg.get("samples") or []
    slot["samples"][r] = samples[0] if samples else None
    slot["cold"].extend(msg.get("cold") or [])


def arm_plan(plan, arm):
    """The plan of one arm in the single-SHA shape that ``verify`` and ``timing_fields`` read."""
    slot = (plan.get("arms") or {}).get(arm) or {}
    rounds = sorted(slot.get("samples", {}))
    view = dict(plan, parts=[dict(part, runs=_paths(part, arm, "runs")[:len(rounds) or 1],
                                  warm=_paths(part, arm, "warm")) for part in plan["parts"]])
    view["samples"] = [[slot["samples"][r] for r in rounds]]
    view["cold"] = [slot.get("cold") or []]
    profile = slot.get("profile") or {}
    if rounds and all(profile.get(r) for r in rounds):
        view["profile"] = {"samples": [profile[r] for r in rounds], "cold": slot.get("profile_cold") or []}
    elif slot.get("profile_error"):
        view["profile_error"] = slot["profile_error"]
    if slot.get("error"):
        view["error"] = slot["error"]
    view["rounds"] = rounds
    return view


def execute(repo, cfg, plans, update=None):
    """Candidate (A) and baseline (B) builds alternate in one broker job (case_pairs)."""
    import case_batch
    update = update or (lambda progress: None)
    ready = [plan for plan in plans if "error" not in plan]
    executables = {"A": repo / "build-casebench/sample", "B": repo.parent / "base" / "build-casebench/sample"}
    cases, mapping = broker_cases(ready, cfg["repeats"], executables)
    by_index = {plan["progress"]["index"]: plan for plan in ready}
    job = dict(case_batch.broker_job(repo, cfg, ready), cases=cases)
    job["job_timeout"] = min(1800, 2 * job["job_timeout"])
    path = case_batch.ensure_broker(cfg, Path(case_batch.__file__).with_name("device_broker.py").read_text(encoding="utf-8"),
                                    companions=case_batch._companions())
    if path is None:
        for plan in ready:
            plan["error"] = {"status": "ERROR", "detail": "paired runs need the device broker"}
        return {"wait_sec": 0.0, "hold_sec": 0.0, "acquisitions": 0, "segments": 0, "broker": False}

    def on_event(msg):
        kind = msg.get("event")
        if kind in ("case", "profile") and msg.get("index") in mapping:
            index, arm, r = mapping[msg["index"]]
            record(by_index[index], arm, r, msg)
        elif kind == "progress" and msg.get("index") in mapping:
            index, arm, r = mapping[msg["index"]]
            update(dict(by_index[index]["progress"], phase="execute", stage=f"{arm}{r}", part=msg["part"],
                        parts=msg["parts"], repeat=r + 1, repeats=cfg["repeats"], timeout_sec=cfg["timeout"]))

    done = case_batch._stream_job(path, job, on_event) or {}
    return {"wait_sec": round(done.get("wait_sec", 0.0), 3), "hold_sec": round(done.get("hold_sec", 0.0), 3),
            "acquisitions": done.get("segments", 0), "segments": done.get("segments", 0), "broker": True,
            "paired_steps": len(cases), "profiler": list(case_batch.PROFILER_EVENTS)}


def _checked(plan, cfg, compare):
    if plan.get("error"):
        return plan["error"]
    if not plan["rounds"]:
        return {"status": "NOT_RUN", "detail": "no paired round completed"}
    try:
        import case_batch
        return case_batch.verify(plan, cfg, compare)
    except (ValueError, OSError) as exc:
        return {"status": "ERROR", "detail": str(exc)}


def result(plan, cfg, compare):
    """Candidate fields as in a single run, the baseline's as ``base_*``, and the per-round pairs."""
    views = {arm: arm_plan(plan, arm) for arm in ARMS}
    cand, base = (_checked(views[arm], cfg, compare) for arm in ARMS)
    out = dict({"cold_us": []}, **cand)
    out.update({"base_" + k: v for k, v in base.items() if k in (
        "status", "detail", "device_us", "event_us", "profiler_us", "timing_source", "profiler_error")})
    if plan["timed"] and cand.get("status") == base.get("status") == "PASS":
        rounds = [r for r in views["A"]["rounds"] if r in views["B"]["rounds"]]
        slots = {arm: plan["arms"][arm] for arm in ARMS}
        pairs = {"event": [[slots["A"]["samples"][r], slots["B"]["samples"][r]] for r in rounds]}
        if all(slots[arm]["profile"].get(r) for arm in ARMS for r in rounds):
            pairs["profiler"] = [[slots["A"]["profile"][r], slots["B"]["profile"][r]] for r in rounds]
        out["pairs"] = pairs
    return out
