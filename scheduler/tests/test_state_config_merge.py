"""A gate switch set by one process survives another engine that saves twice (4.3.2)."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler.cli_bridge import SchedulerEngine  # noqa: E402


class StateConfigMergeTest(unittest.TestCase):
    def test_a_second_save_does_not_write_back_a_stale_switch(self) -> None:
        """4.3 run 2: `opsb queue config --gate low_gain_min_prob=0.6` was set, then a long bridge run
        that had loaded the state earlier saved twice and restored 0.5."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ws, st = root / "ws", root / "st"
            (ws / "results").mkdir(parents=True)
            st.mkdir()
            (st / "scheduler_state.json").write_text(json.dumps({"config": {"low_gain_min_prob": 0.5}}))
            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(root / "ph")}, clear=False):
                stale = SchedulerEngine(workspace=ws, state_dir=st)
                setter = SchedulerEngine(workspace=ws, state_dir=st)
                setter.config.set_switch("low_gain_min_prob=0.6")
                setter._save_state()
                stale.held_items["Op:a"] = {"title": "Op", "sha": "a"}
                stale._save_state()
                self.assertEqual(stale.config.low_gain_min_prob, 0.6)
                stale.held_items["Op:b"] = {"title": "Op", "sha": "b"}
                stale._save_state()
                saved = json.loads((st / "scheduler_state.json").read_text())
                self.assertEqual(saved["config"]["low_gain_min_prob"], 0.6)
                self.assertEqual(set(saved["held_items"]), {"Op:a", "Op:b"})
                # an engine's own change still wins over the value it loaded
                stale.config.set_switch("low_gain_min_prob=0.7")
                stale._save_state()
                self.assertEqual(json.loads((st / "scheduler_state.json").read_text())["config"]["low_gain_min_prob"], 0.7)


if __name__ == "__main__":
    unittest.main()
