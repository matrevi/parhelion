"""4.3 P29: measured judge jitter, the two prediction tiers and the saturation transform."""
from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest

from scheduler import judge_noise, ranking, ranking_metrics
from scheduler.estimator import SIGMA_HIDDEN_JITTER, ScoreEstimator

A = "a1" * 20
B = "b2" * 20
C = "c3" * 20
D = "d4" * 20


def journal(workspace: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    """The receipt journal every reader in this module goes through."""
    path = workspace / "results" / "submit-queue-outcome.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def receipt(sid: str, title: str, sha: str, score: float, when: str, *, times: list[float] | None = None,
            tbest: list[float] | None = None, status: str = "Pass") -> dict:
    row = {"submission_id": sid, "title": title, "sha": sha, "status": status, "score": score,
           "submitted_at": when, "queued_at": when}
    if times is not None:
        row["public_times_us"] = times
        row["public_tbest_us"] = tbest or [1.0] * len(times)
    return row


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def commit(repo: pathlib.Path, files: dict[str, str], message: str) -> str:
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class TierTest(unittest.TestCase):
    """Same-generation retest versus cross-generation change, decided by the diff."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name) / "Op"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        self.base = commit(self.repo, {"op_kernel/add.cpp": "a", "README.md": "x"}, "first")
        self.docs = commit(self.repo, {"README.md": "y", "tools/bench.py": "print(1)"}, "docs and a script")
        self.kernel = commit(self.repo, {"op_kernel/add.cpp": "b"}, "faster kernel")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_same_sha_is_a_same_generation_retest(self) -> None:
        info = ranking.classify(self.repo, self.base, self.base)
        self.assertEqual(info["tier"], ranking.SAME_GEN)
        self.assertEqual(info["reason"], "same SHA")

    def test_a_kernel_source_change_makes_a_new_generation(self) -> None:
        info = ranking.classify(self.repo, self.kernel, self.docs)
        self.assertEqual(info["tier"], ranking.CROSS_GEN)
        self.assertEqual(info["kernel_files"], ["op_kernel/add.cpp"])
        self.assertEqual(info["files"], 1)

    def test_a_change_outside_kernel_source_stays_same_generation(self) -> None:
        info = ranking.classify(self.repo, self.docs, self.base)
        self.assertEqual(info["tier"], ranking.SAME_GEN)
        self.assertEqual(info["files"], 2)
        # a later commit that touches kernel source is cross-generation against the same baseline
        self.assertEqual(ranking.classify(self.repo, self.kernel, self.base)["files"], 3)
        self.assertEqual(info["kernel_files"], [])
        self.assertIn("none of them kernel source", info["reason"])

    def test_kernel_source_needs_both_the_directory_and_a_compiled_suffix(self) -> None:
        self.assertTrue(ranking.kernel_source("op_kernel/a.cpp"))
        self.assertTrue(ranking.kernel_source("op_host/a.h"))
        self.assertFalse(ranking.kernel_source("op_kernel/notes.md"))
        self.assertFalse(ranking.kernel_source("docs/op_kernel/a.cpp"))
        self.assertFalse(ranking.kernel_source("tools/a.py"))

    def test_identical_trees_are_a_same_generation_retest(self) -> None:
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
            "--allow-empty", "-m", "empty")
        empty = git(self.repo, "rev-parse", "HEAD")
        info = ranking.classify(self.repo, empty, self.kernel)
        self.assertEqual(info["tier"], ranking.SAME_GEN)
        self.assertEqual(info["reason"], "identical trees")

    def test_a_failure_to_measure_counts_as_a_new_generation(self) -> None:
        for repo, sha, base in ((None, self.kernel, self.base),
                                (self.repo, self.kernel, "f" * 40),
                                (self.repo, "not-a-sha", self.base),
                                (pathlib.Path("/nonexistent"), self.kernel, self.base),
                                (self.repo, self.kernel, None)):
            info = ranking.classify(repo, sha, base)
            self.assertEqual(info["tier"], ranking.CROSS_GEN, (repo, sha, base))
            self.assertFalse(info["kernel_files"])


class JitterTest(unittest.TestCase):
    """sigma per problem, then the contest pool, then the 4.2.1 constant."""

    def test_the_fallback_constant_is_the_estimator_constant(self) -> None:
        self.assertEqual(ranking.SIGMA_FALLBACK, SIGMA_HIDDEN_JITTER)

    def test_problem_then_contest_then_constant(self) -> None:
        noisy = [receipt(str(n), "Noisy", A, score, f"2026-11-20T0{n}:00:00Z")
                 for n, score in enumerate((50.0, 53.0, 47.0, 51.0), start=1)]
        quiet = [receipt("q1", "Quiet", B, 60.0, "2026-11-20T01:00:00Z")]
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            journal(ws, noisy + quiet)
            own = ranking.jitter_sigma(ws, "Noisy")
            self.assertEqual(own["source"], "judge_noise:problem")
            self.assertGreater(own["df"], judge_noise.MIN_DF - 1)
            pooled = ranking.jitter_sigma(ws, "Quiet")  # too few repeated judgments of its own
            self.assertEqual(pooled["source"], "judge_noise:contest")
            self.assertAlmostEqual(pooled["sigma"], own["sigma"], places=3)
        with tempfile.TemporaryDirectory() as td:  # nothing measured at all
            empty = ranking.jitter_sigma(td, "Anything")
        self.assertEqual(empty["source"], "constant")
        self.assertEqual(empty["sigma"], SIGMA_HIDDEN_JITTER)

    def test_the_estimator_prices_the_sigma_it_measures(self) -> None:
        rows = [receipt(str(n), "Op", A, score, f"2026-11-20T0{n}:00:00Z")
                for n, score in enumerate((50.0, 53.0, 47.0, 51.0), start=1)]
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            journal(ws, rows)
            estimator = ScoreEstimator(ws)
            measured = estimator.estimate("Op", A)
            constant = estimator.estimate("Op", A, jitter_from_noise=False)
            sigma = ranking.jitter_sigma(ws, "Op")["sigma"]
            self.assertEqual(measured.ranking["jitter"]["source"], "judge_noise:problem")
            self.assertEqual(constant.ranking["jitter"]["source"], "constant")
            self.assertLess(abs(measured.confidence_sigma / constant.confidence_sigma
                                - sigma / SIGMA_HIDDEN_JITTER), 0.01)  # both sigmas are rounded
            self.assertNotAlmostEqual(measured.confidence_sigma, constant.confidence_sigma, places=2)


class SaturationTest(unittest.TestCase):
    """The judge formula, applied per case to a real per-case distribution."""

    def doc(self, times: list[float], tbest: list[float] | None = None) -> dict:
        best = tbest or [1.0] * len(times)
        return {"times": {str(i): t for i, t in enumerate(times)},
                "tbest": {str(i): b for i, b in enumerate(best)}}

    def test_a_speedup_of_one_returns_the_anchor(self) -> None:
        doc = self.doc([1.4, 2.0, 1.1, 3.0, 1.6])
        for anchor in (10.0, 42.0, 63.5, 88.0):
            got = ranking.saturate(doc, 1.0, anchor)["saturated_score"]
            self.assertAlmostEqual(got, anchor, places=6)

    def test_the_mapping_is_monotone_in_the_speedup(self) -> None:
        for doc in (self.doc([1.4, 2.0, 1.1, 3.0, 1.6]), self.doc([1.05, 1.1, 1.2, 1.05]),
                    self.doc([1.8, 2.4, 1.2, 4.0, 2.2])):
            scores = [ranking.saturate(doc, k, 55.0)["saturated_score"]
                      for k in (0.5, 0.75, 1.0, 1.1, 1.25, 1.5, 2.0, 4.0, 10.0)]
            self.assertEqual(scores, sorted(scores))
            self.assertTrue(all(0.0 <= s <= 100.0 for s in scores))
            self.assertGreaterEqual(scores[-1], scores[3])

    def test_the_boundaries_are_the_judge_bounds(self) -> None:
        doc = self.doc([1.2, 1.5, 1.1])
        self.assertLessEqual(ranking.saturate(doc, 20.0, 60.0)["saturated_score"], 100.0)
        self.assertLess(ranking.saturate(doc, 0.05, 60.0)["saturated_score"], 20.0)
        # a distribution already at tbest has no headroom, however large the speedup
        self.assertEqual(ranking.saturate(self.doc([1.0, 1.0, 1.0]), 10.0, 100.0)["saturated_score"], 100.0)
        # a score of zero anchors nothing: the adjustment refuses to transform it at all
        refused = ranking.adjust(tempfile.mkdtemp(), "Op", None, A, B, 0.0, 2.0, 5.0)
        self.assertFalse(refused["applied"])
        self.assertEqual(refused["estimated_score"], 5.0)

    def test_the_level_shift_reproduces_any_anchor(self) -> None:
        doc = self.doc([1.4, 2.0, 1.1, 3.0, 1.6])
        for anchor in (12.0, 40.0, 77.0):
            shift = ranking.level_shift(doc, anchor)
            self.assertAlmostEqual(ranking._score(doc["times"], doc["tbest"], shift), anchor, places=4)


class ShrinkTest(unittest.TestCase):
    """The coefficient is fitted on this problem's receipts, and only the ones before as_of."""

    def rows(self) -> list[dict]:
        return [
            receipt("1", "Op", A, 40.0, "2026-11-20T01:00:00Z", times=[2.0, 2.0, 2.0], tbest=[1.0, 1.0, 1.0]),
            receipt("2", "Op", B, 42.0, "2026-11-20T02:00:00Z", times=[1.8, 1.8, 1.8], tbest=[1.0, 1.0, 1.0]),
            receipt("3", "Op", C, 43.0, "2026-11-20T03:00:00Z", times=[1.7, 1.7, 1.7], tbest=[1.0, 1.0, 1.0]),
        ]

    def test_pairs_carry_a_saturated_prediction_and_a_realized_gain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            journal(ws, self.rows())
            pairs = ranking.fit_pairs(ws, "Op")
            self.assertEqual(len(pairs), 2)
            predicted, actual = pairs[0]
            self.assertGreater(predicted, 0.0)  # 2.0 -> 1.8 us is a speedup the judge should pay for
            self.assertAlmostEqual(actual, 2.0, places=4)  # 40 -> 42 official points
            # a failed shot is a prediction problem for S1, not a ranking sample
            failed = self.rows() + [receipt("4", "Op", D, 0.0, "2026-11-20T04:00:00Z", status="Wrong Answer")]
            journal(ws, failed)
            self.assertEqual(len(ranking.fit_pairs(ws, "Op")), 2)

    def test_nothing_after_as_of_is_read(self) -> None:
        future = receipt("9", "Op", D, 90.0, "2026-11-20T09:00:00Z",
                         times=[0.4, 0.4, 0.4], tbest=[1.0, 1.0, 1.0])
        from scheduler.estimator import _parse_iso

        as_of = _parse_iso("2026-11-20T05:00:00Z")
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            journal(ws, self.rows())
            before_pairs = ranking.fit_pairs(ws, "Op", as_of)
            before_fit = ranking.shrinkage(ws, "Op", as_of)
            before_pooled = ranking.fit_pairs(ws, None, as_of)
            journal(ws, self.rows() + [future])  # a receipt the prediction at as_of cannot know about
            self.assertEqual(ranking.fit_pairs(ws, "Op", as_of), before_pairs)
            self.assertEqual(ranking.shrinkage(ws, "Op", as_of), before_fit)
            self.assertEqual(ranking.fit_pairs(ws, None, as_of), before_pooled)
            # without as_of the future receipt is visible, which is what "no future data" forbids
            self.assertEqual(len(ranking.fit_pairs(ws, "Op")), 3)

    def test_one_pair_is_not_a_fit(self) -> None:
        """A problem speaks for itself only after ``MIN_PAIRS`` pairs; before that it borrows the pool."""
        other = [receipt("7", "Other", A, 30.0, "2026-11-20T01:00:00Z", times=[2.0] * 3, tbest=[1.0] * 3),
                 receipt("8", "Other", B, 33.0, "2026-11-20T02:00:00Z", times=[1.8] * 3, tbest=[1.0] * 3),
                 receipt("9", "Other", C, 34.0, "2026-11-20T03:00:00Z", times=[1.7] * 3, tbest=[1.0] * 3)]
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            journal(ws, self.rows()[:2] + other)  # "Op" has one pair, "Other" has two
            self.assertEqual(len(ranking.fit_pairs(ws, "Op")), 1)
            self.assertEqual(ranking.shrinkage(ws, "Op")["source"], "contest")
            journal(ws, self.rows() + other)  # two pairs of its own
            fit = ranking.shrinkage(ws, "Op")
            self.assertEqual(fit["source"], "problem")
            self.assertEqual(fit["pairs"], 2)
        with tempfile.TemporaryDirectory() as td:  # a single pair anywhere is still no fit
            ws = pathlib.Path(td)
            journal(ws, self.rows()[:2])
            self.assertEqual(ranking.shrinkage(ws, "Op")["source"], "prior")

    def test_a_problem_without_pairs_falls_back_to_the_pool_then_to_the_prior(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            journal(ws, self.rows())
            own = ranking.shrinkage(ws, "Op")
            self.assertEqual(own["source"], "problem")
            self.assertTrue(0.0 <= own["c"] <= 1.0)  # never amplifies a gain
            other = ranking.shrinkage(ws, "NoReceipts")
            self.assertEqual(other["source"], "contest")
            self.assertGreaterEqual(other["c"], ranking.SHRINK_PRIOR)
        with tempfile.TemporaryDirectory() as td:
            empty = ranking.shrinkage(td, "Op")
        self.assertEqual(empty["source"], "prior")
        self.assertEqual(empty["c"], ranking.SHRINK_PRIOR)

    def test_a_pair_that_predicted_nothing_cannot_set_the_slope(self) -> None:
        """Two receipts with almost the same times: the ridge must keep the slope sane."""
        flat = [receipt("1", "Op", A, 40.0, "2026-11-20T01:00:00Z", times=[2.0, 2.0, 2.0], tbest=[1.0] * 3),
                receipt("2", "Op", B, 39.84, "2026-11-20T02:00:00Z", times=[1.99, 1.99, 1.99], tbest=[1.0] * 3)]
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            journal(ws, flat)
            pairs = ranking.fit_pairs(ws, "Op")
            self.assertEqual(len(pairs), 1)
            self.assertLess(abs(pairs[0][0]), 0.5)  # the transform predicted almost no gain
            # the ridge keeps the slope near zero instead of the -52 an unregularised one produces
            self.assertGreater(ranking._slope(pairs), -1.0)
            self.assertEqual(ranking.shrinkage(ws, "Op")["source"], "prior")  # one pair is no fit

    def test_the_adjustment_only_ever_reduces_the_gain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            journal(ws, self.rows())
            up = ranking.adjust(ws, "Op", None, B, A, 40.0, 1.4, 6.0)
            self.assertEqual(up["tier"], ranking.CROSS_GEN)
            self.assertTrue(up["applied"])
            self.assertLessEqual(up["gain"], 6.0)
            self.assertLessEqual(up["estimated_score"], 46.0)
            down = ranking.adjust(ws, "Op", None, B, A, 40.0, 0.7, -6.0)
            self.assertGreaterEqual(down["gain"], -6.0)
            same = ranking.adjust(ws, "Op", None, A, A, 40.0, 1.4, 6.0)
            self.assertFalse(same["applied"])
            self.assertEqual(same["estimated_score"], 46.0)


class MetricsTest(unittest.TestCase):
    """Spearman, the top-1 hit rate and the gain error on hand-built replay shots."""

    def shot(self, title: str, ts: float, priority: float, predicted: float, gain: float,
             *, status: str = "Pass", approved: bool = True) -> dict:
        return {"title": title, "ts": ts, "approved": approved, "status": status,
                "score": 50.0 + gain, "board_score": 50.0, "priority": priority,
                "predicted_gain": predicted}

    def test_ranks_and_spearman(self) -> None:
        self.assertEqual(ranking_metrics.ranks([3.0, 1.0, 1.0, 2.0]), [4.0, 1.5, 1.5, 3.0])
        self.assertEqual(ranking_metrics.spearman([1, 2, 3], [1, 2, 3]), 1.0)
        self.assertEqual(ranking_metrics.spearman([1, 2, 3], [3, 2, 1]), -1.0)
        self.assertIsNone(ranking_metrics.spearman([1, 2, 3], [4, 4, 4]))  # no order to agree on
        self.assertIsNone(ranking_metrics.spearman([1], [1]))

    def test_rows_keep_only_rankable_shots(self) -> None:
        shots = [self.shot("OpA", 0.0, 5.0, 1.0, 2.0),
                 self.shot("OpA", 0.0, 5.0, 1.0, 2.0, approved=False),
                 {**self.shot("OpA", 0.0, 5.0, 1.0, 2.0), "score": None}]
        rows = ranking_metrics.rows(shots)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gain"], 2.0)

    def test_moments_top1_and_the_error_bands(self) -> None:
        shots = []
        for bucket in range(3):
            base = bucket * ranking_metrics.MOMENT_SECONDS
            shots += [self.shot("OpA", base + 1, 10.0, 4.0, 3.0),   # highest priority, best gain
                      self.shot("OpB", base + 2, 1.0, 1.0, 0.0),
                      self.shot("OpC", base + 3, 5.0, 1.0, 1.0)]
        block = ranking_metrics.evaluate(shots, rounds=200)
        self.assertEqual(block["shots"]["ranked"], 9)
        self.assertEqual(block["shots"]["problems"], 3)
        self.assertEqual(block["spearman"]["value"], 1.0)  # priority order matches the gains
        self.assertEqual(block["spearman_moment"]["value"], 1.0)
        self.assertEqual(block["spearman_moment"]["moments"], 3)
        self.assertEqual(block["top1"]["value"], 1.0)
        self.assertEqual(block["top1"]["n"], 3)
        self.assertEqual(block["top1"]["moments"], 3)
        self.assertEqual(block["top1"]["hits"], 3)
        self.assertEqual(block["error"]["n"], 9)
        self.assertAlmostEqual(block["error"]["mean"], round(2.0 / 3.0, 4), places=4)
        self.assertAlmostEqual(block["error"]["median"], 1.0, places=4)
        self.assertEqual([b["band"] for b in block["error"]["bands"]], ["0-2", "2-5"])
        for key in ("spearman", "spearman_predicted", "spearman_moment", "top1"):
            self.assertIsNotNone(block[key]["ci95"], key)
            low, high = block[key]["ci95"]
            self.assertLessEqual(low, high)

    def test_a_single_candidate_moment_contributes_no_spearman(self) -> None:
        shots = [self.shot("OpA", 0.0, 3.0, 1.0, 1.0), self.shot("OpA", 5000.0, 4.0, 1.0, 2.0)]
        block = ranking_metrics.evaluate(shots, rounds=50)
        self.assertIsNone(block["spearman_moment"]["value"])
        self.assertIsNone(block["top1"]["value"])
        self.assertEqual(block["spearman_moment"]["moments"], 0)
        self.assertIsNotNone(block["spearman"]["value"])  # the per-shot fallback still works

    def test_compare_and_the_enable_rule(self) -> None:
        current = {"spearman": {"value": 0.2, "ci95": [0.1, 0.3]},
                   "spearman_moment": {"value": None, "ci95": None},
                   "top1": {"value": 0.4, "ci95": [0.3, 0.5]}}
        better = {"spearman": {"value": 0.4, "ci95": [0.25, 0.55]},
                  "spearman_moment": {"value": None, "ci95": None},
                  "top1": {"value": 0.4, "ci95": [0.25, 0.55]}}
        worse = {"spearman": {"value": 0.3, "ci95": [0.05, 0.5]},
                 "spearman_moment": {"value": None, "ci95": None},
                 "top1": {"value": 0.4, "ci95": [0.3, 0.5]}}
        self.assertTrue(ranking_metrics.compare(current, better)["spearman"]["beats"])
        self.assertFalse(ranking_metrics.compare(current, worse)["spearman"]["beats"])
        self.assertTrue(ranking_metrics.verdict({"a": ranking_metrics.compare(current, better),
                                                 "b": ranking_metrics.compare(current, better)})["enabled"])
        # the same metric must clear the bar in every contest
        self.assertFalse(ranking_metrics.verdict({"a": ranking_metrics.compare(current, better),
                                                  "b": ranking_metrics.compare(current, worse)})["enabled"])
        self.assertFalse(ranking_metrics.verdict({})["enabled"])


if __name__ == "__main__":
    unittest.main()
