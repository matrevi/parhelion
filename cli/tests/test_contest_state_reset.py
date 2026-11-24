"""Re-initialising a folder for a new contest must not inherit the old leaderboard state."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

CLI_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.services.contest_state_service import existing_contest_id, reset_contest_state  # noqa: E402


class ContestStateResetTests(unittest.TestCase):
    def test_reads_contest_id_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            self.assertEqual(existing_contest_id(ws), "")
            (ws / "opsb.workspace.json").write_text(json.dumps({"contest_id": "mat-chem"}), encoding="utf-8")
            self.assertEqual(existing_contest_id(ws), "mat-chem")

    def test_reset_archives_scores_queue_and_board(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            (ws / "results" / "scoring-evidence").mkdir(parents=True)
            (ws / "results" / "score-snapshot.json").write_text(json.dumps({"ConcatD": {"score": 60}}), encoding="utf-8")
            (ws / "results" / "submit-queue.jsonl").write_text('{"title":"ConcatD"}\n', encoding="utf-8")
            (ws / "results" / "scoring-evidence" / "board-summary.json").write_text("{}", encoding="utf-8")
            res = reset_contest_state(ws, "mat-chem")
            self.assertEqual(res["contest_id"], "mat-chem")
            self.assertTrue((ws / "results" / "score-snapshot.json").exists() is False)
            self.assertFalse((ws / "results" / "submit-queue.jsonl").exists())
            archives = list((ws / "results").glob("_archive-*"))
            self.assertEqual(len(archives), 1)
            self.assertTrue((archives[0] / "score-snapshot.json").is_file())
            self.assertTrue((archives[0] / "scoring-evidence" / "board-summary.json").is_file())


if __name__ == "__main__":
    unittest.main()
