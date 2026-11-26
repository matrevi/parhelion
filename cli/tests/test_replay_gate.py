"""4.3 P21: ``opsb replay gate`` judges each recorded shot again on a throwaway workspace copy."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.replay import gate_metrics, gate_replay  # noqa: E402

TITLES = ("OpA", "OpB", "OpC")


def sha(tag: str) -> str:
    """A 40-hex commit id; the tag keeps the cases readable."""
    return (tag + "0" * 40)[:40]


def _recorded(root: Path, title: str, sha_value: str, *, reference: bool = False) -> None:
    """The local evidence S1 reads: a clean ``result.json`` and a green ``validation.log``."""
    folder = root / "results" / title / sha_value
    if folder.is_dir():
        return
    folder.mkdir(parents=True)
    body = {"sha": sha_value, "exit_code": 0, "layout": "template", "verified": True}
    if reference:
        body["scheme"] = "reference"
    (folder / "result.json").write_text(json.dumps(body), encoding="utf-8")
    (folder / "validation.log").write_text(
        "EXTENDED_PASSED 5\nmismatched=0/5\nGATE_BAD bad=0/5\nGATE_DET identical=True\n", encoding="utf-8")


def build_workspace(root: Path, *, latest: bool = True) -> Path:
    """Three problems, thirteen judged shots: a zeroed board with a restore, a TLE, and six flat shots."""
    (root / "results" / "scoring-evidence").mkdir(parents=True)
    (root / "opsb.workspace.json").write_text(
        json.dumps({"schema_version": 1, "layout": "clean-agents-first",
                    "problems": [{"title": t, "path": t, "difficulty": 2} for t in TITLES]}), encoding="utf-8")
    if latest:
        (root / "results" / "scoring-evidence" / "problems-all.json").write_text(
            json.dumps({t: {"title": t, "difficulty": 2, "ranking_submission_mode": "latest"} for t in TITLES}),
            encoding="utf-8")
    rows = [
        # OpA: new high, another new high, a WA that zeroes the board, the restore, a regression.
        ("OpA", sha("a1"), "Pass", 40.0, "2026-11-10T01:00:00+00:00"),
        ("OpA", sha("a2"), "Pass", 41.0, "2026-11-10T01:10:00+00:00"),
        ("OpA", sha("a3"), "Wrong Answer", 0.0, "2026-11-10T01:20:00+00:00"),
        ("OpA", sha("a1"), "Pass", 40.0, "2026-11-10T01:30:00+00:00"),
        ("OpA", sha("a4"), "Pass", 39.0, "2026-11-10T01:40:00+00:00"),
        # OpB: a timeout, the same commit resubmitted (S1 freezes it), a pass, a WA, then a restore.
        ("OpB", sha("b1"), "Time Limit Exceeded", 0.0, "2026-11-10T02:00:00+00:00"),
        ("OpB", sha("b1"), "Time Limit Exceeded", 0.0, "2026-11-10T02:10:00+00:00"),
        ("OpB", sha("b2"), "Pass", 50.0, "2026-11-10T02:20:00+00:00"),
        ("OpB", sha("b3"), "Wrong Answer", 0.0, "2026-11-10T02:30:00+00:00"),
        ("OpB", sha("b2"), "Pass", 50.0, "2026-11-10T02:40:00+00:00"),
        # OpC: six flat shots in the 12h window, then one that beats the peak (arms the breaker).
        ("OpC", sha("c1"), "Pass", 60.0, "2026-11-10T03:00:00+00:00"),
    ] + [("OpC", sha(f"c{n}"), "Pass", 59.0, f"2026-11-10T03:{n:02d}:00+00:00") for n in range(2, 8)] + [
        ("OpC", sha("c8"), "Pass", 61.0, "2026-11-10T03:50:00+00:00"),
    ]
    ledger = []
    for idx, (title, value, status, score, when) in enumerate(rows):
        _recorded(root, title, value)
        ledger.append({"submission_id": f"sid{idx:04d}", "title": title, "sha": value, "status": status,
                       "score": score if status == "Pass" else None, "submitted_at": when,
                       "queued_at": when, "source": "submit-poll"})
    (root / "results" / "submit-queue-outcome.jsonl").write_text(
        "\n".join(json.dumps(row) for row in ledger) + "\n", encoding="utf-8")
    return root


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return build_workspace(tmp_path / "contest")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private platform home so the replay cannot read or write the machine's real one."""
    home = tmp_path_factory.mktemp("ph")
    monkeypatch.setenv("PARHELION_HOME", str(home))
    monkeypatch.setenv("OPSB_HOME", str(home))
    return home


def test_replays_every_judged_shot_and_counts_blocks(workspace: Path) -> None:
    result = gate_replay.run(workspace, jobs=1, detail=True)
    assert result["ok"] and result["shots"]["judged"] == 18 and result["shots"]["replayed"] == 18
    assert set(result["groups"]) == {"default", "420"}
    default, old = result["groups"]["default"], result["groups"]["420"]
    # Every group judges the same history, so the receipt-derived outcome must not move; only the
    # board-dependent fields (points, closing gap, recovery) differ between latest and best.
    receipt_keys = ("judged", "new_highs", "new_high_rate", "ties", "tie_rate", "regressions",
                    "regression_rate", "hard_failures", "hard_fail_rate", "same_source_resubmits",
                    "same_source_rate")
    assert {k: default["outcome"][k] for k in receipt_keys} == {k: old["outcome"][k] for k in receipt_keys}
    assert default["outcome"]["recovery_events"] == 2 and old["outcome"]["recovery_events"] == 0
    assert default["outcome"]["judged"] == 18
    assert default["outcome"]["hard_failures"] == 4  # WA x2 + TLE x2
    assert default["outcome"]["same_source_resubmits"] == 3  # a1 twice, b1 twice, b2 twice
    # S1 refuses the commit that timed out unchanged, and says what to do instead.
    tle = [s for s in default["shots_detail"] if s["code"] == "TLE_SHA_REPEAT"]
    assert tle and tle[0]["next_step"]
    assert default["gate"]["blocked_with_next_step"] >= 1
    assert "SAFETY:TLE_SHA_REPEAT" in default["gate"]["by_stage_code"]
    # 4.2.0's breaker refuses the shot that beats the peak after six flat ones.
    assert "CIRCUIT_BREAKER:CIRCUIT_OPEN_SWITCH_BASELINE" in old["gate"]["by_stage_code"]
    assert any(h["sha"] == sha("c8")[:7] for h in old["gate"]["blocked_new_highs"])
    assert old["gate"]["blocked"] > default["gate"]["blocked"]
    assert old["board_mode"] == "best" and default["board_mode"] == "latest"


def test_blocked_new_highs_beat_the_peak_of_that_moment(workspace: Path) -> None:
    result = gate_replay.run(workspace, jobs=1, detail=True)
    for group in result["groups"].values():
        for item in group["gate"]["blocked_new_highs"]:
            assert item["score"] > item["previous_peak"]
            assert item["code"]
    opa = [s for s in result["groups"]["default"]["shots_detail"] if s["title"] == "OpA"]
    assert [s["score"] for s in opa] == [40.0, 41.0, 0.0, 40.0, 39.0]
    assert opa[0]["board_score"] == 0.0 and opa[1]["board_score"] == 40.0


def test_latest_zeroed_board_approves_the_restore(workspace: Path) -> None:
    gate = gate_replay.run(workspace, jobs=1)["groups"]["default"]
    restore = gate["gate"]["restore"]
    assert restore["zero_events"] >= 2  # OpA after the WA, OpB after the second timeout
    assert restore["restore_attempts"] >= 2
    assert restore["restore_approved"] == restore["restore_attempts"]
    assert restore["restore_approve_rate"] == 1.0
    restores = [e for e in restore["events"] if e["is_restore"]]
    assert restores and all(e["approved"] for e in restores)
    # A judge-side failure freezes nothing while S1 lets the next shot through.
    assert gate["gate"]["frozen"]["total"] >= 1


def test_workspace_is_byte_identical_after_a_replay(workspace: Path) -> None:
    before = snapshot(workspace)
    config = Path(workspace) / "results" / "submit-queue-outcome.jsonl"
    config_before = (config.stat().st_mtime_ns, config.stat().st_size)
    result = gate_replay.run(workspace, jobs=1)
    assert snapshot(workspace) == before
    assert (config.stat().st_mtime_ns, config.stat().st_size) == config_before
    assert not Path(result["copy"]["copy"]).parent.exists()  # the copy is gone
    assert not (workspace / "results" / "feedback-log.jsonl").exists()
    assert not (workspace / "results" / "OpA" / ".case-map.json").exists()


def test_platform_state_is_not_written(workspace: Path, isolated_home: Path) -> None:
    before = snapshot(isolated_home) if isolated_home.is_dir() else {}
    result = gate_replay.run(workspace, jobs=1)
    after = snapshot(isolated_home) if isolated_home.is_dir() else {}
    assert after == before  # the replay keeps its PARHELION_HOME inside the deleted copy
    assert not (isolated_home / "workspaces").exists()
    assert result["copy"]["bench_specs"] is False  # nothing to copy, and nothing created


def test_parallel_and_serial_runs_agree(workspace: Path) -> None:
    serial = gate_replay.run(workspace, jobs=1)
    parallel = gate_replay.run(workspace, jobs=4)
    assert serial["groups"] == parallel["groups"]
    assert serial["shots"] == parallel["shots"]


def test_gate_switch_is_scoped_to_the_replay(workspace: Path, isolated_home: Path) -> None:
    from scheduler.gate import GateConfig

    (isolated_home / "config.json").write_text(json.dumps({"scheduler": {"noise_floor_delta": 2.5}}), encoding="utf-8")
    live = ((isolated_home / "config.json").stat().st_mtime_ns,
            (isolated_home / "config.json").stat().st_size)
    plain = gate_replay.run(workspace, jobs=1, groups=["default"])["groups"]["default"]
    armed = gate_replay.run(workspace, jobs=1, groups=["default"],
                            gate_switches=["tle_arms_escape_lock=true"])["groups"]["default"]
    assert armed["gate"]["blocked"] >= plain["gate"]["blocked"]
    assert "SAFETY:ESCAPE_LOCK_ACTIVE" in armed["gate"]["by_stage_code"]
    assert armed["switches"]["tle_arms_escape_lock"] is True
    assert GateConfig().tle_arms_escape_lock is False  # the switch never becomes a default
    assert ((isolated_home / "config.json").stat().st_mtime_ns,
            (isolated_home / "config.json").stat().st_size) == live


def test_window_replays_only_the_tail_but_keeps_the_board(workspace: Path) -> None:
    tail = gate_replay.run(workspace, since="2026-11-10T03:00:00Z", jobs=1)["groups"]["default"]
    whole = gate_replay.run(workspace, jobs=1)["groups"]["default"]
    assert tail["gate"]["shots"] == 8 and whole["gate"]["shots"] == 18
    # The window only replays OpC, whose board closes on its peak, so its own closing gap is 0.
    assert tail["outcome"]["closing_gap"] == 0.0
    assert whole["outcome"]["closing_gap"] >= 2.0  # OpA closes 2 points below its peak


def test_rejects_unknown_switch_group_and_board_mode(workspace: Path) -> None:
    with pytest.raises(ValueError):
        gate_replay.run(workspace, groups=["nope"])
    with pytest.raises(ValueError):
        gate_replay.run(workspace, board_mode="sideways")
    with pytest.raises(ValueError):
        gate_replay.run(workspace, gate_switches=["not_a_switch=1"])
    with pytest.raises(ValueError):
        gate_replay.run(workspace / "missing")


def test_manifest_can_be_given_explicitly(tmp_path: Path) -> None:
    root = build_workspace(tmp_path / "contest")
    (root / "opsb.workspace.json").unlink()
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"problems": [{"title": t, "path": t} for t in TITLES]}), encoding="utf-8")
    result = gate_replay.run(root, manifest=roster, jobs=1)
    assert result["copy"]["manifest"] == "arg"
    assert result["shots"]["replayed"] == 18


def test_every_shot_carries_a_board_mode_and_bucket(workspace: Path) -> None:
    result = gate_replay.run(workspace, jobs=1, detail=True)
    detail = result["groups"]["default"]["shots_detail"]
    assert len(detail) == 18
    assert {s["board_mode"] for s in detail} == {"latest"}
    assert result["groups"]["default"]["per_problem"] == {t: "latest" for t in TITLES}
    assert set(result["groups"]["420"]["per_problem"].values()) == {"best"}
    for shot in detail:
        assert shot["status_tag"] and shot["reason"]
        assert shot["board_source"] in {"none", "previous", "unknown", "peak"}


def test_metrics_fold_a_known_history() -> None:
    def shot(title: str, value: str, score: float | None, *, status: str = "Pass", before: float = 0.0,
             after: float | None = None, approved: bool = True, stage: str = "", code: str = "",
             step: str = "", at: float = 0.0, board_status: str | None = None) -> dict[str, object]:
        return {"title": title, "sha": value, "short_sha": value[:7], "status": status, "score": score,
                "board_score": before, "board_after": before if after is None else after,
                "board_status": board_status or ("Pass" if before > 0 else "no-submission"), "approved": approved,
                "stage": stage, "code": code, "next_step": step, "at": at, "ts": at,
                "submitted_at": str(at), "board_source": "previous", "board_mode": "latest",
                "paired_cases": 1, "method": "case_bench_paired"}

    shots = [shot("OpA", sha("a1"), 40.0, after=40.0, at=0.0),
             shot("OpA", sha("a2"), 41.0, before=40.0, after=41.0, at=60.0),
             shot("OpA", sha("a3"), None, status="Wrong Answer", before=41.0, after=0.0, at=120.0),
             shot("OpA", sha("a1"), 40.0, before=0.0, after=40.0, at=180.0, board_status="Wrong Answer")]
    blocks = gate_metrics.blocked(shots)
    assert blocks["blocked"] == 0 and blocks["blocked_rate"] == 0.0
    found = gate_metrics.events(shots)
    assert found["restore"]["zero_events"] == 1
    assert found["restore"]["restore_approved"] == 1
    out = gate_metrics.outcome(shots)
    assert out["new_highs"] == 1
    assert out["points"] == 40.0 and out["hard_failures"] == 1 and out["regressions"] == 1
    assert out["recovery_median_min"] == 1.0  # zeroed at 120s, back at 180s
