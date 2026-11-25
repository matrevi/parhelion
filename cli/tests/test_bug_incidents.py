import json

from opsb_core.knowledge.bugs import annotate_bug, sync_bugs
from opsb_core.knowledge.query import list_kb_entries
from opsb_core.services.contest_service import judge_case_count


def test_receipt_replay_and_update(tmp_path,monkeypatch):
    monkeypatch.setenv("PARHELION_HOME",str(tmp_path/"home"))
    (tmp_path/"opsb.workspace.json").write_text(json.dumps({"problems":[{"title":"A"}]}))
    results = tmp_path/"results"
    results.mkdir()
    receipt = dict(title="A",sha="a"*40,status="Wrong Answer",submission_id="r1",
                   cases_passed=2,cases_total=7)
    ledger = results/"submit-queue-outcome.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in
        [receipt,receipt,{**receipt,"submission_id":"r2","status":"sync_error"}]))
    assert sync_bugs(tmp_path)["count"] == 1
    card = list_kb_entries("bugs",auto_seed=False)["entries"][0]
    assert len(card["evidence"]) == 1 and "2/7" in card["summary"]
    annotate_bug(card["id"],"Check the empty input")
    sync_bugs(tmp_path)
    assert list_kb_entries("bugs",auto_seed=False)["entries"][0]["lesson"]=="Check the empty input"
    ledger.write_text(json.dumps({**receipt,"status":"Pass","score":21}))
    assert sync_bugs(tmp_path)["count"] == 0


def test_visible_cases_do_not_determine_judge_count():
    partial = {"testcases":[{},{}]}
    assert judge_case_count(partial,{},None) is None
    assert judge_case_count(partial,{"total_testcase_count":23},None)==23
    assert judge_case_count(partial,{},7)==7
