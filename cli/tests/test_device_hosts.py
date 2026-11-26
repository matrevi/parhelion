"""Device host routing (4.3.2): each problem is pinned to one device host."""
from __future__ import annotations

import json
import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core import device_hosts  # noqa: E402

TITLES = ["Alpha", "Beta", "Gamma", "Delta"]


def _ws(tmp_path):
    (tmp_path / "opsb.workspace.json").write_text(json.dumps({
        "layout": "clean-agents-first", "problems": [{"title": t, "path": t, "difficulty": 2} for t in TITLES]}))
    return tmp_path


def test_one_host_runs_everything(tmp_path):
    ws = _ws(tmp_path)
    assert device_hosts.device_hosts({}) == ["npu"]
    assert device_hosts.device_hosts({"npu_ssh_host": "npu", "npu_ssh_hosts": []}) == ["npu"]
    assert {device_hosts.host_for({"npu_ssh_host": "npu"}, ws, t) for t in TITLES} == {"npu"}


def test_problems_alternate_over_the_roster_and_stay_pinned(tmp_path):
    ws = _ws(tmp_path)
    cfg = {"npu_ssh_host": "npu", "npu_ssh_hosts": ["npu", "npu2", "npu2", ""]}
    assert device_hosts.device_hosts(cfg) == ["npu", "npu2"]
    assert device_hosts.assignment(cfg, ws) == {"Alpha": "npu", "Beta": "npu2", "Gamma": "npu", "Delta": "npu2"}
    # the same problem always lands on the same host, whatever the case of its name
    assert device_hosts.host_for(cfg, ws, "beta") == device_hosts.host_for(cfg, ws, "Beta") == "npu2"


def test_a_pin_overrides_the_roster_order_only_for_a_known_host(tmp_path):
    ws = _ws(tmp_path)
    cfg = {"npu_ssh_host": "npu", "npu_ssh_hosts": ["npu2"], "npu_host_by_problem": {"Alpha": "npu2", "Beta": "gone"}}
    assert device_hosts.host_for(cfg, ws, "Alpha") == "npu2"
    assert device_hosts.host_for(cfg, ws, "Beta") == "npu2"  # unknown pin: back to the roster order
    assert device_hosts.host_for(cfg, ws, "NotInRoster") in ("npu", "npu2")
