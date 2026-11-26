"""P08: the worker start-up pack (4.2 G), its sections, its byte budget and its wiring."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from opsb_core.drivers import startup_pack as sp
from opsb_core.drivers.brief import MAX_BRIEF_BYTES, compose_contest_subagent_brief, compose_session_brief


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=p08@example.invalid", "-c", "user.name=P08",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A contest workspace inside ``tmp_path`` with an isolated Parhelion home."""
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "phome"))
    monkeypatch.setenv("PARHELION_DISABLE_DAEMON_PROXY", "1")
    ws = (tmp_path / "contest").resolve()
    ws.mkdir()
    return ws, None


@pytest.fixture()
def problem(env):
    """A contest workspace with one problem repository and a template commit."""
    ws, _ = env
    (ws / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title": "DemoOp"}]}), encoding="utf-8")
    repo = ws / "DemoOp"
    repo.mkdir()
    (repo / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
    (repo / "run.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (repo / "kernel.asc").write_text("\n".join(f"__aicore__ void stage{i}(int x) {{" for i in range(40)) + "\n",
                                     encoding="utf-8")
    (repo / "main.asc").write_text("int main() { return 0; }\n", encoding="utf-8")
    (repo / "bench").mkdir()
    (repo / "bench" / "reference.py").write_text("def ref(x):\n    return x\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "template baseline")
    kernel = repo / "kernel.asc"
    lines = kernel.read_text(encoding="utf-8").splitlines()
    lines[4] = "__aicore__ void stage4_fast(int x) {"
    lines[5] = "  int y = x + 1;"
    kernel.write_text("\n".join(lines) + "\n", encoding="utf-8")
    new_file = repo / "op_kernel"
    new_file.mkdir()
    (new_file / "detail.asc").write_text("__aicore__ void Detail(int x) {\n}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "optimize stages")
    return ws, repo


def test_sections_carry_contract_path_workdir_and_budget(problem):
    ws, repo = problem
    contract = "\n".join([f"# DemoOp · worker", "Goal: raise the score."] + ["Loop: parhelion_test ..."] * 40)
    pack = sp.build(ws, "DemoOp", contract=contract, worktree=repo, branch="sprint-solo",
                    driver="deepseek", tokens=6000)
    text = pack["text"]
    assert pack["bytes"] == len(text.encode("utf-8"))
    assert pack["bytes"] <= pack["budget_bytes"] == sp.budget_bytes(6000)
    assert pack["tokens"] == pytest.approx(pack["bytes"] / sp.BYTES_PER_TOKEN, rel=1e-3)
    # 1. the contract section never exceeds the old brief cap
    assert len(pack["sections"]["Task contract"]["text"].encode("utf-8")) <= MAX_BRIEF_BYTES
    # 2. the absolute worktree path and the workdir rule
    assert str(repo.resolve()) in text
    assert "workdir" in text and "never begin a command with `cd`" in text
    # 4. the files to change: kernel.asc with a line range, plus the main.asc entry file
    assert "kernel.asc" in text and "lines 5-6" in text
    assert "main.asc" in text and "entry file" in text
    assert "## 5." in text  # the ledger section is present (empty ledger is stated, not omitted)
    assert "no rows yet" in text and "STATE DemoOp" in text


def test_workdir_guidance_is_per_driver(problem):
    ws, repo = problem
    dsh = sp.section_worktree(repo, "t", "deepseek")
    codex = sp.section_worktree(repo, "t", "codex")
    claude = sp.section_worktree(repo, "t", "claude")
    assert "`bash` takes `workdir`" in dsh
    assert "`exec_command / shell` takes `workdir`" in codex
    assert "`Bash` has no `workdir` argument" in claude
    for text in (dsh, codex, claude):
        assert str(repo.resolve()) in text
        assert "cd" in text


def test_fit_truncates_in_order_5_3_4_and_never_the_contract():
    sections = {1: "c" * 400, 2: "p" * 400, 3: "m" * 4000, 4: "f" * 4000, 5: "l" * 4000}
    fitted, cuts = sp.fit(dict(sections), 3000)
    assert fitted[1] == sections[1] and fitted[2] == sections[2]
    order = [c["section"] for c in cuts]
    assert order and order[0] == 5, order
    # section 5 is exhausted before 3 is touched, and 3 before 4
    first_three = order.index(3) if 3 in order else len(order)
    first_four = order.index(4) if 4 in order else len(order)
    assert all(s == 5 for s in order[:first_three]), order
    assert all(s == 3 for s in order[first_three:first_four]), order
    assert sum(len(v.encode("utf-8")) for v in fitted.values()) <= 3000
    assert "cut to" in fitted[5] or fitted[5] == ""


def test_truncation_counts_utf8_bytes_not_characters():
    # 3000 CJK characters are ~9000 bytes: a byte budget must cut them, a character budget would not.
    sections = {1: "契约" * 50, 2: "路径" * 50, 3: "图" * 3000, 4: "", 5: "账" * 3000}
    fitted, cuts = sp.fit(dict(sections), 6000)
    total = sum(len(v.encode("utf-8")) for v in fitted.values())
    assert total <= 6000
    assert cuts and cuts[0]["section"] == 5
    for value in fitted.values():
        assert "\ufffd" not in value  # never cut a multi-byte character in half


def test_repo_map_uses_aoci_f_fields_when_present(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "aoci.txt").write_text(
        "#AOCI-ROOT-MANIFEST: 1\n#Volume: id=code kind=code path=aoci.code.txt format=object-fras-v2\n",
        encoding="utf-8")
    (wt / "aoci.code.txt").write_text(
        "kernel.asc[K=Kernel,C=9]: F:Vectorized stage loop over the tile grid. | R:bench/reference.py | A:opsb test | S:aligned\n"
        "main.asc[K=Entry,C=7]: F:Official template entry point; never edited. | R:kernel.asc | A:- | S:-\n",
        encoding="utf-8")
    mapped = sp.repo_map(wt)
    assert mapped["source"] == "aoci"
    assert "Vectorized stage loop" in mapped["text"]
    assert "Official template entry point" in mapped["text"]
    assert "K=Kernel" not in mapped["text"]  # only the F field survives


def test_repo_map_falls_back_to_code_scan(tmp_path):
    wt = tmp_path / "wt"
    (wt / "op_kernel").mkdir(parents=True)
    (wt / "op_kernel" / "copy.asc").write_text("__aicore__ void CopyIn(int x) {\n}\n", encoding="utf-8")
    (wt / "bench").mkdir()
    (wt / "bench" / "reference.py").write_text("def ref(x):\n    return x\n\nclass Bucket:\n    pass\n",
                                               encoding="utf-8")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "junk.py").write_text("def hidden():\n    pass\n", encoding="utf-8")
    mapped = sp.repo_map(wt)
    assert mapped["source"] == "code"
    assert "op_kernel/copy.asc" in mapped["text"] and "CopyIn" in mapped["text"]
    assert "bench/reference.py" in mapped["text"] and "ref" in mapped["text"] and "Bucket" in mapped["text"]
    assert "__pycache__" not in mapped["text"]
    disabled = sp.section_repo_map(wt, enabled=False)
    assert disabled["text"] == "" and disabled["source"] == "disabled"


def test_change_targets_derives_files_and_hunk_ranges(problem):
    _, repo = problem
    targets = sp.change_targets(repo)
    paths = [f["path"] for f in targets["files"]]
    assert "kernel.asc" in paths and "op_kernel/detail.asc" in paths
    assert "main.asc" in paths and next(f for f in targets["files"] if f["path"] == "main.asc")["entry"]
    kernel = next(f for f in targets["files"] if f["path"] == "kernel.asc")
    assert kernel["hunks"] == [[5, 6]]
    assert targets["base"] and targets["best"] and targets["base"] != targets["best"]
    text = sp.section_files(targets)
    assert "lines 5-6" in text
    assert "entry file" in text


def test_ledger_section_uses_digest_and_state_card(problem):
    ws, repo = problem
    from opsb_core.ledger import store as ledger_store
    ledger_store.Ledger(ws, "DemoOp", "dee-1").append(
        "hypothesis", {"claim": "Vectorize the tile loop", "priority": 2, "status": "open",
                       "predicted_effect": {"mte2": "-20%"}})
    section = sp.section_ledger(ws, "DemoOp", "dee-1")
    assert "Vectorize the tile loop" in section
    assert "Per-case evidence" in section or "case gap" in section


def test_session_brief_can_carry_the_pack_and_keeps_its_contract(problem):
    ws, repo = problem
    brief = compose_session_brief(ws, "DemoOp", goal="Cut latency", worktree=repo, branch="sprint-solo",
                                  include_startup_pack=True, driver="deepseek")
    assert brief.startswith("# STARTUP PACK")
    contract, rest = brief.split("## 2. Worktree", 1)
    assert len(contract.split("## 1. Task contract", 1)[1].encode("utf-8")) <= MAX_BRIEF_BYTES + 64
    assert "STATE DemoOp" in brief  # the platform card stays inside section 5
    assert "parhelion_squad" in contract and "parhelion_test" in contract
    assert str(repo.resolve()) in rest


def test_orchestrator_brief_does_not_treat_quota_as_a_gate(problem):
    ws, _ = problem
    brief = compose_contest_subagent_brief(ws)
    assert "quota is not a gate" in brief.lower()
    assert "quota gates" not in brief
    assert "parhelion_squad" in brief  # the P14 envelope line survives


def test_pack_size_enters_the_p03_meter(problem):
    """The pack size is a metered row so `opsb replay metrics` counts it with the tool bytes."""
    ws, repo = problem
    from opsb_core.metrics import mcp_meter
    pack = sp.build(ws, "DemoOp", contract="contract", worktree=repo, driver="deepseek", tokens=6000)
    assert sp.record_size(ws, pack, agent_id="dee-1") is True
    rows = [r for r in mcp_meter.read_rows(ws) if r.get("tool") == "startup_pack"]
    assert rows and rows[-1]["agent_id"] == "dee-1"
    assert rows[-1]["result_bytes"] == len(json.dumps({"bytes": pack["bytes"], "tokens": pack["tokens"],
                                                       "budget_bytes": pack["budget_bytes"],
                                                       "sections": {k: v["bytes"] for k, v in pack["sections"].items()},
                                                       "repo_map": pack["repo_map_source"]},
                                                      separators=(",", ":")).encode("utf-8"))
