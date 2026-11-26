"""Cross-problem device batches and the standard-level early abort (4.1 route #3, R9)."""
import fcntl
import json
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "npu_runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import case_batch  # noqa: E402
import early_abort  # noqa: E402
from test_device_broker import (  # noqa: E402,F401
    Client, base_cfg, env, hello, ledger, make_case, make_job, prepared, start, wait_until,
)


def _queue_behind_lock(env, specs):
    """Submit jobs while the device lock is held elsewhere, then release it; return the clients."""
    path = start(env)
    clients = {}
    with env.lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        for run_id, title, prio, cases, extra in specs:
            clients[run_id] = Client(path, make_job(env, run_id, cases, title=title, prio=prio, **extra))
            clients[run_id].wait_for("queued")
        wait_until(lambda: hello(path)["queued"] == len(specs))
        fcntl.flock(holder, fcntl.LOCK_UN)
    for client in clients.values():
        client.wait_for("done", timeout=20)
    return clients


def test_following_jobs_of_other_problems_share_one_lock_hold(env):
    case = lambda i: [make_case(env, i, "us-2")]
    clients = _queue_behind_lock(env, [("a", "X", 3.0, case(1), {}), ("b", "Y", 2.0, case(2), {}),
                                        ("c", "Z", 1.5, case(3), {}), ("d", "X", 1.0, case(4), {})])
    rows = ledger(env)
    assert [r["run_id"] for r in rows] == ["a", "b", "c", "d"]  # still the WSPT order
    assert [r.get("batch") for r in rows] == [["a", "b", "c"]] * 3 + [None]  # at most three members
    assert clients["b"].of("done")[0]["batch"] == ["a", "b", "c"]
    # Accounting stays per problem: b queued while a held the device, and holds only its own time.
    assert clients["b"].of("done")[0]["wait_sec"] >= rows[0]["hold_sec"]
    for name, client in clients.items():
        own = next(r["hold_sec"] for r in rows if r["run_id"] == name)
        assert abs(client.of("done")[0]["hold_sec"] - own) < 0.002


def test_merging_stops_at_the_first_job_that_cannot_join(env):
    case = lambda i: [make_case(env, i, "us-1")]
    _queue_behind_lock(env, [("a", "X", 3.0, case(1), {}), ("b", "X", 2.0, case(2), {}),
                             ("c", "Y", 1.0, case(3), {})])
    rows = ledger(env)
    assert [(r["run_id"], r.get("batch")) for r in rows] == [("a", None), ("b", ["b", "c"]), ("c", ["b", "c"])]


def test_switched_off_head_runs_alone(env):
    case = lambda i: [make_case(env, i, "us-1")]
    _queue_behind_lock(env, [("a", "X", 3.0, case(1), {"merge": False}), ("b", "Y", 2.0, case(2), {})])
    assert [r.get("batch") for r in ledger(env)] == [None, None]


def test_member_timeout_requeues_only_that_member(env):
    slow = [make_case(env, 1, "us-1"), make_case(env, 2, "hang"), make_case(env, 3, "us-1")]
    clients = _queue_behind_lock(env, [("a", "X", 3.0, slow, {"case_timeout": 1}),
                                       ("b", "Y", 2.0, [make_case(env, 4, "us-3")], {})])
    rows = ledger(env)
    assert [(r["run_id"], r["segment"]) for r in rows] == [("a", 1), ("b", 1), ("a", 2)]
    assert rows[0]["batch"] == rows[1]["batch"] == ["a", "b"]
    assert [c["status"] for c in clients["a"].of("case")] == ["PASS", "TLE", "PASS"]
    assert [c["status"] for c in clients["b"].of("case")] == ["PASS"]
    assert clients["a"].of("done")[0]["segments"] == 2 and clients["b"].of("done")[0]["segments"] == 1


def test_watch_needs_every_counted_case_slower_and_ignores_tiny_baselines():
    watch = early_abort.Watch({"baseline_us": {"01": 300, "02": 400, "03": 10, "04": 500, "05": 600},
                               "factor": 1.25, "cases": 3, "min_us": 200, "base_sha": "b" * 40})
    assert not watch.observe("01", [500.0, 480.0])
    assert not watch.observe("03", [90.0])  # a 10 us baseline is below the event timer's noise
    assert not watch.observe("02", [700.0])
    assert watch.observe("04", [900.0])
    assert watch.aborted["after_cases"] == ["01", "02", "04"] and "1.25x" in watch.skip_detail()
    calm = early_abort.Watch({"baseline_us": {"01": 300, "02": 300, "03": 300}})
    assert not any(calm.observe(c, [310.0]) for c in ("01", "02", "03"))
    assert not early_abort.Watch(None).active


def test_standard_run_stops_early_and_frees_the_device(env, monkeypatch):
    selected = [{"id": f"0{i}", "timed": True} for i in range(1, 6)]
    repo, plans = prepared(env, monkeypatch, "us-400,us-450,us-500,us-400,us-400", selected)
    cfg = base_cfg(env, repeats=1, warmup=0, early_abort={
        "baseline_us": {f"0{i}": 200.0 for i in range(1, 6)}, "factor": 1.25, "cases": 3, "min_us": 100,
        "base_sha": "b" * 40})
    start(env)
    result = case_batch.execute_batch(repo, cfg, plans)
    assert result["aborted_early"]["after_cases"] == ["01", "02", "03"]
    assert [p.get("error", {}).get("status") for p in plans] == [None, None, None, "SKIP", "SKIP"]
    assert "aborted early" in plans[4]["error"]["detail"]

    def lock_free():
        with env.lock.open("a") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

    wait_until(lock_free)
    executed = [line for line in env.log.read_text().splitlines() if line.startswith("exec")]
    assert not any("/work/5/" in line for line in executed)  # the cancelled job stopped before the rest
