"""4.2 C (P05): the checkpoint keeps the stage tail; handoff checks; ACP and Codex compaction; replay seed.

Fixtures and helpers come from ``test_ledger``. Fake adapters and clients only.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.ledger import checkpoint, handoff, store, summary  # noqa: E402
from test_ledger import WORKER, _git, contest  # noqa: E402,F401  (``contest`` is a fixture)

# ---------------------------------------------------------------- checkpoint (#8)


def test_checkpoint_keeps_the_end_of_the_stage_result():
    card = "STATE Op00 @ 10:00\nbest: 72.0 abc1234 | head: def5678\nrules: device work only via parhelion_test."
    stage = "OLD-START " + "filler line\n" * 1200 + "CONCLUSION: next experiment is L-0042"
    out = checkpoint.build(card, stage, 8000)
    assert len(out) <= 8000 and out.startswith(card)
    assert "CONCLUSION: next experiment is L-0042" in out and "OLD-START" not in out
    assert out.endswith(checkpoint.CHECKPOINT_TAIL)
    assert checkpoint.MARK in out  # the cut is marked
    short = checkpoint.build(card, "all done", 8000)
    assert short == "\n".join([card, "all done", checkpoint.CHECKPOINT_TAIL])
    assert checkpoint.build(card, "", 8000) == "\n".join([card, checkpoint.CHECKPOINT_TAIL])
    tight = checkpoint.build(card, "z" * 5000, 3000)
    assert len(tight) <= 3000 and tight.count("z") > 2000


def test_seed_budget_config_is_clamped():
    assert checkpoint.seed_budget({}) == 8000
    assert checkpoint.seed_budget({"agents": {"seed_budget_chars": 6000}}) == 6000
    assert checkpoint.seed_budget({"agents": {"seed_budget_chars": 10}}) == checkpoint.SEED_BUDGET_MIN
    assert checkpoint.seed_budget({"agents": {"seed_budget_chars": 10**9}}) == checkpoint.SEED_BUDGET_MAX
    assert checkpoint.seed_budget({"agents": {"seed_budget_chars": "many"}}) == 8000
    with mock.patch("opsb_core.config.ConfigRepository.load", return_value={"agents": {"seed_budget_chars": 5000}}):
        assert checkpoint.seed_budget() == 5000


def test_runner_checkpoint_follows_the_seed_budget(tmp_path):
    from opsb_core.agents.manager import _WorkerRunner
    adapter = mock.Mock()
    adapter.workspace = tmp_path
    runner = _WorkerRunner.__new__(_WorkerRunner)
    runner.adapter, runner.agent_id = adapter, "dee-aaaaaa"
    runner._stage_result = "start " + "m" * 9000 + " THE-END"
    with mock.patch("opsb_core.config.ConfigRepository.load", return_value={"agents": {"seed_budget_chars": 4000}}):
        cp = runner._checkpoint({"problem": "Op00"}, "")
    assert len(cp) <= 4000 and "THE-END" in cp and "start " not in cp and cp.startswith("STATE Op00")


# ---------------------------------------------------------------- handoff checks (#6)

NARRATIVE = """## Where I am
Tiled the inner loop; head is {sha7}.
## In the middle of
Waiting on run {rid}.
## Uncommitted changes and paths
Edited src/kernel.cpp and src/missing_file.cpp.
## Next experiment
{ref}
## Not yet in the ledger
Decided to reject double buffering because the kernel is DMA bound.
"""


def _session(contest, agent=WORKER):
    return handoff.HandoffSession(contest["ws"], "Op00", agent, contest["repo"], "deepseek")


def _seed_ledger(contest):
    led = store.Ledger(contest["ws"], "Op00", WORKER)
    h1 = led.append("hypothesis", {"claim": "swap tile order on case 05", "priority": 1, "sha": contest["second"]},
                    source="worker")
    h2 = led.append("hypothesis", {"claim": "prefetch the B tile", "priority": 4}, source="worker")
    led.append("hypothesis", {"claim": "old idea", "status": "refuted"}, source="worker")
    ins = led.append("insight", {"claim": "case 03 is DMA bound", "cases": ["03"]}, source="worker")
    led.append("failure", {"claim": "double buffering did not help", "approach": "double-buffer A",
                           "why_failed": "DMA bound on B", "retry_conditions": "B resident in L1"}, source="worker")
    return led, h1, h2, ins


def test_narrative_headings_are_parsed_and_clipped():
    text = NARRATIVE.format(sha7="abc1234", rid="r" * 4, ref="L-0001")
    assert handoff.missing_headings(text) == []
    parts = handoff.sections(text)
    assert parts["Next experiment"] == "L-0001" and parts["Where I am"].startswith("Tiled")
    assert handoff.missing_headings("## Where I am\nonly this") == list(handoff.HEADINGS[1:])
    assert handoff.missing_headings("**Where I am**: x\n1. In the middle of\nUncommitted changes and paths:\n"
                                    "# Next experiment\n- Not yet in the ledger") == []
    assert "Where I am" not in handoff.sections("Where I am going next is the tile swap.")  # prose, not a heading
    long = "\n".join(f"## {h}\n" + ("x" * 800) for h in handoff.HEADINGS)
    clipped = handoff.clip_narrative(long)
    assert len(clipped) <= handoff.NARRATIVE_MAX + 50 and handoff.missing_headings(clipped) == []


def test_references_are_verified_against_git_files_and_the_registry(contest):
    from opsb_core.services import test_runs
    real_rid, fake_rid = "7" * 32, "6" * 32
    test_runs._write(contest["ws"], {"request_id": real_rid, "run_id": "9" * 32, "problem": "Op00", "status": "finished"})
    text = (f"head {contest['second'][:7]} and 0badf00d99, runs {real_rid} {fake_rid} {'9' * 32}; "
            "files src/kernel.cpp src/missing_file.cpp ../outside.py /etc/hosts.txt; ratio 1234567 and effaced.")
    out, bad = handoff.verify_refs(text, worktree=contest["repo"], workspace=contest["ws"])
    assert bad == {"sha": ["0badf00d99"], "path": ["src/missing_file.cpp", "../outside.py", "/etc/hosts.txt"],
                   "test_run": [fake_rid]}
    assert f"{contest['second'][:7]} and" in out and "0badf00d99 [unverified]" in out
    assert f"{real_rid} {fake_rid} [unverified] {'9' * 32}" in out  # the registry knows the run id too
    assert "src/kernel.cpp src/missing_file.cpp [unverified]" in out and "1234567 and effaced." in out


def test_carry_forward_restores_dropped_decisions():
    previous = ("## Where I am\nTiling.\n## Not yet in the ledger\n"
                "- Decided to reject double buffering because the kernel is DMA bound on case 07.\n"
                "- Tuned the prefetch distance to four.\n- WA on case 12 at L-0007 is a dead end for fp16 rounding.")
    now = "## Where I am\nDouble buffering was rejected, DMA bound on case 07.\n## Next experiment\nL-0003"
    # the first decision is restated, the second names a ledger id the summary has, the third is not a decision
    assert handoff.carry_forward(previous, now, "ledger summary lists L-0007 already") == []
    lost = handoff.carry_forward(previous, "## Where I am\nnothing relevant", "")
    assert len(lost) == 2 and lost[0].startswith("Decided to reject double buffering")
    assert any("dead end" in item for item in lost) and not any("Tuned" in item for item in lost)


def test_summary_names_every_open_hypothesis_and_insight_even_when_long(contest):
    led, h1, h2, ins = _seed_ledger(contest)
    for i in range(80):
        led.append("hypothesis", {"claim": f"variant {i} " + "w" * 200, "priority": 3}, source="worker")
        led.append("insight", {"claim": f"insight {i} " + "v" * 200, "cases": ["05"]}, source="worker")
    rows = led.rows()
    for budget in (50_000, 3000, 500):
        text = summary.render(rows, max_chars=budget, own=WORKER)
        assert summary.ids_missing(text, rows, WORKER) == []
    assert h1["id"] in text and "L-0003" not in text.split("Unexploited")[0]  # a refuted idea is not open
    block = _session(contest).summary_block()
    assert summary.ids_missing(block, rows, WORKER) == [] and block.count("\n- ") > 160


def test_validated_narrative_gets_marks_summary_and_carry_forward(contest):
    led, h1, h2, ins = _seed_ledger(contest)
    session = _session(contest)
    first = NARRATIVE.format(sha7=contest["second"][:7], rid="9" * 32, ref=h1["id"]).replace(
        "Decided to reject double buffering because the kernel is DMA bound.",
        "Decided to abandon the swizzle layout because bank conflicts doubled on case 09.")
    out = session.build_seed(first, "CHECKPOINT-BODY")
    assert out.status == "ok" and out.findings["next_experiment_ref"] is True
    assert out.text.startswith("Preserve this checkpoint and await the next task.\n## Where I am")
    assert "src/missing_file.cpp [unverified]" in out.text and "9" * 32 + " [unverified]" in out.text
    assert out.findings["unverified"]["path"] == ["src/missing_file.cpp"]
    for entry in (h1, h2, ins):
        assert entry["id"] in out.text.split("LEDGER SUMMARY")[1]
    assert out.text.rstrip().endswith("Last reported result:\nCHECKPOINT-BODY")
    rows = [json.loads(line) for line in session.log_path.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["narrative"] == first and rows[0]["status"] == "ok"
    # the next handoff forgets the decision: it is carried forward from the previous narrative
    second = NARRATIVE.format(sha7=contest["second"][:7], rid="9" * 32, ref=h2["id"]).replace(
        "Decided to reject double buffering because the kernel is DMA bound.", "Nothing new.")
    again = session.build_seed(second, "CP")
    assert again.findings["carried_forward"] == 1
    assert "Carried forward from the previous handoff:\n- Decided to abandon the swizzle layout" in again.text
    assert len(session.log_path.read_text().splitlines()) == 2
    assert session.previous_narrative().startswith("## Where I am")


def test_missing_headings_get_one_repair_then_fall_back_to_the_ledger_only_seed(contest):
    led, h1, h2, ins = _seed_ledger(contest)
    session = _session(contest)
    prompts = []
    good = NARRATIVE.format(sha7=contest["second"][:7], rid="1" * 32, ref=h1["id"])

    def repair_ok(prompt):
        prompts.append(prompt)
        return good

    fixed = session.build_seed("I did some tiling and will continue.", "CP", repair=repair_ok)
    assert fixed.status == "repaired" and len(prompts) == 1 and "Where I am" in prompts[0]
    assert fixed.findings["repaired_headings"] == list(handoff.HEADINGS)

    def repair_bad(prompt):
        prompts.append(prompt)
        return "still no headings"

    (contest["repo"] / "src/kernel.cpp").write_text("int main() { return 2; }\n")  # a dirty worktree for the fallback
    out = session.build_seed("free text", "CP", repair=repair_bad)
    assert len(prompts) == 2 and out.status == "ledger_only"
    assert out.findings["missing_headings"] == list(handoff.HEADINGS)
    for part in ("rules: device work only via parhelion_test", "STATE Op00", "LEDGER SUMMARY", h1["id"], h2["id"],
                 ins["id"], "git status:", "kernel.cpp", "git diff --stat:"):
        assert part in out.text, part
    assert "free text" not in out.text and "Last reported result" not in out.text
    broken = session.build_seed("free text", "CP", repair=mock.Mock(side_effect=RuntimeError("boom")))
    assert broken.status == "ledger_only" and "boom" in broken.findings["repair_error"]
    assert session.build_seed("free text", "CP").status == "ledger_only"  # no repair channel: straight to the fallback
    last = [json.loads(line) for line in session.log_path.read_text().splitlines()][-1]
    assert last["status"] == "ledger_only" and last["narrative"] == "free text"


# ---------------------------------------------------------------- adapters


class FakeAcpClient:
    def __init__(self, replies):
        self.replies, self.calls, self.sessions = list(replies), [], 0

    def send_prompt(self, session_id, prompt):
        self.calls.append((session_id, prompt))
        return {"text": self.replies.pop(0) if self.replies else "Ready."}

    def open_session(self, **kwargs):
        self.sessions += 1
        return f"new-{self.sessions}", {}


def _acp(contest, tmp_path, replies, with_session=True):
    from opsb_core.agents.adapters import AcpWorkerAdapter
    adapter = AcpWorkerAdapter(contest["ws"], contest["repo"], "deepseek", WORKER, tmp_path / "log.jsonl")
    adapter._client, adapter.native_session_id = FakeAcpClient(replies), "old-sid"
    if with_session:
        adapter.handoff = _session(contest)
    return adapter


def test_acp_compact_sends_the_structured_prompt_and_seeds_a_validated_handoff(contest, tmp_path):
    led, h1, h2, ins = _seed_ledger(contest)
    good = NARRATIVE.format(sha7=contest["second"][:7], rid="2" * 32, ref=h1["id"])
    adapter = _acp(contest, tmp_path, [good])
    assert "validated" in adapter.compact("CP-BODY")
    (old, prompt), (new, seed) = adapter._client.calls
    assert old == "old-sid" and prompt == handoff.HANDOFF_PROMPT and prompt.startswith("Write a structured handoff")
    assert all(f"## {h}" in prompt for h in handoff.HEADINGS) and "1500" in prompt
    assert new == "new-1" and adapter.native_session_id == "new-1"
    assert seed.startswith("Preserve this checkpoint and await the next task.") and h2["id"] in seed
    assert "2" * 32 + " [unverified]" in seed and seed.endswith("CP-BODY")


def test_acp_compact_repairs_in_the_old_session_then_falls_back(contest, tmp_path):
    led, h1, h2, ins = _seed_ledger(contest)
    adapter = _acp(contest, tmp_path, ["no headings at all", "still none"])
    adapter.compact("CP-BODY")
    calls = adapter._client.calls
    assert [c[0] for c in calls] == ["old-sid", "old-sid", "new-1"]  # the repair round used the old context
    assert "missing the heading" in calls[1][1]
    seed = calls[2][1]
    assert "could not be validated" in seed and h1["id"] in seed and "no headings at all" not in seed


def test_acp_compact_without_a_session_keeps_the_plain_seed(contest, tmp_path):
    adapter = _acp(contest, tmp_path, ["a plain narrative"], with_session=False)
    adapter.compact("CP-BODY")
    assert adapter._client.calls[1][1] == ("Preserve this checkpoint and await the next task.\na plain narrative"
                                           "\nLast reported result:\nCP-BODY")


def test_a_validator_fault_does_not_cost_the_worker_its_compaction(contest, tmp_path):
    from opsb_core.agents.adapters import CodexWorkerAdapter
    broken = mock.Mock()
    broken.build_seed.side_effect = RuntimeError("validator bug")
    broken.ledger_seed.side_effect = RuntimeError("validator bug")
    adapter = _acp(contest, tmp_path, ["a plain narrative"], with_session=False)
    adapter.handoff = broken
    assert "stage handoff" in adapter.compact("CP-BODY")
    assert adapter._client.calls[1][1].endswith("a plain narrative\nLast reported result:\nCP-BODY")
    codex = CodexWorkerAdapter(contest["ws"], contest["repo"], "codex", WORKER, tmp_path / "log3.jsonl")
    codex._client, codex.handoff = mock.Mock(), broken
    codex.compact("CP-BODY")
    assert codex._client.start_turn.call_args.args[1].endswith("\nCP-BODY")


def test_codex_compact_prefixes_the_next_turn_with_the_ledger_summary(contest, tmp_path):
    from opsb_core.agents.adapters import CodexWorkerAdapter
    led, h1, h2, ins = _seed_ledger(contest)
    adapter = CodexWorkerAdapter(contest["ws"], contest["repo"], "codex", WORKER, tmp_path / "log.jsonl")
    adapter._client = mock.Mock()
    adapter.native_session_id = "thread-1"
    adapter.handoff = _session(contest)
    adapter.compact("CP-BODY")
    adapter._client.compact.assert_called_once_with("thread-1")
    prompt = adapter._client.start_turn.call_args.args[1]
    assert prompt.startswith("Preserve this checkpoint and await the next task.\nLEDGER SUMMARY")
    assert prompt.index(h1["id"]) < prompt.index("Last reported result:\nCP-BODY") and ins["id"] in prompt
    log = json.loads(adapter.handoff.log_path.read_text().splitlines()[-1])
    assert log["kind"] == "codex" and log["seed_chars"] == len(prompt)
    bare = CodexWorkerAdapter(contest["ws"], contest["repo"], "codex", "dee-bbbbbb", tmp_path / "log2.jsonl")
    bare._client = mock.Mock()
    bare.compact("CP-BODY")
    assert bare._client.start_turn.call_args.args[1] == "Preserve this checkpoint and await the next task.\nCP-BODY"


def test_runner_hands_the_adapter_a_handoff_session_before_compacting(contest):
    from opsb_core.agents.manager import _WorkerRunner
    adapter = mock.Mock()
    adapter.workspace, adapter.worktree, adapter.driver_kind = contest["ws"], contest["repo"], "deepseek"
    seen = {}

    def compact(cp):
        seen["session"] = adapter.handoff
        return "compacted"

    adapter.compact.side_effect = compact
    runner = _WorkerRunner.__new__(_WorkerRunner)
    runner.adapter, runner.agent_id, runner.store = adapter, WORKER, mock.Mock()
    runner._stage_result, runner._context_percent, runner._context_tokens = "stage", None, 5
    runner._on_notify, runner._stop_flag, runner.squad_turn = (lambda: None), threading.Event(), False
    with mock.patch("opsb_core.config.ConfigRepository.load", return_value={}):
        runner._compact({"problem": "Op00"}, "reply")
    session = seen["session"]
    assert isinstance(session, handoff.HandoffSession) and session.problem == "Op00" and session.agent_id == WORKER
