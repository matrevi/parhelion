"""Worker worktrees share their contest's platform state."""
from cli.opsb_core import config
from scheduler import parhelion_paths


def test_worktree_paths_map_to_the_contest_workspace(tmp_path):
    contest = tmp_path / "contest"
    worktree = contest / "Op" / ".worktrees" / "sprint-solo"
    worktree.mkdir(parents=True)
    (contest / "opsb.workspace.json").write_text("{}")
    for module in (config, parhelion_paths):
        assert module.workspace_id(worktree) == module.workspace_id(contest)
        assert module.canonical_workspace(worktree) == contest.resolve()
    stray = tmp_path / "other" / ".worktrees" / "x"
    stray.mkdir(parents=True)
    assert parhelion_paths.canonical_workspace(stray) == stray.resolve()
