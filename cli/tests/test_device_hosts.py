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


def test_choosing_another_primary_keeps_every_problem_on_its_device(tmp_path):
    ws = _ws(tmp_path)
    before = device_hosts.assignment({"npu_ssh_host": "npu", "npu_ssh_hosts": ["npu", "npu2"]}, ws)
    after = device_hosts.assignment({"npu_ssh_host": "npu2", "npu_ssh_hosts": ["npu", "npu2"]}, ws)
    assert before == after
    # a primary outside the pool still runs tests, in front of it
    assert device_hosts.device_hosts({"npu_ssh_host": "npu3", "npu_ssh_hosts": ["npu", "npu2"]}) == ["npu3", "npu", "npu2"]


def test_set_pool_keeps_the_primary_and_clears_a_single_host_pool():
    cfg = {"npu_ssh_host": "npu"}
    assert device_hosts.set_pool(cfg, ["npu2", "npu2", ""]) == ["npu", "npu2"]
    assert cfg["npu_ssh_hosts"] == ["npu", "npu2"]
    assert device_hosts.set_pool(cfg, []) == ["npu"]
    assert cfg["npu_ssh_hosts"] == []


def test_a_pin_overrides_the_roster_order_only_for_a_known_host(tmp_path):
    ws = _ws(tmp_path)
    cfg = {"npu_ssh_host": "npu", "npu_ssh_hosts": ["npu2"], "npu_host_by_problem": {"Alpha": "npu2", "Beta": "gone"}}
    assert device_hosts.host_for(cfg, ws, "Alpha") == "npu2"
    assert device_hosts.host_for(cfg, ws, "Beta") == "npu2"  # unknown pin: back to the roster order
    assert device_hosts.host_for(cfg, ws, "NotInRoster") in ("npu", "npu2")
