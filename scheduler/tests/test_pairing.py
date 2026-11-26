"""Case-bench pairing keys on the official inputs, not on the suite version."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler import case_bench, case_map, pairing  # noqa: E402
from scheduler.estimator import ScoreEstimator  # noqa: E402

CAND, BASE = "c" * 40, "b" * 40
KEYS = {"01": "k1", "02": "k2"}


def _catalog(keys, variants=()):
    rows = [{"id": cid, "source": "official", "key": key} for cid, key in keys.items()]
    return rows + [{"id": v, "source": "local", "key": "v-" + v} for v in variants]


def _report(ws, sha, run, minima, finished, *, version, keys=KEYS, variants=(), stored=True, catalog=True):
    doc = {"formal": True, "verified": True, "exit_code": 0, "sha": sha, "run_id": run, "timing_score_eligible": True,
           "suite_version": version, "finished_at": finished,
           "cases": [{"id": cid, "source": "official", "min_us": us} for cid, us in minima.items()]}
    if catalog:
        doc["catalog"] = _catalog(keys, variants)
    if stored:
        doc["pairing_key"] = pairing.official_key(_catalog(keys, variants))
    path = ws / "results" / "Op" / sha / "bench-runs" / run / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return doc


def _estimator(ws):
    est = ScoreEstimator.__new__(ScoreEstimator)
    est.workspace = ws
    return est


def test_final_only_variants_keep_runs_pairable(tmp_path):
    ws = tmp_path
    # The board SHA was measured before the WA receipts added variants (suite 0, older report
    # without the stored key); the candidate after (suite 3, two final-only variants).
    _report(ws, BASE, "r1", {"01": 10.0, "02": 100.0}, "2026-11-26T01:00:00+00:00", version=0, stored=False)
    _report(ws, CAND, "r2", {"01": 10.0, "02": 50.0}, "2026-11-26T02:00:00+00:00", version=3,
            variants=("local/wa-1", "local/wa-2"))
    pair = _estimator(ws)._case_bench_pair("Op", CAND, BASE)
    assert pair is not None and pair["matched"] == 2 and abs(pair["geo"] - 2 ** 0.5) < 1e-9
    assert case_map.local_minima(ws, "Op", CAND, pairing.pairing_key(json.loads(
        (ws / "results/Op" / BASE / "bench-runs/r1/report.json").read_text())))[0] == {"01": 10.0, "02": 50.0}


def test_changed_official_inputs_break_pairing(tmp_path):
    ws = tmp_path
    _report(ws, BASE, "r1", {"01": 10.0, "02": 100.0}, "2026-11-26T01:00:00+00:00", version=0)
    _report(ws, CAND, "r2", {"01": 10.0, "02": 50.0}, "2026-11-26T02:00:00+00:00", version=1,
            keys={"01": "k1", "02": "k2-new-params"})
    est = _estimator(ws)
    assert est._case_bench_pair("Op", CAND, BASE) is None
    # Once the board SHA is re-run on the new inputs, the newest shared identity pairs.
    _report(ws, BASE, "r3", {"01": 10.0, "02": 80.0}, "2026-11-26T03:00:00+00:00", version=1,
            keys={"01": "k1", "02": "k2-new-params"})
    pair = est._case_bench_pair("Op", CAND, BASE)
    assert pair is not None and pair["runs_base"] == 1 and abs(pair["geo"] - (80 / 50) ** 0.5) < 1e-9


def test_old_reports_derive_the_key_and_keyless_ones_fall_back_to_the_version():
    new = {"suite_version": 3, "pairing_key": pairing.official_key(_catalog(KEYS, ("local/x",)))}
    old = {"suite_version": 0, "catalog": _catalog(KEYS)}
    assert pairing.pairing_key(old) == new["pairing_key"] and pairing.same_inputs(old, new)
    keyless = {"suite_version": 0, "cases": [{"id": "01", "source": "official", "min_us": 1.0}]}
    assert pairing.pairing_key(keyless) == "suite-v0"
    assert pairing.same_inputs(keyless, {**new, "suite_version": 0}) and not pairing.same_inputs(keyless, new)


def test_best_known_minima_spans_a_variant_bump(tmp_path):
    ws = tmp_path
    _report(ws, CAND, "r1", {"01": 9.0, "02": 40.0}, "2026-11-26T01:00:00+00:00", version=0, stored=False)
    current = {"title": "Op", "sha": CAND, "run_id": "r9", "suite_version": 2,
               "pairing_key": pairing.official_key(_catalog(KEYS, ("local/wa-1",)))}
    assert case_bench.best_known_minima(ws, current) == {"01": 9.0, "02": 40.0}
    changed = {**current, "pairing_key": pairing.official_key(_catalog({"01": "k1", "02": "other"}))}
    assert case_bench.best_known_minima(ws, changed) == {}


def _on_host(ws, sha, run, host):
    path = ws / "results" / "Op" / sha / "bench-runs" / run / "report.json"
    doc = json.loads(path.read_text())
    doc["host"] = host
    path.write_text(json.dumps(doc))


def test_runs_pair_only_on_the_same_device_host(tmp_path):
    """4.3.2: two device hosts are two instruments; a candidate measured on npu2 never pairs
    with a board run measured on npu, so the gate falls back instead of mixing devices."""
    ws = tmp_path
    _report(ws, BASE, "r1", {"01": 10.0, "02": 100.0}, "2026-11-26T01:00:00+00:00", version=0)
    _on_host(ws, BASE, "r1", "npu")
    _report(ws, CAND, "r2", {"01": 10.0, "02": 50.0}, "2026-11-26T02:00:00+00:00", version=0)
    _on_host(ws, CAND, "r2", "npu2")
    est = _estimator(ws)
    assert est._case_bench_pair("Op", CAND, BASE) is None
    _report(ws, BASE, "r3", {"01": 10.0, "02": 80.0}, "2026-11-26T03:00:00+00:00", version=0)
    _on_host(ws, BASE, "r3", "npu2")
    pair = est._case_bench_pair("Op", CAND, BASE)
    assert pair is not None and pair["runs_base"] == 1 and abs(pair["geo"] - (80 / 50) ** 0.5) < 1e-9
