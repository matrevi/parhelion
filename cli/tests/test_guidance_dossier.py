"""4.4 P31: the gap triage numbers and the dossier files (code-built, ignored by git, never submitted).

Temporary repositories and a temporary PARHELION_HOME only; no daemon, device or real contest data.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
ROOT = CLI_DIR.parent
for p in (str(CLI_DIR), str(ROOT), str(ROOT / "runtime" / "judge")):
    if p not in sys.path:
        sys.path.insert(0, p)

from opsb_core.guidance import dossier, gap, settings  # noqa: E402
from opsb_core.ledger import store  # noqa: E402

S = settings({})


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def test_points_match_the_judge_formula_and_the_tiers_say_where_the_points_are():
    from scheduler import receipts
    for t, b in ((20.0, 10.0), (4000.0, 10.0), (5.0, 10.0)):
        assert gap.points(t, b) == pytest.approx(receipts.case_points(t, b))
    rows = [{"case": "#01", "ours_us": 4000.0, "tbest_us": 10.0},   # 400x
            {"case": "#02", "ours_us": 20.0, "tbest_us": 10.0},     # 2x
            {"case": "#03", "ours_us": 100.0, "tbest_us": 10.0},    # 10x
            {"case": "#04", "ours_us": None, "tbest_us": 10.0}]
    doc = gap.triage(rows, 5, S)
    cases = {c["case"]: c for c in doc["cases"]}
    assert doc["n"] == 3 and doc["untimed"] == 1
    assert cases["#01"]["tier"] == "algorithm" and cases["#02"]["tier"] == "tuning" and cases["#03"]["tier"] == "datapath"
    # logarithmic scoring: a 2x at 400x is worth far less than a 2x at 2x tbest
    assert cases["#01"]["gain2_w"] * 20 < cases["#02"]["gain2_w"]
    assert cases["#01"]["gain100_w"] > cases["#01"]["gain10_w"] > cases["#01"]["gain2_w"]
    assert cases["#02"]["gain2_w"] == pytest.approx((100.0 - gap.points(20.0, 10.0)) * 5 / 3, abs=0.01)
    assert doc["cases"][0]["case"] == "#01"  # largest headroom first
    assert doc["focus"] in doc["tiers"] and "weighted pts" in gap.summary_line(doc)


@pytest.fixture()
def worktree(tmp_path):
    repo = tmp_path / "Op"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        _git(repo, *args)
    (repo / "kernel.asc").write_text("// kernel\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_dossier_is_ignored_by_git_and_rewrites_only_changed_files(worktree):
    # no repository .gitignore at all: the dossier directory ignores itself
    changed = dossier.write(worktree, {"index.md": "a\n", "gap.md": "b\n"})
    assert sorted(changed) == ["gap.md", "index.md"]
    assert (dossier.path_of(worktree) / ".gitignore").read_text() == "*\n"
    assert _git(worktree, "status", "--porcelain").strip() == ""
    _git(worktree, "add", "-A")
    assert _git(worktree, "diff", "--cached", "--name-only").strip() == ""
    assert dossier.write(worktree, {"index.md": "a\n", "gap.md": "c\n"}) == ["gap.md"]


def test_dossier_never_lands_in_a_submission(worktree):
    import project_files
    dossier.write(worktree, {"index.md": "x\n", "gap.md": "y\n"})
    template = {"files": [{"path": "kernel.asc", "content": "// kernel\n"}]}
    files = project_files.collect_kernel_files(worktree, template, "pid")
    assert [f["path"] for f in files] == ["kernel.asc"]


def _rows(ws: Path) -> list[dict]:
    me = store.Ledger(ws, "Op", "dee-aaaaaa")
    mate = store.Ledger(ws, "Op", "dee-bbbbbb")
    me.append("insight", {"claim": "case 04 cost is the scalar re-interleave", "actionable_next": "stub it"},
              source="worker")
    me.append("hypothesis", {"claim": "fuse the three passes", "priority": 1, "test_plan": "standard"},
              source="worker")
    me.append("failure", {"claim": "double buffer on B", "approach": "double buffer",
                          "why_failed": "no change", "retry_conditions": "after tiling"}, source="worker")
    me.append("experiment", {"claim": "abc WA", "outcome": "WA", "why": "case 05 WA"})
    mate.append("insight", {"claim": "board 1185, attack outliers"}, source="peer")
    me.append("constraint", {"claim": "worker boundary: platform_state"})  # platform row: not a finding
    return store.read_problem(ws, "Op")


def test_findings_put_the_agents_own_discoveries_first_and_dead_ends_list_refutations(tmp_path):
    rows = _rows(tmp_path)
    text = dossier.render_findings("Op", rows)
    assert text.index("Unexploited insights") < text.index("Open hypotheses")
    assert "scalar re-interleave" in text and "fuse the three passes" in text
    assert "unverified (teammate)" in text and "worker boundary" not in text
    dead = dossier.render_dead_ends("Op", rows)
    assert "double buffer on B" in dead and "retry if after tiling" in dead and "WA: abc WA" in dead


def _entry(eid, pillar, status, *, online=0, local=1, problem="", category="tiling-regime", retracted=False):
    return {"id": eid, "pillar": pillar, "status": status, "title": f"title {eid}", "summary": "summary",
            "category": category, "problem": problem, "confidence": 0.7, "retracted": retracted,
            "evidence_composition": {"local": local, "paired": 0, "online_receipt": online},
            "evidence_strong": bool(online), "delta_weighted": 3.0 if pillar == "breakthrough" else None}


def test_hardware_and_prior_art_order_evidence_and_citability():
    entries = [_entry("HW-u", "hardware", "unverified"), _entry("HW-v", "hardware", "verified", online=1),
               _entry("HW-a", "hardware", "anti-pattern"), _entry("HW-r", "hardware", "verified", retracted=True),
               _entry("BT-own", "breakthrough", "verified", online=1, problem="Op"),
               _entry("BT-other", "breakthrough", "verified", online=1, problem="Other", category="dma-coalescing")]
    firm, pending = dossier.rank_hardware("Op", entries, set(), "datapath")
    assert {e["id"] for e in firm} == {"HW-v", "HW-a"} and [e["id"] for e in pending] == ["HW-u"]
    hw = dossier.render_hardware("Op", (firm, pending))
    assert "HW-r" not in hw and "online_receipt 1" in hw and hw.index("HW-v") < hw.index("Pending validation")
    own, rest = dossier.rank_prior("Op", entries, set(), "datapath")
    assert [e["id"] for e in own] == ["BT-own"] and rest[0]["id"] == "BT-other"
    assert "1. `BT-own`" in dossier.render_prior_art("Op", (own, rest), "datapath")
    assert dossier.citable(own[0]) and not dossier.citable(firm[1] if firm[1]["id"] == "HW-a" else firm[0])


def test_index_is_at_most_one_kilobyte_and_names_every_file():
    text = dossier.render_index("Op", "x" * 5000, "2026-11-26T21:00:00+00:00")
    assert len(text.encode("utf-8")) <= dossier.INDEX_MAX
    assert all(name in text for name in ("gap.md", "findings.md", "dead-ends.md", "hardware.md", "prior-art.md"))


def test_monitor_writes_the_dossier_into_the_seat_worktree_only(tmp_path, monkeypatch):
    from opsb_core.guidance import monitor
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    ws = (tmp_path / "contest").resolve()
    repo = ws / "Op"
    repo.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        _git(repo, *args)
    (repo / "kernel.asc").write_text("// k\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (ws / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title": "Op", "path": "Op"}]}))
    assert monitor.valid_worktree(ws, "Op", str(ws)) is None  # never the contest root
    assert monitor.valid_worktree(ws, "Op", str(repo)) == repo.resolve()
    _rows(ws)
    import datetime
    ctx = monitor.Context(ws, S, datetime.datetime.now(datetime.timezone.utc))
    monkeypatch.setattr(monitor, "kb_entries", lambda: [_entry("HW-v", "hardware", "verified", online=1)])
    triage, meta = ctx.gap("Op")
    changed = monitor.refresh_dossier(ctx, "Op", "dee-aaaaaa", repo.resolve(), triage, meta, "plateau")
    assert set(changed) == set(dossier.FILES)
    assert "scalar re-interleave" in (dossier.path_of(repo) / "findings.md").read_text()
    assert monitor.refresh_dossier(ctx, "Op", "dee-aaaaaa", repo.resolve(), triage, meta, "plateau") == []
