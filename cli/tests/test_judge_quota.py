"""Judge-side quota cache beats local receipt counting."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

CLI_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.services.judge_quota import beijing_date, read_quota, write_quota  # noqa: E402
from opsb_core.services.submit_runner import compute_account_slots  # noqa: E402


class JudgeQuotaTests(unittest.TestCase):
    def test_write_then_read_same_day(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            self.assertTrue(write_quota(ws, used=12, source="test"))
            doc = read_quota(ws)
            self.assertEqual(doc["used"], 12)
            self.assertEqual(doc["remaining"], 38)
            self.assertEqual(doc["beijing_date"], beijing_date())

    def test_stale_day_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            path = ws / "results" / ".judge-quota.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"beijing_date":"2000-01-01","used":50,"limit":50}', encoding="utf-8")
            self.assertIsNone(read_quota(ws))

    def test_slots_use_judge_count_over_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td) / "ws"
            (ws / "results").mkdir(parents=True)
            write_quota(ws, used=12)
            slots = compute_account_slots(ws)
            primary = next(s for s in slots if s["id"] == "primary")
            self.assertEqual(primary["used_today"], 12)


if __name__ == "__main__":
    unittest.main()
