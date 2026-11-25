import json
from unittest.mock import Mock

from opsb_core.services import board_refresh_service as board
from opsb_core.services import ranking_service as ranking


def test_workspace_rebind_rejects_other_boards(tmp_path, monkeypatch):
    manifest = tmp_path / "opsb.workspace.json"
    manifest.write_text(json.dumps({"contest_id": "chem", "contest_url": "https://judge.invalid/public/chem"}))
    cache = ranking._cache_path(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"contest_id": "f" * 24, "team_name": "old team",
                                 "score": 1683.80, "rank": 3, "total": 801}))
    assert ranking.team_ranking(tmp_path, allow_fetch=False) is None
    get = Mock(side_effect=[{"_id": "g"}, [{"name": "chem", "_id": "e" * 24,
                                             "signup_status": {"team_name": "new team"}}],
                            [{"team": {"team_name": "new team"}, "score": 73}]])
    monkeypatch.setattr(ranking, "_get_json", get)
    result = ranking.team_ranking(tmp_path, force=True)
    assert (result["rank"], result["total"], result["score"]) == (1, 1, 73)
    assert get.call_args.args == ("/api/submissions/contest/" + "e" * 24 + "/stats",)
    get.side_effect = OSError("offline")
    assert ranking.team_ranking(tmp_path, force=True)["score"] == 73
    for target in ({"contest_id": "chem", "team_name": "different team"},
                   {"contest_id": "different-contest"},
                   {"contest_url": "https://judge.invalid/another-group/chem"}):
        manifest.write_text(json.dumps(target))
        assert ranking.team_ranking(tmp_path, force=True) is None


def test_late_result_does_not_overwrite_rebound_workspace(tmp_path, monkeypatch):
    manifest = tmp_path / "opsb.workspace.json"
    manifest.write_text(json.dumps({"contest_id": "a" * 24, "team_name": "one"}))
    def finish_after_switch(path):
        assert path.endswith("/" + "a" * 24 + "/stats")
        manifest.write_text(json.dumps({"contest_id": "b" * 24, "team_name": "two"}))
        return [{"team": {"team_name": "one"}, "score": 24}]
    monkeypatch.setattr(ranking, "_get_json", finish_after_switch)
    assert ranking.team_ranking(tmp_path) is None
    assert not ranking._cache_path(tmp_path).exists()


def test_captain_switch_and_registration_changes(tmp_path, monkeypatch):
    cid = "a" * 24
    (tmp_path / "opsb.workspace.json").write_text(json.dumps({"contest_id": cid, "team_name": "old"}))
    accounts = []
    for slot in ("one", "two"):
        session = tmp_path / slot
        session.write_text(json.dumps({"user_id": slot, "cookies": [{"name": "sid", "value": slot}]}))
        accounts.append({"id": slot, "logged_in": True, "enabled": False, "user_id": slot,
                         "session_file": str(session), "base_url": "https://example.test"})
    monkeypatch.setattr(ranking, "list_judge_accounts", lambda: accounts)
    monkeypatch.setattr(ranking, "refresh_async", Mock())
    teams = {"one": "old-id", "two": "new-id"}

    def fetch(base, cookie, path):
        if path == f"/api/contests/{cid}/registration":
            team_id = teams[cookie.removeprefix("sid=")]
            return {"application": {"team_name": "same", "team_id": team_id, "status": "active"} if team_id else None}
        assert path == f"/api/submissions/contest/{cid}/stats"
        return [{"team": {"_id": team_id, "team_name": "same"}, "score": score}
                for team_id, score in [("old-id", 70), ("new-id", 40)]]

    monkeypatch.setattr(ranking, "fetch_json_with_cookie", fetch)
    ranking.set_captain(tmp_path, "one")
    assert ranking.team_ranking(tmp_path, force=True)["rank"] == 1
    old_scope = board.board_scope(tmp_path)
    ranking.set_captain(tmp_path, "two")
    assert ranking.team_ranking(tmp_path, allow_fetch=False) is None
    assert ranking.team_ranking(tmp_path, force=True)["rank"] == 2
    assert board.board_scope(tmp_path) != old_scope
    teams["two"] = "old-id"
    assert ranking.team_ranking(tmp_path, force=True)["rank"] == 1
    teams["two"] = None
    assert ranking.team_ranking(tmp_path, force=True)["ranking_state"] == "no_team"
    accounts[1]["logged_in"] = False
    assert ranking.team_ranking(tmp_path, force=True)["ranking_state"] == "unavailable"
