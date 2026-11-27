"""A long case the broker cut to two timed runs is verified on those two runs only (FFT case 05).

device_broker stops an execution of 2 s or more after one warmup and LONG_RUN_REPEATS timed runs, and
reports one sample per executed run. The unexecuted run directories stay empty; before the fix verify()
read all of them and failed every long case with "missing or unexpected output files" on repeat 3.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "npu_runtime"
sys.path.insert(0, str(RUNTIME))

import case_batch  # noqa: E402
import device_broker  # noqa: E402


PAYLOAD = bytes(range(32))


@pytest.fixture(autouse=True)
def fake_numpy(monkeypatch):
    """numpy lives on the device host; verify() needs only load and fromfile here."""
    want = types.SimpleNamespace(dtype="float32", nbytes=len(PAYLOAD))
    fake = types.ModuleType("numpy")
    fake.load = lambda path, **kw: want
    fake.fromfile = lambda path, dtype=None: Path(path).read_bytes()
    monkeypatch.setitem(sys.modules, "numpy", fake)


def _plan(tmp: Path, repeats: int, executed: int) -> dict:
    base = tmp / "case"
    base.mkdir()
    (base / "y.npy").write_bytes(b"")
    runs = []
    for r in range(repeats):
        out = tmp / f"run-{r}" / "output"
        out.mkdir(parents=True)
        if r < executed:
            (out / "y.bin").write_bytes(PAYLOAD)
        runs.append(out.parent)
    return {"parts": [{"path": str(base), "tolerances": {"y": {}}, "runs": runs, "warm": []}],
            "samples": [], "cold": [], "timed": True}


def _compare(got, want, tolerance, strict):
    return {"pass": got == PAYLOAD}


def test_a_case_cut_to_two_timed_runs_verifies_on_those_runs(tmp_path):
    plan = _plan(tmp_path, repeats=5, executed=device_broker.LONG_RUN_REPEATS)
    samples = [9642.0] * device_broker.LONG_RUN_REPEATS  # what the broker's PASS event carries
    case_batch.keep_executed(plan, samples)
    assert len(plan["parts"][0]["runs"]) == device_broker.LONG_RUN_REPEATS
    assert case_batch.verify(plan, {}, _compare)["status"] == "PASS"


def test_a_short_case_still_needs_every_repeat(tmp_path):
    plan = _plan(tmp_path, repeats=5, executed=3)
    case_batch.keep_executed(plan, [1.0] * 5)  # five samples: nothing is cut
    checked = case_batch.verify(plan, {}, _compare)
    assert checked["status"] == "WA" and checked["repeat"] == 4
