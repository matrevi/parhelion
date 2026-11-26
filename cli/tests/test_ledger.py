"""4.2 C (P05): ledger core (store, model, views), platform auto-writes, checkpoint tail, handoff checks.

Everything runs on temporary directories and fake adapters; no model API, daemon or real contest data.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.ledger import auto, checkpoint, gitref, handoff, model, rows as builders, store, summary, view  # noqa: E402

DIGEST = {
    "level": "standard", "suite_version": 3, "formal": True,
    "summary": {"total": 15, "passed": 15, "counts": {"PASS": 15}}, "failures": [],
    "timing": {"eligible": True, "source": "event_warm", "drift": 1.01, "noisy": False, "warmup": 2, "repeats": 5,
               "cases": {"01": {"min_us": 9.2, "median_us": 9.4, "cold_us": 558.8},
                         "02": {"min_us": 4.1, "median_us": 4.3, "cold_us": 300.0}}},
    "baseline": {"sha": "1111111", "run_id": "r0", "geomean_speedup": 1.07, "cases": {"01": 1.12, "02": 1.02}},
    "hint": "",
}
WORKER = "dee-aaaaaa"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def contest(tmp_path):
    """A contest workspace with one problem repository ``Op00`` and two commits."""
    ws = (tmp_path / "contest").resolve()
    repo = ws / "Op00"
    (repo / "src").mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        _git(repo, *args)
    first = _commit(repo, "src/kernel.cpp", "int main() { return 0; }\n", "baseline kernel")
    second = _commit(repo, "src/kernel.cpp", "int main() { return 1; }\n", "Tile the inner loop by 64")
    (ws / "opsb.workspace.json").write_text(json.dumps({
        "layout": "clean-agents-first", "problems": [{"title": "Op00", "path": "Op00", "difficulty": 2}]}))
    return {"ws": ws, "repo": repo, "first": first, "second": second}


def _record(sha: str, rid: str = "a" * 32, agent: str | None = WORKER, **over) -> dict:
    return {"request_id": rid, "problem": "Op00", "sha": sha, "level": "standard", "status": "finished",
            "digest": json.loads(json.dumps(DIGEST)), "agent_id": agent, "error": None, **over}


# ---------------------------------------------------------------- store, model, views

def test_append_numbers_entries_and_bounds_lines(tmp_path):
    led = store.Ledger(tmp_path, "Op00", WORKER)
    first = led.append("insight", {"claim": "tail tile of case 05 is slow", "cases": ["05"]}, source="worker")
    second = led.append("failure", {"claim": "double buffering did not help", "approach": "double-buffer A",
                                    "why_failed": "w" * 900, "retry_conditions": "r" * 400}, source="worker")
    assert (first["id"], second["id"]) == ("L-0001", "L-0002")
    assert first["source"] == "worker" and first["confidence"] == "inferred" and first["problem"] == "Op00"
    raw = led.path.read_bytes().splitlines()
    assert len(raw) == 2 and all(len(line) <= model.LINE_LIMIT for line in raw)
    stored = json.loads(raw[1])
    assert stored["truncated"] and "why_failed" in stored["truncated"] and stored["claim"].startswith("double buffering")
    assert len(model.clip("x" * 500, model.CLAIM_MAX)) == model.CLAIM_MAX
    with pytest.raises(model.LedgerError):
        led.append("insight", {"claim": ""}, source="worker")
    with pytest.raises(model.LedgerError):
        led.append("bogus", {"claim": "x"})
    for bad in ("../x", "a/b", "", ".hidden"):
        with pytest.raises(model.LedgerError):
            store.Ledger(tmp_path, "Op00", bad)


def test_platform_fields_are_not_editable_by_a_model(tmp_path):
    led = store.Ledger(tmp_path, "Op00", WORKER)
    note = led.append("hypothesis", {"claim": "try swizzle", "confidence": "measured", "diff_hash": "deadbeef",
                                     "outcome": "better", "source": "platform", "id": "L-9999", "priority": 2},
                      source="worker")
    assert note["source"] == "worker" and note["confidence"] == "inferred"
    assert "diff_hash" not in note and "outcome" not in note and note["id"] == "L-0001"
    with pytest.raises(model.LedgerError):
        led.append("measurement", {"key": "x", "value": 1}, source="worker")
    assert model.worker_fields({"claim": "c", "diff_hash": "h", "ekey": "k", "receipt_id": "r"}) == {"claim": "c"}
    plat = led.append("experiment", {"claim": "abc1234 standard: better", "sha": "abc1234", "outcome": "better",
                                     "diff_hash": "0123456789abcdef"})
    assert plat["confidence"] == "measured" and plat["source"] == "platform"
    moved = led.supersede(plat["id"], {"lesson": "tile helps", "outcome": "worse", "confidence": "measured"},
                          source="worker")
    assert moved["outcome"] == "better" and moved["diff_hash"] == "0123456789abcdef"  # platform fields survive
    assert moved["lesson"] == "tile helps" and moved["source"] == "worker"
    assert moved["confidence"] == "inferred"  # a model cannot claim ``measured``
    plat2 = led.append("experiment", {"claim": "def5678 standard: worse", "sha": "def5678", "outcome": "worse"})
    kept = led.supersede(plat2["id"], {"lesson": "second thought"}, source="worker")
    assert kept["confidence"] == "measured" and kept["outcome"] == "worse"  # untouched fields keep their values
    meas = led.append("measurement", {"key": "k", "value": 1})
    with pytest.raises(model.LedgerError):
        led.supersede(meas["id"], {"claim": "edited"}, source="worker")


def test_supersede_folds_and_old_ids_keep_resolving(tmp_path):
    led = store.Ledger(tmp_path, "Op00", WORKER)
    hyp = led.append("hypothesis", {"claim": "reorder the load loop", "sha": "abc1234"}, source="worker")
    new = led.supersede(hyp["id"], {"status": "testing"}, source="worker")
    newer = led.supersede(new["id"], {"status": "confirmed", "resolution_note": "gain +2"}, source="worker")
    rows = led.rows()
    assert [r["id"] for r in view.current(rows)] == [newer["id"]]
    assert newer["supersedes"] == [new["id"]] and newer["claim"] == "reorder the load loop"
    assert view.head(rows, hyp["id"])["id"] == newer["id"] and view.head(rows, "L-0099") is None
    assert view.chain_ids(rows, newer) == {hyp["id"], new["id"], newer["id"]}
    assert view.open_hypotheses(rows) == []
    with pytest.raises(model.LedgerError):
        led.supersede("L-0099", {"status": "open"})
    assert len(led.path.read_bytes().splitlines()) == 3  # history stays on disk


def test_views_open_unexploited_failures_case_since_and_problem_merge(tmp_path):
    a, b = store.Ledger(tmp_path, "Op00", "w-a"), store.Ledger(tmp_path, "Op00", "w-b")
    h1 = a.append("hypothesis", {"claim": "low priority idea", "priority": 5}, source="worker")
    h2 = a.append("hypothesis", {"claim": "urgent idea", "priority": 1, "status": "queued"}, source="worker")
    done = a.append("hypothesis", {"claim": "done idea", "status": "refuted"}, source="worker")
    i1 = a.append("insight", {"claim": "case 05 tail tile", "cases": ["05"], "actionable_next": "swap tile order"},
                  source="worker")
    i2 = a.append("insight", {"claim": "case 03 is DMA bound", "cases": ["03"]}, source="worker")
    a.append("experiment", {"claim": "swap tile order on 05", "links": [i1["id"]], "hypothesis_id": h2["id"],
                            "per_case_us": {"05": 12.0}, "outcome": "better"})
    f1 = a.append("failure", {"claim": "first dead end", "approach": "A"}, source="worker")
    f2 = b.append("failure", {"claim": "second dead end", "approach": "B"}, source="worker")
    mark = b.append("insight", {"claim": "teammate insight", "cases": ["05"]}, source="worker")
    rows = store.read_problem(tmp_path, "Op00")
    assert [r["id"] for r in view.query(rows, "open") if r["worker"] == "w-a"] == [h2["id"], h1["id"]]
    assert {r["id"] for r in view.query(rows, "unexploited")} == {i2["id"], mark["id"]}  # i1 is cited by an experiment
    assert [r["claim"] for r in view.query(rows, "failures")] == ["second dead end", "first dead end"]
    assert {r["claim"] for r in view.query(rows, "case:05")} == {
        "case 05 tail tile", "swap tile order on 05", "teammate insight"}
    later = view.query(rows, f"since:w-a/{f1['id']}")
    assert [r["id"] for r in later] == [f2["id"], mark["id"]] and view.ref(later[0]) == f"w-b/{f2['id']}"
    assert done["id"] not in {r["id"] for r in view.open_hypotheses(rows)}
    with pytest.raises(model.LedgerError):
        view.query(rows, "nonsense")


def test_flock_serializes_threads_and_processes(tmp_path):
    def burst(n: int) -> list[str]:
        led = store.Ledger(tmp_path, "Op00", WORKER)
        return [led.append("insight", {"claim": f"insight {i}"}, source="worker")["id"] for i in range(n)]

    script = ("import sys; sys.path.insert(0, %r); from opsb_core.ledger import store; "
              "led = store.Ledger(%r, 'Op00', %r); "
              "[led.append('insight', {'claim': 'from process %%d' %% i}, source='worker') for i in range(25)]"
              % (str(CLI_DIR), str(tmp_path), WORKER))
    proc = subprocess.Popen([sys.executable, "-c", script])
    with ThreadPoolExecutor(8) as pool:
        ids = [i for batch in pool.map(burst, [20] * 8) for i in batch]
    assert proc.wait(30) == 0
    raw = (tmp_path / "results/ledger/Op00" / f"{WORKER}.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in raw]
    assert len(rows) == 185 and len(set(ids)) == 160
    assert sorted(model.seq_of(r["id"]) for r in rows) == list(range(1, 186))  # no gap, no repeat


# ---------------------------------------------------------------- platform writes

def test_outcome_classification():
    def digest(**over):
        d = json.loads(json.dumps(DIGEST))
        for key, value in over.items():
            d[key].update(value) if isinstance(value, dict) else d.__setitem__(key, value)
        return d

    def run(**kw):
        return builders.outcome_of({"status": "finished", "level": "standard", "digest": kw.pop("digest", digest()), **kw})

    assert run() == "better"
    assert run(digest=digest(baseline={"geomean_speedup": 0.95})) == "worse"
    assert run(digest=digest(baseline={"geomean_speedup": 1.0})) == "neutral"
    assert run(digest=digest(baseline={"geomean_speedup": None})) == "neutral"
    assert run(digest=digest(baseline={"geomean_speedup": 1.05}, timing={"noisy": True})) == "noise"
    assert run(digest=digest(baseline={"geomean_speedup": 1.4}, timing={"noisy": True})) == "better"
    assert run(digest=digest(summary={"counts": {"PASS": 14, "WA": 1}})) == "WA"
    assert run(digest=digest(summary={"counts": {"PASS": 14, "RE": 1}})) == "RE"
    assert run(digest=digest(summary={"counts": {"PASS": 14, "TLE": 1}})) == "TLE"
    assert run(digest=digest(summary={"counts": {"PASS": 13, "TLE": 1, "WA": 1}})) == "WA"
    assert builders.outcome_of({"status": "failed", "level": "build", "error": "x"}) == "compile_fail"
    assert builders.outcome_of({"status": "failed", "level": "standard", "error": "ccec: error: compile failed"}) == "compile_fail"
    assert builders.outcome_of({"status": "failed", "level": "standard", "error": "ssh refused"}) is None
    assert builders.outcome_of({"status": "cancelled", "digest": DIGEST}) is None


def test_settled_test_writes_measurement_and_experiment_once(contest):
    ws, sha = contest["ws"], contest["second"]
    hyp = auto.on_gate_request(ws, "Op00", sha, WORKER, note="Tile the inner loop by 64", enqueued=False)
    written = auto.on_test_settled(ws, _record(sha))
    meas, exp = written
    assert (meas["type"], exp["type"]) == ("measurement", "experiment")
    assert meas["per_case_us"] == {"01": 9.2, "02": 4.1} and meas["median_us"]["01"] == 9.4
    assert meas["test_level"] == "standard" and meas["n_repeats"] == 5 and meas["suite_version"] == 3
    assert meas["noise_band"] == {"drift": 1.01, "noisy": False} and meas["test_run_id"] == "a" * 32
    assert exp["outcome"] == "better" and exp["sha"] == sha and exp["base_sha"] == "1111111"
    assert exp["delta_vs_baseline"] == {"geomean_pct": -6.5, "cases": {"01": -10.7, "02": -2.0}}
    assert exp["hypothesis_id"] == hyp["id"] and exp["change_summary"] == "Tile the inner loop by 64"
    assert exp["diff_hash"] == gitref.diff_hash(contest["repo"], sha) != ""
    assert exp["actual_outcome"]["speedup"] == 1.07 and exp["confidence"] == "measured"
    assert exp["source"] == "platform" and exp["submitted"] is False
    path = ws / "results/ledger/Op00" / f"{WORKER}.jsonl"
    before = path.read_bytes()
    assert auto.on_test_settled(ws, _record(sha)) == []           # replay: the same request id
    assert path.read_bytes() == before
    again = auto.on_test_settled(ws, _record(sha, rid="b" * 32))  # a new run of the same SHA is a new row
    assert [r["type"] for r in again] == ["measurement", "experiment"]
    assert auto.on_test_settled(ws, _record(sha, rid="c" * 32, status="running")) == []
    other = auto.on_test_settled(ws, _record(sha, rid="d" * 32, agent=None))
    assert other[0]["worker"] == auto.PLATFORM_WORKER  # no owning worker: the platform's own ledger


def test_failed_run_is_recorded_only_when_it_did_not_compile(contest):
    ws, sha = contest["ws"], contest["second"]
    crash = _record(sha, rid="e" * 32, status="failed", digest=None, error="ssh: connection refused")
    assert auto.on_test_settled(ws, crash) == []
    broken = _record(sha, rid="f" * 32, status="failed", digest=None, error="kernel.cpp:3: error: compile failed")
    (exp,) = auto.on_test_settled(ws, broken)
    assert exp["outcome"] == "compile_fail" and "compile failed" in exp["why"]
    wa = _record(sha, rid="1" * 32)
    wa["digest"]["summary"]["counts"] = {"PASS": 14, "WA": 1}
    wa["digest"]["failures"] = [{"id": "07", "status": "WA", "detail": "out0: mismatch at 12"}]
    meas, wa_exp = auto.on_test_settled(ws, wa)
    assert wa_exp["outcome"] == "WA" and wa_exp["scope"] == {"cases": ["07"]} and "mismatch at 12" in wa_exp["why"]


def test_finalize_hook_and_delivery_backstop_write_once(contest, tmp_path):
    from opsb_core.services import test_runs
    ws, sha = contest["ws"], contest["second"]
    rid = "9" * 32
    log = tmp_path / "run.log"
    log.write_text("compiling\n" + json.dumps({"run_id": "r1", "exit_code": 0, "level": "standard",
                                                "summary": DIGEST["summary"], "digest": DIGEST}) + "\n")
    record = {"request_id": rid, "problem": "Op00", "sha": sha, "level": "standard", "cases": [], "build_only": False,
              "agent_id": WORKER, "role": "worker", "created_at": test_runs._now_iso(), "pid": None, "log": str(log),
              "status": "running", "run_id": None, "exit_code": None, "digest": None, "report": None,
              "finished_at": None, "observed": False, "delivered": False}
    test_runs._write(ws, record)
    settled = test_runs._finalize(ws, record, returncode=0)
    assert settled["status"] == "finished"
    path = ws / "results/ledger/Op00" / f"{WORKER}.jsonl"
    first = path.read_bytes()
    assert len(first.splitlines()) == 2
    test_runs._finalize(ws, record, returncode=0)  # already terminal: nothing happens
    auto.on_test_settled(ws, test_runs._load(ws, rid))  # the delivery backstop
    assert path.read_bytes() == first

    from opsb_core.agents.manager import AgentMeshManager
    mgr = AgentMeshManager()
    try:
        mgr._store(ws).create_agent({"agent_id": WORKER, "driver": "deepseek", "problem": "Op00",
                                     "worktree_mode": "readonly"})
        assert mgr.deliver_test_results(ws) == 1
        assert path.read_bytes() == first
    finally:
        mgr.stop_all()


def test_gate_request_creates_a_platform_hypothesis_once(contest):
    ws, sha = contest["ws"], contest["second"]
    estimate = {"delta_expected": 3.1, "confidence_sigma": 1.4}
    hyp = auto.on_gate_request(ws, "Op00", sha, WORKER, note="Swap the tile order on case 05", estimate=estimate,
                               enqueued=True)
    assert hyp["type"] == "hypothesis" and hyp["source"] == "platform" and hyp["confidence"] == "inferred"
    assert hyp["claim"] == "Swap the tile order on case 05" and hyp["status"] == "queued"
    assert hyp["predicted_effect"] == {"delta_mu": 3.1, "delta_sigma": 1.4} and hyp["sha"] == sha
    assert auto.on_gate_request(ws, "Op00", sha, WORKER, note="again", estimate=estimate, enqueued=True) is None
    first = contest["first"]
    held = auto.on_gate_request(ws, "Op00", first, WORKER, note="", estimate={}, enqueued=False)
    assert held["claim"] == "baseline kernel" and held["status"] == "open" and "predicted_effect" not in held
    assert auto.on_gate_request(ws, "Op00", "HEAD", WORKER) is None  # not a SHA
    # a hypothesis the worker wrote for the SHA is respected
    led = store.Ledger(ws, "Op00", "dee-bbbbbb")
    led.append("hypothesis", {"claim": "mine", "sha": sha}, source="worker")
    assert auto.on_gate_request(ws, "Op00", sha, "dee-bbbbbb", note="x", enqueued=True) is None


def test_submit_gate_handler_writes_on_requests_not_previews(contest):
    from opsb_core.mcp import handlers_bench
    ws, sha = contest["ws"], contest["second"]
    verdict = {"approved": True, "priority_score": 1.0, "stages": [
        {"stage": "SAFETY", "passed": True, "reason": ""}, {"stage": "NOISE_FLOOR", "passed": True, "reason": ""},
        {"stage": "CIRCUIT_BREAKER", "passed": True, "reason": ""}, {"stage": "QUOTA_RESERVE", "passed": True, "reason": ""}],
        "estimate": {"estimated_score": 75.0, "current_score": 72.0, "delta_expected": 3.0, "confidence_sigma": 1.0}}
    args = {"problem": "Op00", "sha": sha, "note": "Tile by 64", "_caller_agent_id": WORKER}
    with mock.patch.object(handlers_bench, "invoke_bridge", return_value=verdict):
        handlers_bench.tool_parhelion_submit_gate(args, ws)  # preview
        assert not (ws / "results/ledger").exists()
    with mock.patch.object(handlers_bench, "invoke_bridge", return_value={"enqueued": True, "verdict": verdict}):
        out = handlers_bench.tool_parhelion_submit_gate({**args, "enqueue": True}, ws)
    assert out["enqueued"] is True
    (hyp,) = [r for r in store.Ledger(ws, "Op00", WORKER).rows() if r["type"] == "hypothesis"]
    assert hyp["status"] == "queued" and hyp["predicted_effect"] == {"delta_mu": 3.0, "delta_sigma": 1.0}


def _receipt(sid: str, sha: str, status: str = "Pass", score=72.0, **over) -> dict:
    return {"submission_id": sid, "title": "Op00", "sha": sha, "status": status, "score": score if status == "Pass" else 0,
            "submitted_at": "2026-01-02T03:00:00+00:00", "finished_at": "2026-01-02T03:05:00+00:00", **over}


@pytest.mark.parametrize("gain,expected", [(2.5, "confirmed"), (2.0, "confirmed"), (1.5, "inconclusive"),
                                           (0.0, "refuted"), (-1.0, "refuted")])
def test_receipt_resolves_the_hypothesis_by_prediction(contest, gain, expected):
    ws, sha = contest["ws"], contest["second"]
    auto.on_gate_request(ws, "Op00", sha, WORKER, note="Tile", enqueued=True,
                         estimate={"delta_expected": 3.0, "confidence_sigma": 1.0})
    auto.on_test_settled(ws, _record(sha))
    result = auto.on_receipt(ws, "Op00", _receipt("s1", sha), WORKER, gain=gain, prior=70.0)
    assert result["new"] and [h["status"] for h in result["hypotheses"]] == [expected]
    rows = store.Ledger(ws, "Op00", WORKER).rows()
    (hyp,) = [r for r in view.current(rows) if r["type"] == "hypothesis"]
    assert hyp["status"] == expected and hyp["resolution_note"] and hyp["resolved_by"].startswith("L-")
    (exp,) = [r for r in view.current(rows) if r["type"] == "experiment"]
    assert exp["receipt_id"] == "s1" and exp["submitted"] is True and exp["actual_outcome"]["official_score"] == 72.0
    meas = [r for r in rows if r["type"] == "measurement" and r.get("receipt_id") == "s1"]
    assert len(meas) == 1 and meas[0]["value"] == 72.0 and meas[0]["source"] == "platform"


def test_receipt_failures_refute_and_unknown_gain_waits(contest):
    ws, sha = contest["ws"], contest["second"]
    auto.on_gate_request(ws, "Op00", sha, WORKER, note="Tile", enqueued=True,
                         estimate={"delta_expected": 3.0, "confidence_sigma": 1.0})
    unknown = auto.on_receipt(ws, "Op00", _receipt("s1", sha), WORKER, gain=None)  # a Pass without a gain yet
    assert unknown["hypotheses"] == []
    led = store.Ledger(ws, "Op00", WORKER)
    assert [h["status"] for h in view.open_hypotheses(led.rows())] == ["queued"]
    assert auto.on_receipt(ws, "Op00", _receipt("s2", sha, status="Pass", score=None, official_score=None), WORKER) is None
    wa = auto.on_receipt(ws, "Op00", _receipt("s3", sha, status="Wrong Answer"), WORKER, gain=-72.0, prior=72.0)
    assert wa["hypotheses"][0]["status"] == "refuted"
    (hyp,) = [r for r in view.current(led.rows()) if r["type"] == "hypothesis"]
    assert hyp["resolution_note"] == "official verdict Wrong Answer"
    assert not any(r["type"] == "experiment" for r in led.rows())  # no local run: nothing to update


def test_receipt_replay_does_not_write_again(contest):
    ws, sha = contest["ws"], contest["second"]
    auto.on_gate_request(ws, "Op00", sha, WORKER, note="Tile", enqueued=True,
                         estimate={"delta_expected": 3.0, "confidence_sigma": 1.0})
    auto.on_test_settled(ws, _record(sha))
    auto.on_receipt(ws, "Op00", _receipt("s1", sha), WORKER, gain=2.5, prior=70.0)
    path = ws / "results/ledger/Op00" / f"{WORKER}.jsonl"
    before = path.read_bytes()
    for _ in range(3):
        replay = auto.on_receipt(ws, "Op00", _receipt("s1", sha), WORKER, gain=2.5, prior=70.0)
        assert replay["new"] is False and replay["hypotheses"] == [] and replay["experiments"] == []
    assert path.read_bytes() == before
    wa = auto.on_receipt(ws, "Op00", _receipt("s4", sha, status="Wrong Answer"), WORKER, gain=-72.0)
    exp = [r for r in view.current(store.Ledger(ws, "Op00", WORKER).rows()) if r["type"] == "experiment"]
    assert wa["new"] and exp[0]["receipt_id"] == "s4" and exp[0]["outcome"] == "WA"  # the verdict replaces the local outcome


def test_deliver_judgements_writes_the_ledger_once(contest):
    from opsb_core.agents.manager import AgentMeshManager
    ws, sha = contest["ws"], contest["second"]
    auto.on_gate_request(ws, "Op00", sha, WORKER, note="Tile", enqueued=True,
                         estimate={"delta_expected": 3.0, "confidence_sigma": 1.0})
    (ws / "results").mkdir(exist_ok=True)
    rows = [_receipt("s1", sha, agent_id=WORKER, score=70.0),
            _receipt("s2", sha, agent_id=WORKER, score=74.0, submitted_at="2026-01-03T03:00:00+00:00",
                     finished_at="2026-01-03T03:05:00+00:00")]
    outcome = ws / "results/submit-queue-outcome.jsonl"
    outcome.write_text("".join(json.dumps(r) + "\n" for r in rows))
    mgr = AgentMeshManager()
    try:
        mgr._store(ws).create_agent({"agent_id": WORKER, "driver": "deepseek", "problem": "Op00",
                                     "worktree_mode": "readonly"})
        mgr.deliver_judgements(ws, 5.0)
        path = ws / "results/ledger/Op00" / f"{WORKER}.jsonl"
        led_rows = store.Ledger(ws, "Op00", WORKER).rows()
        values = sorted(r["value"] for r in led_rows if r.get("receipt_id"))
        assert values == [70.0, 74.0]
        (hyp,) = [r for r in view.current(led_rows) if r["type"] == "hypothesis"]
        assert hyp["status"] == "confirmed"  # s1: gain 70 (no prior) is above the prediction
        first = path.read_bytes()
        mgr._feedback_versions.clear()
        mgr._ledger_receipts.clear()
        mgr.deliver_judgements(ws, 5.0)  # the whole batch again, as after a daemon restart
        assert path.read_bytes() == first
    finally:
        mgr.stop_all()


def test_epoch_gains_use_the_breakthrough_arithmetic():
    from opsb_core.agents.judge_feedback import epoch_gains
    rows = [_receipt("s1", "a" * 40, score=70.0),
            _receipt("s2", "b" * 40, score=74.0, submitted_at="2026-01-02T05:00:00+00:00",
                     finished_at="2026-01-02T05:05:00+00:00"),
            _receipt("s3", "c" * 40, status="Wrong Answer", submitted_at="2026-01-02T06:00:00+00:00"),
            _receipt("s4", "d" * 40, score=None, official_score=None, submitted_at="2026-01-02T07:00:00+00:00")]
    gains = epoch_gains(rows, 1.0)
    assert gains["s1"] == (70.0, 0.0) and set(gains) == {"s1", "s2", "s3"}  # an unscored Pass has no gain yet
    gain, prior = gains["s2"]
    assert 69.8 < prior <= 70.0 and abs(gain - (74.0 - prior)) < 0.02
    assert gains["s3"][0] < 0  # a failed verdict scores 0 against the best so far
    assert epoch_gains(rows, 0.9)["s2"][0] == round(74.0 * 0.9 - epoch_gains(rows, 0.9)["s2"][1], 2)
