"""`opsb ssh pool|select|list`: the device pool the app's settings tab edits (4.3.2)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "opsb"


def _opsb(tmp_path: Path, *args: str) -> tuple[int, dict]:
    env = {**os.environ, "HOME": str(tmp_path / "home"), "PARHELION_HOME": str(tmp_path / "phome"),
           "PARHELION_DISABLE_DAEMON_PROXY": "1"}
    env.pop("PYTHONPATH", None)
    (tmp_path / "home").mkdir(exist_ok=True)
    out = subprocess.run([sys.executable, str(CLI), "ssh", *args], env=env, capture_output=True, text=True, timeout=60)
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    return out.returncode, (json.loads(lines[-1]) if lines else {"stderr": out.stderr})


def test_pool_add_remove_and_the_primary_stays(tmp_path):
    rc, res = _opsb(tmp_path, "pool")
    assert rc == 0 and res["pool"] == ["npu"]
    rc, res = _opsb(tmp_path, "pool", "--add", "npu2", "--add", "npu3")
    assert res["pool"] == ["npu", "npu2", "npu3"]
    rc, res = _opsb(tmp_path, "pool", "--remove", "npu")
    assert rc == 2 and not res["ok"]
    rc, res = _opsb(tmp_path, "pool", "--remove", "npu3")
    assert res["pool"] == ["npu", "npu2"]
    listed = _opsb(tmp_path, "list")[1]
    assert listed["pool"] == ["npu", "npu2"]


def test_select_keeps_the_pool_order_so_no_problem_moves(tmp_path):
    _opsb(tmp_path, "pool", "--set", "npu,npu2")
    rc, res = _opsb(tmp_path, "select", "npu2")
    assert rc == 0 and res["active_host"] == "npu2" and res["pool"] == ["npu", "npu2"]
    # selecting a host outside the pool adds it at the end
    rc, res = _opsb(tmp_path, "select", "npu3")
    assert res["pool"] == ["npu", "npu2", "npu3"]
    # with no pool configured, select only changes the primary
    _opsb(tmp_path, "pool", "--set", "")
    assert _opsb(tmp_path, "select", "npu")[1]["pool"] == ["npu"]
