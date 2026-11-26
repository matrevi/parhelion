"""Tests for the Judge public-contest scraper (no network: HTTP layer is faked)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.services import contest_scrape_service as scrape  # noqa: E402


class ContestScrapeTests(unittest.TestCase):
    def test_parse_contest_ref_accepts_links_ids_and_names(self) -> None:
        self.assertEqual(
            scrape.parse_contest_ref("https://judge.invalid/public/mat-chem-sim-pred"),
            {"base_url": "https://judge.invalid", "contest_name": "mat-chem-sim-pred", "contest_id": ""},
        )
        self.assertEqual(
            scrape.parse_contest_ref("https://judge.invalid/public/mat-chem-sim-pred/ranking?x=1")["contest_name"],
            "mat-chem-sim-pred",
        )
        self.assertEqual(
            scrape.parse_contest_ref("https://judge.invalid/op_challenge_jiangshan_prelim/ranking")["contest_name"],
            "ranking",
        )
        self.assertEqual(
            scrape.parse_contest_ref("6aeae3b61dac5d235bfef644"),
            {"base_url": "https://judge.invalid", "contest_name": "", "contest_id": "6aeae3b61dac5d235bfef644"},
        )
        self.assertEqual(scrape.parse_contest_ref("mat-chem-sim-pred")["contest_name"], "mat-chem-sim-pred")
        with self.assertRaises(scrape.ContestScrapeError):
            scrape.parse_contest_ref("   ")

    def test_fetch_resolves_group_contest_and_problems(self) -> None:
        calls: list[str] = []

        def fake_get(base: str, path: str, timeout: float):
            calls.append(path)
            if path == "/api/groups/public":
                return {"_id": "gid-1", "name": "public"}
            if path == "/api/contests/group/gid-1":
                return [
                    {"_id": "cid-other", "name": "other", "title": "Other"},
                    {"_id": "cid-1", "name": "mat-chem-sim-pred", "title": "第二届石油和化工行业人工智能创新大赛-算子赛道"},
                ]
            if path == "/api/problems/contest/cid-1":
                return [
                    {"_id": "p1", "name": "neighborlistpbcfixedk", "title": "NeighborListPbcFixedK", "difficulty": 5,
                     "kernel_pattern": "fused", "sdk_version": "9.0.0", "tags": ["科学计算"],
                     "ranking_submission_mode": "latest"},
                    {"_id": "p2", "name": "revinnorm", "title": "RevINNorm", "difficulty": 2},
                ]
            raise AssertionError(f"unexpected path {path}")

        with mock.patch.object(scrape, "_get_json", side_effect=fake_get):
            res = scrape.fetch_contest_problems("https://judge.invalid/public/mat-chem-sim-pred")

        self.assertEqual(calls, ["/api/groups/public", "/api/contests/group/gid-1", "/api/problems/contest/cid-1"])
        self.assertEqual(res["contest"]["id"], "cid-1")
        self.assertEqual(res["contest"]["problem_count"], 2)
        self.assertEqual([p["title"] for p in res["problems"]], ["NeighborListPbcFixedK", "RevINNorm"])
        first = res["problems"][0]
        self.assertEqual(first["difficulty"], 5)
        self.assertEqual(first["problem_id"], "p1")
        self.assertEqual(first["kernel_pattern"], "fused")
        self.assertEqual(first["ranking_submission_mode"], "latest")
        self.assertNotIn("ranking_submission_mode", res["problems"][1])
        self.assertTrue(first["problem_url"].endswith("/public/mat-chem-sim-pred/neighborlistpbcfixedk"))

    def test_unknown_contest_name_reports_available_names(self) -> None:
        def fake_get(base: str, path: str, timeout: float):
            if path == "/api/groups/public":
                return {"_id": "gid-1"}
            if path == "/api/contests/group/gid-1":
                return [{"_id": "c1", "name": "alpha"}, {"_id": "c2", "name": "beta"}]
            raise AssertionError(path)

        with mock.patch.object(scrape, "_get_json", side_effect=fake_get):
            with self.assertRaises(scrape.ContestScrapeError) as ctx:
                scrape.fetch_contest_problems("does-not-exist")
        self.assertIn("not found", str(ctx.exception))
        self.assertIn("alpha", str(ctx.exception))

    def test_empty_problem_list_is_an_error(self) -> None:
        with mock.patch.object(scrape, "_get_json", return_value=[]):
            with self.assertRaises(scrape.ContestScrapeError):
                scrape.fetch_contest_problems("6aeae3b61dac5d235bfef644")


if __name__ == "__main__":
    unittest.main()
