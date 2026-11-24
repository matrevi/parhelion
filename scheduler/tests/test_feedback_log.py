"""Feedback log: append-only JSONL event capture for the live contest run."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

SUP_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(SUP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUP_ROOT))

from scheduler.feedback_log import LOG_TEXT_LIMIT, feedback_log_path, log_feedback, tail_feedback  # noqa: E402


class FeedbackLogTests(unittest.TestCase):
    def test_append_and_tail_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(log_feedback(td, "gate", title="OpA", status="PROBE", approved=True))
            self.assertTrue(log_feedback(td, "submit_error", title="OpA", detail="HTTP 400"))
            rows = tail_feedback(td)
            self.assertEqual([r["kind"] for r in rows], ["gate", "submit_error"])
            self.assertEqual(rows[0]["title"], "OpA")
            self.assertTrue(rows[0]["ts"])
            self.assertEqual(len(tail_feedback(td, kind="gate")), 1)
            self.assertTrue(feedback_log_path(td).is_file())

    def test_long_fields_are_clipped_and_writes_never_raise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_feedback(td, "runtime", log="x" * (LOG_TEXT_LIMIT * 2))
            row = tail_feedback(td)[-1]
            self.assertLessEqual(len(row["log"]), LOG_TEXT_LIMIT + len("…[truncated]"))
            self.assertTrue(log_feedback(td, "ok"))
            self.assertFalse(feedback_log_path(td).is_dir())

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(tail_feedback(td), [])


if __name__ == "__main__":
    unittest.main()
