"""Feedback log: append-only JSONL event capture for the live contest run."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SUP_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(SUP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUP_ROOT))

from scheduler import feedback_log  # noqa: E402
from scheduler.feedback_log import (  # noqa: E402
    LOG_TEXT_LIMIT,
    feedback_log_path,
    feedback_signatures_path,
    log_feedback,
    log_feedback_on_change,
    tail_feedback,
)


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


class FeedbackOnChangeTests(unittest.TestCase):
    def test_same_signature_logs_once_and_change_logs_again(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(log_feedback_on_change(td, "gate", "OpA:abc1234", "sig-1", title="OpA", status="HOLD"))
            self.assertFalse(log_feedback_on_change(td, "gate", "OpA:abc1234", "sig-1", title="OpA", status="HOLD"))
            self.assertEqual(len(tail_feedback(td, kind="gate")), 1)
            self.assertTrue(log_feedback_on_change(td, "gate", "OpA:abc1234", "sig-2", title="OpA", status="PROBE"))
            rows = tail_feedback(td, kind="gate")
            self.assertEqual([r["status"] for r in rows], ["HOLD", "PROBE"])
            state = json.loads(feedback_signatures_path(td).read_text(encoding="utf-8"))
            self.assertEqual(state, {"gate": {"OpA:abc1234": "sig-2"}})
            self.assertTrue(feedback_signatures_path(td).with_name(".feedback-signatures.json.lock").exists())

    def test_keys_and_kinds_are_tracked_independently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(log_feedback_on_change(td, "gate", "OpA:1", "s"))
            self.assertTrue(log_feedback_on_change(td, "gate", "OpB:1", "s"))
            self.assertTrue(log_feedback_on_change(td, "probe", "OpA:1", "s"))
            self.assertFalse(log_feedback_on_change(td, "gate", "OpB:1", "s"))
            self.assertEqual(len(tail_feedback(td)), 3)

    def test_failed_write_does_not_record_signature(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(feedback_log, "log_feedback", return_value=False):
                self.assertFalse(log_feedback_on_change(td, "gate", "OpA:1", "s"))
            self.assertTrue(log_feedback_on_change(td, "gate", "OpA:1", "s"))


class FeedbackRotationTests(unittest.TestCase):
    def test_oversized_log_rotates_to_dot_one(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(feedback_log, "LOG_ROTATE_BYTES", 200):
            path = feedback_log_path(td)
            rotated = path.with_name("feedback-log.1.jsonl")
            for i in range(4):
                log_feedback(td, "gate", n=i, pad="x" * 60)
            self.assertTrue(rotated.is_file())
            self.assertLessEqual(path.stat().st_size, 400)
            first_rotation = rotated.read_text(encoding="utf-8")
            for i in range(4, 10):
                log_feedback(td, "gate", n=i, pad="x" * 60)
            # `.1` is overwritten by the newest rotation, never accumulated.
            self.assertNotEqual(rotated.read_text(encoding="utf-8"), first_rotation)
            self.assertEqual(tail_feedback(td)[-1]["n"], 9)
            self.assertFalse(path.with_name("feedback-log.2.jsonl").exists())

    def test_small_log_is_not_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_feedback(td, "gate", n=1)
            log_feedback(td, "gate", n=2)
            self.assertFalse(feedback_log_path(td).with_name("feedback-log.1.jsonl").exists())
            self.assertEqual([r["n"] for r in tail_feedback(td)], [1, 2])


if __name__ == "__main__":
    unittest.main()
