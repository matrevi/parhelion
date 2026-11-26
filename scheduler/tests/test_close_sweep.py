"""4.3 P25 closing reconciliation: the three lists, the window, the restore boost and per-slot use."""
from __future__ import annotations

import datetime
import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from cli.opsb_core.device_env import device_text  # noqa: E402
from scheduler import close_sweep  # noqa: E402
from scheduler.estimator import ScoreEstimator  # noqa: E402

WORK_PREFIX = device_text("work_prefix")
UTC = datetime.timezone.utc
TITLE = "CloseOp"


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo: pathlib.Path, text: str) -> str:
    (repo / "op_kernel" / "k.cpp").write_text(f"// {text}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", text)
    return _git(repo, "rev-parse", "HEAD")


def _repo(ws: pathlib.Path, *, on_branch: bool) -> tuple[pathlib.Path, str, str]:
    """A problem repository with a ``best`` commit and a newer ``board`` commit on ``main``."""
    repo = ws / WORK_PREFIX / TITLE
    (repo / "op_kernel").mkdir(parents=True)
    (repo / "op_host").mkdir()
    (repo / "op_host" / "h.cpp").write_text("// host\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    best = _commit(repo, "best")
    if on_branch:  # the best commit lives only on the race branch
        _git(repo, "checkout", "-q", "-b", "race")
        best = _commit(repo, "best on the race branch")
        _git(repo, "checkout", "-q", "main")
    board = _commit(repo, "board")
    return repo, best, board


def _workspace(root: pathlib.Path, *, mode: str = "latest", on_branch: bool = False,
               board: tuple[str, float] = ("Pass", 55.0), tag: str | None = None,
               extra: list[dict] | None = None) -> tuple[pathlib.Path, str, str, str]:
    ws = root / "ws"
    repo, best, board_sha = _repo(ws, on_branch=on_branch)
    results = ws / "results"
    (results / TITLE).mkdir(parents=True)
    (results / TITLE / ".judge-rule.json").write_text(json.dumps({"ranking_submission_mode": mode}))
    rows = [{"title": TITLE, "sha": best, "status": "Pass", "score": 60.0, "submission_id": "s-best",
             "submitted_at": "2026-11-20T00:00:00Z", "queued_at": "2026-11-20T00:00:00Z"},
            {"title": TITLE, "sha": board_sha, "status": board[0], "score": board[1],
             "submission_id": "s-board", "submitted_at": "2026-11-21T00:00:00Z",
             "queued_at": "2026-11-21T00:00:00Z"}]
    rows.extend(extra or [])
    (results / "submit-queue-outcome.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (results / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (results / "score-snapshot.json").write_text(json.dumps({TITLE: {"status": "Pass", "score": board[1]}}))
    if tag is not None:
        _git(repo, "tag", "-f", tag, best)
    (ws / WORK_PREFIX / "problems.json").write_text(
        json.dumps([{"title": TITLE, "slug": TITLE.lower(), "path": f"{WORK_PREFIX}/{TITLE}",
                     "difficulty": 3}]))
    return ws, best, board_sha, repo


def _sweep(ws: pathlib.Path, **config) -> dict:
    now = config.pop("now", None) or datetime.datetime.now(UTC)
    doc = close_sweep.sweep(ws, ScoreEstimator(ws), now=now,
                            config={"contest_end": None, "window_min": 90.0, "restore_boost": 3.0,
                                    "restore_min_gap": 0.0, **config})
    return doc


# ---------------------------------------------------------------- the three lists

def test_a_latest_board_below_its_own_best_lists_one_restore(tmp_path: pathlib.Path) -> None:
    ws, best, board_sha, _ = _workspace(tmp_path)
    doc = _sweep(ws)
    assert [r["title"] for r in doc["restore"]] == [TITLE]
    row = doc["restore"][0]
    assert (row["best_sha"], row["best_score"]) == (best[:7], 60.0)
    assert (row["board_sha"], row["board_score"]) == (board_sha[:7], 55.0)
    assert row["gap"] == 5.0 and row["weighted_gap"] == 15.0 and row["critical"] is False
    assert doc["totals"]["restore"] == 1 and doc["totals"]["restore_gap"] == 5.0
    assert row["mode"] == "latest" and row["sigma_scope"] == "constant"
    assert row["sigma_hat"] == pytest.approx(close_sweep.DEFAULT_SIGMA, abs=0.01)


def test_a_best_board_never_lists_a_restore(tmp_path: pathlib.Path) -> None:
    ws, _, _, _ = _workspace(tmp_path, mode="best")
    doc = _sweep(ws)
    assert doc["board_mode"] == {"best": 1}
    assert doc["restore"] == [] and doc["totals"]["restore"] == 0
    row = doc["problems"][0]  # the gap itself is still reported, it just is not actionable
    assert row["gap"] == 5.0 and row["mode"] == "best"


def test_a_zero_or_failed_board_is_a_critical_restore(tmp_path: pathlib.Path) -> None:
    ws, _, _, _ = _workspace(tmp_path / "zero", board=("Wrong Answer", 0.0))
    row = _sweep(ws)["restore"][0]
    assert row["critical"] is True and row["board_score"] == 0.0
    assert _sweep(ws)["totals"]["restore_gap"] == 60.0


def test_the_board_is_replayed_as_of_the_contest_end(tmp_path: pathlib.Path) -> None:
    """A receipt judged after the contest must not rewrite the closing gap (G5 currency)."""
    late = {"title": TITLE, "sha": "d" * 40, "status": "Pass", "score": 70.0, "submission_id": "s-late",
            "submitted_at": "2026-11-25T00:00:00Z", "queued_at": "2026-11-25T00:00:00Z"}
    ws, _, _, _ = _workspace(tmp_path, extra=[late])
    at_close = _sweep(ws, contest_end="2026-11-22T00:00:00+00:00")
    assert at_close["restore"] and at_close["restore"][0]["gap"] == 5.0
    now = _sweep(ws)
    assert now["restore"] == []  # the 70.0 draw closed the gap after the contest


def test_merge_lists_the_branch_and_never_merges(tmp_path: pathlib.Path) -> None:
    ws, best, board_sha, _ = _workspace(tmp_path, on_branch=True)
    doc = _sweep(ws)
    assert [r["title"] for r in doc["merge"]] == [TITLE]
    row = doc["merge"][0]
    assert row["best_sha"] == best[:7] and row["main_sha"] == board_sha[:7]
    assert row["branches"] == ["race"]
    assert row["best_score"] == 60.0


def test_a_best_sha_on_main_is_not_listed_for_merge(tmp_path: pathlib.Path) -> None:
    ws, best, _, _ = _workspace(tmp_path)
    doc = _sweep(ws)
    assert doc["merge"] == []
    assert "main" in doc["problems"][0]["branches"]
    assert doc["problems"][0]["best_sha"] == best[:7]


def test_a_stale_best_tag_is_listed_and_a_matching_one_is_not(tmp_path: pathlib.Path) -> None:
    stale, _, _, _ = _workspace(tmp_path / "stale", tag="best/42.00-0000000")
    doc = _sweep(stale)
    assert [r["title"] for r in doc["tag"]] == [TITLE]
    assert doc["tag"][0]["tag"] == "42.00-0000000"
    assert doc["tag"][0]["target"].startswith("best/60.00-")
    ws, best, _, _ = _workspace(tmp_path / "fresh")
    _git(ws / WORK_PREFIX / TITLE, "tag", "-f", f"best/60.00-{best[:7]}", best)
    assert _sweep(ws)["tag"] == []
    assert _sweep(ws)["totals"]["tag"] == 0


# ---------------------------------------------------------------- the closing window

def test_the_window_opens_window_min_before_the_end() -> None:
    end = datetime.datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    cfg = {"contest_end": end.isoformat(), "window_min": 90.0, "restore_boost": 3.0}
    inside = close_sweep.window_state(cfg, end - datetime.timedelta(minutes=30))
    assert inside["in_window"] is True and inside["minutes_left"] == 30.0
    assert inside["window_id"] == (end - datetime.timedelta(minutes=90)).isoformat()
    before = close_sweep.window_state(cfg, end - datetime.timedelta(minutes=200))
    assert before["in_window"] is False and before["starts_at"] == inside["starts_at"]
    after = close_sweep.window_state(cfg, end + datetime.timedelta(minutes=1))
    assert after["in_window"] is False
    assert close_sweep.window_state({"contest_end": None})["in_window"] is False


def test_closing_config_fills_the_end_from_the_contest_metadata(tmp_path: pathlib.Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(close_sweep, "read_parhelion_scheduler_config", lambda: {})
    ws, _, _, _ = _workspace(tmp_path)
    assert close_sweep.closing_config(ws)["contest_end"] is None
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    (ws / "results" / "scoring-evidence" / "contest-detail.json").write_text(
        json.dumps({"end_time": "2026-11-23T12:00:00.000Z"}))
    cfg = close_sweep.closing_config(ws)
    assert cfg["contest_end"] == "2026-11-23T12:00:00+00:00"
    assert cfg["contest_end_source"] == "contest-detail"
    assert (cfg["window_min"], cfg["restore_boost"], cfg["restore_min_gap"]) == (90.0, 3.0, 0.0)


def test_the_configured_end_wins_over_the_metadata(tmp_path: pathlib.Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(close_sweep, "read_parhelion_scheduler_config",
                        lambda: {"closing": {"contest_end": "2026-11-24T00:00:00Z", "window_min": 45,
                                             "restore_boost": 5}})
    ws, _, _, _ = _workspace(tmp_path)
    cfg = close_sweep.closing_config(ws)
    assert cfg["contest_end"] == "2026-11-24T00:00:00Z" and cfg["contest_end_source"] == "config"
    assert cfg["window_min"] == 45.0 and cfg["restore_boost"] == 5.0


def test_the_restore_priority_is_multiplied_only_inside_the_window(tmp_path: pathlib.Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from scheduler.tests.test_submit_intents import OTHER, _pass_row, _verdict

    receipts = [_pass_row("@cand", 60.0, when="2026-11-20T00:00:00Z"),
                _pass_row(OTHER, 50.0, when="2026-11-21T00:00:00Z", sid="sid-other")]
    _, _, plain = _verdict(tmp_path / "plain", intent="restore", board=50.0, active_sid="sid-other",
                           receipts=receipts)
    assert plain.approved and plain.status_tag == "READY"
    base = plain.priority_score
    end = (datetime.datetime.now(UTC) + datetime.timedelta(minutes=20)).isoformat()
    monkeypatch.setattr(close_sweep, "read_parhelion_scheduler_config",
                        lambda: {"closing": {"contest_end": end, "window_min": 90,
                                             "restore_boost": 3}})
    assert close_sweep.window_state(close_sweep.closing_config(tmp_path / "plain"))["in_window"] is True
    _, _, boosted = _verdict(tmp_path / "boost", intent="restore", board=50.0, active_sid="sid-other",
                             receipts=receipts)
    assert boosted.approved and boosted.priority_score == round(base * 3.0, 2)
    assert [s.passed for s in boosted.stages] == [s.passed for s in plain.stages]  # no stage moved


def test_the_window_message_is_sent_once_per_window(tmp_path: pathlib.Path) -> None:
    ws, _, _, _ = _workspace(tmp_path)
    doc = _sweep(ws)
    doc["closing"] = {"in_window": True, "window_id": "2026-11-23T10:30:00+00:00",
                      "contest_end": "2026-11-23T12:00:00+00:00", "minutes_left": 30.0}
    first = close_sweep.window_notice(ws, doc)
    assert first["sent"] is True and first["event_key"] == f"closing:{doc['closing']['window_id']}"
    again = close_sweep.window_notice(ws, doc)
    assert again["sent"] is False and "already sent" in again["reason"]
    from cli.opsb_core.agents.bus_store import AgentBusStore

    assert AgentBusStore(ws).has_message(first["event_key"]) is True
    outside = close_sweep.window_notice(ws, {**doc, "closing": {"in_window": False}})
    assert outside == {"sent": False, "reason": "outside the closing window"}


def test_the_window_message_names_the_close_view_numbers() -> None:
    text = close_sweep.window_message({
        "restore": [{"title": "A", "gap": 2.5}], "merge": [{"title": "B", "best_sha": "abc1234",
                                                           "branches": ["race"]}],
        "tag": [{"title": "C"}], "closing": {"contest_end": "2026-11-23T12:00:00+00:00",
                                             "minutes_left": 30.0},
        "totals": {"restore": 1, "restore_gap": 2.5, "restore_weighted_gap": 7.5}})
    assert "A -2.5" in text and "B abc1234 (race)" in text and "1 best/* tag(s) stale" in text
    assert "--enqueue" in text


# ---------------------------------------------------------------- per-slot utilisation

def test_per_slot_utilisation_is_reported_by_slot_number_only() -> None:
    rows = close_sweep.slot_rows([{"used_today": 50, "daily_limit": 50},
                                  {"used_today": 33, "daily_limit": 50}])
    assert rows == [{"slot": 1, "used": 50, "limit": 50, "utilization": 100.0},
                    {"slot": 2, "used": 33, "limit": 50, "utilization": 66.0}]
    assert not any("id" in row or "name" in row for row in rows)


def test_the_hint_fires_only_when_a_slot_is_under_used_and_the_day_is_half_over() -> None:
    rows = close_sweep.slot_rows([{"used_today": 50, "daily_limit": 50},
                                 {"used_today": 25, "daily_limit": 50}])
    hint = close_sweep.slot_notice(rows, 62.0)
    assert "slot 2 at 50%" in hint and "62% of the day gone" in hint
    assert "display only" in hint
    assert close_sweep.slot_notice(rows, 40.0) == ""  # the day is not half over yet
    busy = close_sweep.slot_rows([{"used_today": 40, "daily_limit": 50},
                                  {"used_today": 45, "daily_limit": 50}])
    assert [row["utilization"] for row in busy] == [80.0, 90.0]
    assert close_sweep.slot_notice(busy, 80.0) == ""


def test_day_elapsed_percent_follows_the_beijing_day() -> None:
    midday = datetime.datetime(2026, 11, 23, 4, 0, tzinfo=UTC)  # 12:00 Beijing
    assert close_sweep.day_elapsed_pct(midday) == 50.0
    assert close_sweep.day_elapsed_pct(datetime.datetime(2026, 11, 22, 16, 0, tzinfo=UTC)) == 0.0


def test_an_unregistered_account_id_is_not_credited_to_the_default_slot(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R5 §6: a slot id the registry does not know keeps its own bucket, not the default slot."""
    from scheduler.gate import GateConfig, SubmissionGate

    ws, best, board_sha, _ = _workspace(tmp_path)
    today = datetime.datetime.now(UTC).isoformat()
    rows = [{"title": TITLE, "sha": best, "status": "Pass", "score": 60.0, "submission_id": "g1",
             "submitted_at": today, "queued_at": today, "account_id": "ghost-slot"},
            {"title": TITLE, "sha": board_sha, "status": "Pass", "score": 55.0, "submission_id": "g2",
             "submitted_at": today, "queued_at": today, "account_id": "primary"}]
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr("scheduler.gate.list_eligible_account_slots",
                        lambda: [{"id": "primary", "user_id": "u-1"}])
    quota = SubmissionGate(ws, config=GateConfig()).get_quota_status()
    assert quota.per_slot == [{"slot": 1, "used": 1, "limit": 50, "utilization": 2.0}]
    assert quota.used_today == 2  # the ghost's spend is still counted, just not against a known slot
    assert quota.remaining_today == 48


def test_a_receipt_without_an_account_id_keeps_its_default_slot(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scheduler.gate import GateConfig, SubmissionGate

    ws, best, board_sha, _ = _workspace(tmp_path)
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(json.dumps(
        {"title": TITLE, "sha": best, "status": "Pass", "score": 60.0, "submission_id": "h1",
         "submitted_at": datetime.datetime.now(UTC).isoformat()}) + "\n", encoding="utf-8")
    monkeypatch.setattr("scheduler.gate.list_eligible_account_slots",
                        lambda: [{"id": "primary", "user_id": "u-1"}])
    quota = SubmissionGate(ws, config=GateConfig()).get_quota_status()
    assert quota.per_slot[0]["used"] == 1 and quota.used_today == 1
