"""Reconcile: judge history is the only truth for board scores."""
from __future__ import annotations

import pathlib
import sys
import unittest

CLI_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.services.submit_reconcile import build_boards, prune_queue  # noqa: E402


class ReconcileTests(unittest.TestCase):
    def test_board_uses_judge_history_and_flags_virtual_rows(self) -> None:
        problems = [{"title": "OpA"}, {"title": "OpB"}]
        histories = {
            "OpA": [
                {"_id": "s1", "status": "Wrong Answer", "score": 0, "create_time": "t1"},
                {"_id": "s2", "status": "Pass", "score": 26.8, "create_time": "t2"},
            ],
            "OpB": [],
        }
        receipts = {"s2": {"title": "OpA", "sha": "abc1234", "account_id": "primary"}}
        board, virtual = build_boards(problems, histories, receipts)
        self.assertEqual(len(board), 1)
        row = board[0]
        self.assertTrue(row["passed"])
        self.assertEqual(row["best_submission_id"], "s2")
        self.assertEqual(row["best_sha"], "abc1234")
        self.assertEqual(virtual[0]["title"], "OpB")

    def test_prune_keeps_one_head_sha_row_per_problem(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            (ws / "results").mkdir()
            rows = [
                {"title": "OpA", "sha": "old1111"},
                {"title": "OpA", "sha": "head222"},
                {"title": "OpA", "sha": "head222"},
                {"title": "OpB", "sha": "head333"},
            ]
            (ws / "results" / "submit-queue.jsonl").write_text(
                "".join(__import__("json").dumps(r) + "\n" for r in rows), encoding="utf-8"
            )
            res = prune_queue(ws, {"OpA": "head222aaaa", "OpB": "head333bbbb"})
            self.assertEqual(res["before"], 4)
            self.assertEqual(len(res["kept"]), 2)
            self.assertEqual({r["title"] for r in res["kept"]}, {"OpA", "OpB"})


if __name__ == "__main__":
    unittest.main()
