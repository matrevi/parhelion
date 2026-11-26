"""WA/RE reproducers (4.1 #6): planning, idempotent suite writes, lock states and the S1 decision."""
from __future__ import annotations

import json
import pathlib
import sys
import types
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "cli")):
    if path not in sys.path:
        sys.path.insert(0, path)

from scheduler import suite as suites  # noqa: E402
from scheduler import wa_repro, wa_ties  # noqa: E402

BAD, FIX, OTHER = "b" * 40, "f" * 40, "c" * 40
SID = "6b07b8de85ad43bbe0ff92a5"
TIE_VALUES = [12.7, 0.05, 0.15, 0.25]
OFFICIAL = [
    {"id": "01", "seed": 0, "min_us": 5.0, "params": {"shape": [4, 4], "mode": 0, "qmin": -128, "qmax": 127, "gen": "basic"}},
    {"id": "02", "seed": 1, "min_us": 50.0, "params": {"shape": [64, 64], "mode": 0, "qmin": -128, "qmax": 127, "gen": "basic"}},
    {"id": "03", "seed": 2, "min_us": 900.0, "params": {"shape": [512, 512], "mode": 0, "qmin": -128, "qmax": 127, "gen": "huge"}},
    {"id": "04", "seed": 3, "min_us": 40.0, "params": {"shape": [64, 64], "mode": 1, "qmin": -128, "qmax": 127, "gen": "positive"}},
    {"id": "11", "seed": 10, "min_us": 4.0, "params": {"shape": [4, 1], "mode": 0, "qmin": -128, "qmax": 127, "gen": "tie",
                                                     "values": TIE_VALUES}},
]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PARHELION_MCP_ROLE", raising=False)
    root = tmp_path / "ws"
    (root / "results").mkdir(parents=True)
    _report(root, OTHER, "r0", {c["id"]: "PASS" for c in OFFICIAL}, "2026-11-26T01:00:00+00:00")
    return root


def _report(ws, sha, run, statuses, finished, keys=None):
    path = ws / "results" / "Op" / sha / "bench-runs" / run / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    official = {c["id"]: c for c in OFFICIAL}
    cases = [{**official.get(cid, {}), "id": cid, "source": "official" if cid in official else "local",
              "status": status, "key": (keys or {}).get(cid, f"key-{cid}")} for cid, status in statuses.items()]
    path.write_text(json.dumps({"formal": True, "verified": True, "exit_code": 0, "sha": sha, "run_id": run,
                                "suite_version": 1, "finished_at": finished, "level": "final", "cases": cases}))


def _receipt(**extra):
    return {"title": "Op", "sha": BAD, "status": "Wrong Answer", "submission_id": SID, "score": 0,
            "submitted_at": "2026-11-26T02:00:00+00:00", **extra}


LIKE = {"shape": [128, 256], "mode": 0}


def test_plan_is_deterministic_and_seeds_come_from_the_receipt(ws):
    one = wa_repro.plan_variants(ws, "Op", _receipt(), LIKE)
    two = wa_repro.plan_variants(ws, "Op", _receipt(), LIKE)
    assert one == two
    # Nearest in element count with mode 0 and no explicit values: 02 (4096) and 03 (262144), 8x either side.
    assert [b["local"] for b in one["bases"]] == ["02", "03"] and not any(b["mapped"] for b in one["bases"])
    seeds = one["variants"]
    assert len(seeds) == wa_repro.SEEDS and len({v["seed"] for v in seeds}) == wa_repro.SEEDS
    assert seeds[0]["id"] == "local/wa-e0ff92a5-00" and seeds[0]["seed"] == wa_repro.derived_seed(SID, 0)
    assert {v["base"] for v in seeds} == {"02", "03"} and all(v["params"] == {"shape": [128, 256]} for v in seeds)
    assert all(v["tier"] == "final" and v["origin"] == "wa-repro" and v["receipt"] == SID for v in seeds)
    other = wa_repro.plan_variants(ws, "Op", _receipt(submission_id="6b07b50f85ad43bbe0f68ef6"), LIKE)
    assert {v["seed"] for v in other["variants"]}.isdisjoint({v["seed"] for v in seeds})
    # Exact ties need the explicit-value case; its variants keep the oracle's own generator.
    assert one["tie_base"] == "11" and len(one["ties"]) == len(wa_ties.TIE_SHAPES)
    tie = one["ties"][0]["params"]
    assert len(tie["values"]) == tie["shape"][0] * tie["shape"][1]


def test_without_a_failing_case_or_hint_nothing_is_guessed(ws):
    plan = wa_repro.plan_variants(ws, "Op", _receipt())
    assert plan["variants"] == [] and plan["ties"] == [] and "--like" in plan["reason"]
    result = wa_repro.apply(ws, "Op", _receipt())
    assert result["applied"] is False and not suites.suite_path(ws, "Op").exists()
    assert wa_repro.load_manifest(ws, "Op")["receipts"][SID]["variants"] == []


def test_unmapped_failing_case_falls_back_to_nearest_time(ws):
    receipt = _receipt(case_results=[{"testcase_id": "j7", "status": "Pass", "time_us": 3.0, "tbest_us": 2.0},
                                     {"testcase_id": "j9", "status": "Wrong Answer", "time_us": None, "tbest_us": 45.0}])
    plan = wa_repro.plan_variants(ws, "Op", receipt)
    # No case map exists: the official cases nearest 45 us are 02 (50 us) and 04 (40 us).
    assert sorted(b["local"] for b in plan["bases"]) == ["02", "04"]
    assert all(b["judge"] == "j9" and b["mapped"] is False for b in plan["bases"])
    assert all(v["judge_testcase_id"] == "j9" and "params" not in v for v in plan["variants"])


def test_mapped_failing_case_uses_the_case_map(ws):
    doc = {"method": "roster", "pairs": [{"local": "03", "judge": "j9", "judge_mapped": True}]}
    receipt = _receipt(case_results=[{"testcase_id": "j9", "status": "Runtime Error", "time_us": None, "tbest_us": None}])
    with mock.patch("scheduler.case_map.current", return_value=doc):
        plan = wa_repro.plan_variants(ws, "Op", receipt)
    assert plan["bases"] == [{"local": "03", "judge": "j9", "mapped": True, "reason": "roster case map"}]
    assert {v["base"] for v in plan["variants"]} == {"03"} and all(v["mapped"] for v in plan["variants"])


def test_apply_is_idempotent_and_shares_the_large_tie_inputs(ws):
    first = wa_repro.apply(ws, "Op", _receipt(), like=LIKE)
    suite = suites.load_suite(ws, "Op")
    assert first["applied"] and len(first["added"]) == wa_repro.SEEDS + len(wa_ties.TIE_SHAPES)
    version = suite["suite_version"]
    assert version == 1  # one bump for the whole batch
    again = wa_repro.apply(ws, "Op", _receipt(), like=LIKE)
    assert again["applied"] is False and again["reason"] == "already planned"
    assert suites.load_suite(ws, "Op")["suite_version"] == version
    second = wa_repro.apply(ws, "Op", _receipt(submission_id="6b07b50f85ad43bbe0f68ef6"), like=LIKE)
    assert len(second["added"]) == wa_repro.SEEDS and len(second["entry"]["shared"]) == len(wa_ties.TIE_SHAPES)
    assert second["entry"]["variants"][-len(wa_ties.TIE_SHAPES):] == first["entry"]["variants"][-len(wa_ties.TIE_SHAPES):]
    assert all(v.get("tier") == "final" for v in suites.load_suite(ws, "Op")["variants"])


def test_ingest_only_acts_on_failed_verdicts(ws):
    assert wa_repro.ingest(ws, {"title": "Op", "status": "Pass", "submission_id": "x"}) is None
    out = wa_repro.ingest(ws, _receipt())  # unknown problem repo: reported, never raised
    assert out["applied"] is False


def test_lock_moves_from_pending_to_reproduced_to_fixed(ws):
    wa_repro.apply(ws, "Op", _receipt(), like=LIKE)
    ids = wa_repro.load_manifest(ws, "Op")["receipts"][SID]["variants"]
    view = wa_repro.status(ws, "Op", SID, BAD)
    assert view["state"] == "pending" and "run `opsb test --level final` on bbbbbbb" in wa_repro.next_step(view)
    _report(ws, BAD, "r1", {**{c["id"]: "PASS" for c in OFFICIAL}, **{i: "PASS" for i in ids}, ids[3]: "WA"},
            "2026-11-26T03:00:00+00:00")
    view = wa_repro.status(ws, "Op", SID, BAD, cand_sha=FIX)
    assert view["state"] == "reproduced" and view["reproduced_run"] == "r1" and view["failing_variants"] == [ids[3]]
    # A fix measured on other inputs (different case key) does not count; the same inputs do.
    _report(ws, FIX, "r2", {i: "PASS" for i in ids}, "2026-11-26T04:00:00+00:00", keys={ids[3]: "other"})
    assert wa_repro.status(ws, "Op", SID, BAD, cand_sha=FIX)["state"] == "reproduced"
    _report(ws, FIX, "r3", {i: "PASS" for i in ids}, "2026-11-26T05:00:00+00:00")
    fixed = wa_repro.status(ws, "Op", SID, BAD, cand_sha=FIX)
    assert fixed["state"] == "fixed" and fixed["fixed_run"] == "r3"
    import datetime
    early = datetime.datetime(2026, 11, 26, 4, 30, tzinfo=datetime.timezone.utc)
    assert wa_repro.status(ws, "Op", SID, BAD, cand_sha=FIX, as_of=early)["state"] == "reproduced"
    lock = wa_repro.lock_view(ws, "Op", {"escape_lock": True, "escape_submission_id": SID, "escape_sha": BAD})
    assert lock["state"] == "reproduced" and lock["variant_ids"] == ids and "awaiting a fix" in lock["next_step"]


@pytest.mark.parametrize("state", ["none", "pending", "reproduced", "fixed"])
def test_stage_one_reads_the_reproducer_state(ws, state):
    from scheduler.gate import SubmissionGate

    folder = ws / "results" / "Op" / FIX
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps({"exit_code": 0, "sha": FIX, "scheme": "reference", "run_id": "9" * 32}))
    (folder / "validation.log").write_text("ok\n")
    if state != "none":
        wa_repro.apply(ws, "Op", _receipt(), like=LIKE)
        ids = wa_repro.load_manifest(ws, "Op")["receipts"][SID]["variants"]
        if state in ("reproduced", "fixed"):
            _report(ws, BAD, "r1", {i: ("WA" if n == 0 else "PASS") for n, i in enumerate(ids)}, "2026-11-26T03:00:00+00:00")
        if state == "fixed":
            _report(ws, FIX, "r3", {i: "PASS" for i in ids}, "2026-11-26T05:00:00+00:00")
    gate = SubmissionGate(ws)
    lock = {"escape_lock": True, "escape_submission_id": SID, "escape_sha": BAD, "escape_status": "Wrong Answer"}
    calibrator = types.SimpleNamespace(inspect_escape_state=lambda title, as_of=None: lock)
    with mock.patch.object(gate, "_verify_git_commit_exists", return_value=(True, "")), \
            mock.patch("scheduler.case_bench.formal_evidence", return_value={"run_id": "9" * 32, "cases": []}), \
            mock.patch.object(gate.estimator, "get_calibrator", return_value=calibrator):
        result = gate.evaluate_safety("Op", FIX)
    expected = {"none": ("ESCAPE_LOCK_ACTIVE", False, "reproduce the failure"),
                "pending": ("ESCAPE_LOCK_ACTIVE", False, "awaiting reproduction"),
                "reproduced": ("ESCAPE_LOCK_ACTIVE", False, "reproduced, awaiting a fix"),
                "fixed": ("ESCAPE_REPRO_FIXED", True, "fixed: fffffff passes all")}[state]
    assert (result.code, result.passed) == expected[:2] and expected[2] in result.reason


def test_bridge_lists_then_applies_and_refuses_workers(ws, capsys, monkeypatch):
    from scheduler import cli_bridge

    (ws / "results" / "submit-queue-outcome.jsonl").write_text(json.dumps(_receipt()) + "\n")
    base = ["wa-repro", "--title", "Op", "--workspace", str(ws), "--like", json.dumps(LIKE)]
    assert cli_bridge.main(base) == 0
    listed = json.loads(capsys.readouterr().out)["receipts"][0]
    assert listed["planned"] is False and len(listed["variants"]) == wa_repro.SEEDS + len(wa_ties.TIE_SHAPES)
    assert not suites.suite_path(ws, "Op").exists()  # listing writes nothing
    assert cli_bridge.main([*base, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)["receipts"][0]
    assert applied["applied"] and applied["state"] == "pending" and "awaiting reproduction" in applied["next_step"]
    assert cli_bridge.main(base) == 0
    assert json.loads(capsys.readouterr().out)["receipts"][0]["planned"] is True
    monkeypatch.setenv("PARHELION_MCP_ROLE", "worker")
    assert cli_bridge.main([*base, "--apply"]) == 2


def test_tie_values_are_exact_float32_midpoints():
    base = {"id": "11", "params": {"qmax": 127, "qmin": -128, "mode": 0}}
    for body in wa_ties.tie_variants(base, SID):
        rows, chans = body["params"]["shape"]
        values = body["params"]["values"]
        assert len(values) == rows * chans and all(wa_ties.f32(v) == v for v in values)
        ties = 0
        for c in range(chans):
            col = values[c::chans]
            scale = wa_ties.f32(max(abs(v) for v in col) / 127)  # the oracle's mode-0 scale
            ties += sum(1 for v in col if wa_ties.quotient(v, scale) % 1 == 0.5)
        assert ties == int(body["name"].split(", ")[1].split(" ")[0]) > 0
