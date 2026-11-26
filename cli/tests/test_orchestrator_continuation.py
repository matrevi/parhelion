"""A headless orchestrator keeps its turn going while the sprint has live seats (4.3.2)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.drivers import antigravity_acp, registry  # noqa: E402


def test_continues_while_seats_are_live_and_stops_when_none_are():
    """4.3 run 2: the background orchestrator ended its turn twice within minutes and exited."""
    live = iter([14, 14, 0])
    sent: list[str] = []
    n = antigravity_acp.continue_turns(sent.append, lambda: next(live), clock=lambda: 0.0, sleep=lambda _s: None)
    assert n == 2 and "14 seats" in sent[0] and "Do not end your turn" in sent[0]


def test_a_quick_round_waits_and_the_rounds_are_capped():
    naps: list[float] = []
    ticks = iter(range(1000))
    n = antigravity_acp.continue_turns(lambda _t: None, lambda: 3, max_rounds=4, min_round_sec=30,
                                       clock=lambda: float(next(ticks)), sleep=naps.append)
    assert n == 4 and naps == [30, 30, 30, 30]


def test_only_the_orchestrator_job_gets_a_live_seat_counter(tmp_path, monkeypatch):
    seen = {}

    class Fake:
        live_seats = None

        def _execute_headless_turn(self, **kw):
            seen["live_seats"] = self.live_seats
            return 0

    monkeypatch.setattr(registry, "get_driver", lambda _k: Fake())
    monkeypatch.setattr(registry, "update_session", lambda *a, **k: None)
    monkeypatch.setattr(registry, "_live_seat_count", lambda ws: 5)
    for problem, expect in (("ALL-10", 5), ("SomeOp", None)):
        job = tmp_path / f"{problem}.json"
        job.write_text(json.dumps({"driver": "deepseek", "workspace": str(tmp_path), "problem": problem,
                                   "worktree": str(tmp_path), "brief": "b", "session_id": "s", "native_session_id": "n",
                                   "cmd": ["true"], "env": {}, "log_path": str(tmp_path / "log.jsonl")}))
        Fake.live_seats = None
        assert registry._run_worker_job(job) == 0
        got = seen["live_seats"]
        assert (got() if got else None) == expect
