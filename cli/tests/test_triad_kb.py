"""Unit & CLI tests for Parhelion v3 Track 2 (Self-Evolving Triad Knowledge Base)."""
from __future__ import annotations

import argparse
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.commands.kb_cmd import register_kb_subcommands  # noqa: E402
from opsb_core.services.triad_kb_service import (  # noqa: E402
    compute_confidence,
    ensure_triad_kb_seeded,
    format_prompt_cache_digest,
    get_kb_entry,
    get_kb_evolution_history,
    list_kb_entries,
    project_readonly_skills,
    promote_entry,
    query_kb,
    record_kb_entry,
    record_kb_feedback,
    revert_event,
    rollback_last,
)


def _run_kb_cli(argv: list[str]) -> tuple[int, str, str]:
    parser = argparse.ArgumentParser(prog="opsb")
    sub = parser.add_subparsers(dest="cmd", required=True)
    register_kb_subcommands(sub)
    args = parser.parse_args(argv)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = args.func(args)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class TriadKBTests(unittest.TestCase):
    def test_seed_is_idempotent_and_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                first = ensure_triad_kb_seeded()
                self.assertTrue(first["ok"])
                self.assertEqual(set(first["seeded_pillars"]), {"snippets", "hardware", "community"})
                self.assertGreaterEqual(first["copied_count"], 18)

                snips = list_kb_entries(pillar="snippets")
                hws = list_kb_entries(pillar="hardware")
                coms = list_kb_entries(pillar="community")
                self.assertEqual(snips["count"], 5)
                self.assertGreaterEqual(hws["count"], 5)
                self.assertGreaterEqual(coms["count"], 6)

                for s in snips["entries"]:
                    self.assertTrue(Path(s["abs_doc_path"]).is_file())
                    self.assertTrue(s["doc_path"].endswith(".h"))
                for c in coms["entries"]:
                    self.assertIn(c["portability"], {"portable", "remap-to-accel", "anti-pattern"})
                    self.assertTrue(Path(c["abs_doc_path"]).is_file())

                # Mutate local user-evolved entry + header file
                com_idx_path = p_home / "community" / "index.json"
                com_idx = json.loads(com_idx_path.read_text(encoding="utf-8"))
                com_idx["entries"][0]["summary"] = "USER_EVOLVED_SUMMARY"
                com_idx["entries"][0]["confidence"] = 0.999
                com_idx_path.write_text(json.dumps(com_idx), encoding="utf-8")

                snip_h = p_home / "snippets" / "code" / "SNP-zero-tpipe-ub-arena.h"
                snip_h.write_text("// USER_CUSTOM_SNIPPET\n", encoding="utf-8")

                second = ensure_triad_kb_seeded()
                self.assertEqual(second["seeded_pillars"], [])
                self.assertEqual(second["copied_count"], 0)
                self.assertEqual(
                    json.loads(com_idx_path.read_text(encoding="utf-8"))["entries"][0]["summary"],
                    "USER_EVOLVED_SUMMARY",
                )
                self.assertEqual(snip_h.read_text(encoding="utf-8"), "// USER_CUSTOM_SNIPPET\n")

    def test_query_across_all_three_pillars_antipatterns_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            bt_dir = p_home / "breakthrough"
            (bt_dir / "entries").mkdir(parents=True, exist_ok=True)
            bt_doc = bt_dir / "entries" / "BT-20261122-TestOp-1234567.md"
            bt_doc.write_text("# Breakthrough TestOp\n", encoding="utf-8")
            (bt_dir / "index.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": [
                            {
                                "id": "BT-20261122-TestOp-1234567",
                                "problem": "TestOp",
                                "difficulty": 3,
                                "sha": "1234567890abcdef",
                                "timestamp": "2026-11-22T12:00:00Z",
                                "score_before": 60.0,
                                "score_after": 74.5,
                                "delta_raw": 14.5,
                                "delta_weighted": 43.5,
                                "status": "verified",
                                "category": "tiling-regime",
                                "tags": ["zero-TPipe", "64B-tiling"],
                                "title": "Remove TPipe and 10-core dispatch floor",
                                "bottleneck": "Cold launch tax and 16MB workspace hurt small core regime",
                                "mechanisms": ["Static UB arena", "64B direct gm tiling"],
                                "precision_guard": "EXTENDED_PASSED",
                                "transferable_rule": "Use zero-TPipe static UB arena for S_SMALL_CORE",
                                "doc_path": "entries/BT-20261122-TestOp-1234567.md",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                ensure_triad_kb_seeded()
                res = query_kb(
                    bottlenecks=["cold-launch-tax"],
                    regime="S_SMALL_CORE",
                    dtype="fp16",
                    include_anti_patterns=True,
                    limit=10,
                )
                pillars_returned = {r["pillar"] for r in res["results"]}
                self.assertEqual(pillars_returned, {"breakthrough", "snippets", "hardware", "community"})

                anti_ids = [a["id"] for a in res["anti_patterns"]]
                self.assertIn("HW-10core-dvfs-bimodal-trap", anti_ids)
                for r in res["results"]:
                    self.assertNotEqual(r["status"], "anti-pattern")

                no_anti = query_kb(
                    bottlenecks=["cold-launch-tax"],
                    regime="S_SMALL_CORE",
                    include_anti_patterns=False,
                )
                self.assertEqual(no_anti["anti_pattern_count"], 0)
                self.assertEqual(no_anti["anti_patterns"], [])

                digest = format_prompt_cache_digest(res, max_chars=2400)
                self.assertLessEqual(len(digest), 2400)
                self.assertIn("DO-NOT-REPEAT Warnings", digest)
                self.assertIn("HW-10core-dvfs-bimodal-trap", digest)
                self.assertIn("SNP-direct-gm-tiling-64b", digest)
                # Warnings must appear before positive guidance
                self.assertLess(digest.index("DO-NOT-REPEAT"), digest.index("Verified & Ranked"))

                tight_digest = format_prompt_cache_digest(res, max_chars=280)
                self.assertLessEqual(len(tight_digest), 280)

    def test_evidence_gate_rejects_unbacked_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                ensure_triad_kb_seeded()

                # Missing evidence dict
                with self.assertRaises(ValueError):
                    record_kb_entry(
                        "community",
                        {"title": "Unbacked Idea", "summary": "Should fail"},
                    )

                # Invalid SHA (not 7-40 hex)
                with self.assertRaises(ValueError):
                    record_kb_entry(
                        "snippets",
                        {
                            "title": "Bad SHA Snippet",
                            "summary": "Bad SHA test",
                            "evidence": {"problem": "ConcatD", "sha": "xyz", "verdict": "win", "delta_raw": 2.0},
                        },
                    )

                # Missing measurable metric (vague note only)
                with self.assertRaises(ValueError):
                    record_kb_entry(
                        "community",
                        {
                            "title": "Vague Metric",
                            "summary": "No real numbers",
                            "evidence": {
                                "problem": "ConcatD",
                                "sha": "abcdef1",
                                "verdict": "win",
                                "note": "feels much faster locally",
                            },
                        },
                    )

                # Missing problem in feedback
                with self.assertRaises(ValueError):
                    record_kb_feedback(
                        "COM-online-welford-softmax",
                        "win",
                        {"problem": "", "sha": "abcdef1", "delta_raw": 3.0},
                    )

                # Valid note citing profiler passes
                ok_res = record_kb_entry(
                    "snippets",
                    {
                        "title": "Level0 Hoisted Mask Helper",
                        "summary": "Hoist SetMaskNorm outside inner loop",
                        "bottlenecks": ["scalar-bound"],
                        "code": "#pragma once\n// hoisted mask\n",
                        "evidence": {
                            "problem": "CTCLossV2",
                            "sha": "2b9d49d",
                            "verdict": "win",
                            "note": "profiler Task Duration improved 14.2->11.5us",
                        },
                    },
                )
                self.assertTrue(ok_res["ok"])
                self.assertEqual(ok_res["entry"]["status"], "verified")

    def test_bayesian_confidence_promotion_demotion_and_rollback(self) -> None:
        self.assertEqual(compute_confidence(4, 0, portability="native-accel", status="verified"), 0.857)
        self.assertEqual(compute_confidence(0, 0, portability="portable", status="unverified"), 0.5)

        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                ensure_triad_kb_seeded()

                # 1. Small win keeps unverified, lifts confidence 0.5 -> 0.667
                fb1 = record_kb_feedback(
                    "COM-online-welford-softmax",
                    "win",
                    {"problem": "LayerNormV3", "sha": "1111aaa", "delta_raw": 0.4},
                )
                self.assertEqual(fb1["entry"]["status"], "unverified")
                self.assertEqual(fb1["entry"]["confidence"], 0.667)

                # 2. Strong win (delta_raw >= 1.5) promotes to verified + creates linked snippet
                fb2 = promote_entry(
                    "COM-online-welford-softmax",
                    {"problem": "LayerNormV3", "sha": "2222bbb", "delta_raw": 4.2},
                    promote_snippet_code="#pragma once\n// Welford online pass\n",
                )
                self.assertEqual(fb2["action"], "promote")
                self.assertEqual(fb2["transition"], "unverified->verified")
                self.assertEqual(fb2["entry"]["status"], "verified")
                self.assertEqual(fb2["entry"]["wins"], 2)
                self.assertEqual(fb2["entry"]["confidence"], 0.75)
                snip_id = fb2["created_snippet_id"]
                self.assertIsNotNone(snip_id)
                self.assertIn(snip_id, fb2["entry"]["related_snippets"])
                self.assertIsNotNone(get_kb_entry(snip_id))

                # 3. Record unverified community card via fail -> 3 fails demote to anti-pattern
                rec = record_kb_entry(
                    "community",
                    {
                        "id": "COM-bad-shared-experiment",
                        "title": "Speculative Multi-Barrier Poll",
                        "summary": "Experimental barrier strategy",
                        "bottlenecks": ["sync-hazard"],
                        "portability": "portable",
                        "evidence": {"problem": "PadV3Grad", "sha": "aaa1111", "verdict": "fail", "delta_us": 1.2},
                    },
                )
                self.assertEqual(rec["entry"]["status"], "unverified")
                self.assertEqual(rec["entry"]["fails"], 1)

                fb_fail2 = record_kb_feedback(
                    "COM-bad-shared-experiment",
                    "fail",
                    {"problem": "PadV3Grad", "sha": "bbb2222", "note": "profiler +0.9us regression"},
                )
                self.assertEqual(fb_fail2["entry"]["status"], "unverified")

                fb_fail3 = record_kb_feedback(
                    "COM-bad-shared-experiment",
                    "fail",
                    {"problem": "ConcatD", "sha": "ccc3333", "delta_raw": -2.1},
                )
                self.assertEqual(fb_fail3["action"], "demote")
                self.assertEqual(fb_fail3["entry"]["status"], "anti-pattern")
                self.assertEqual(fb_fail3["entry"]["portability"], "anti-pattern")
                self.assertEqual(fb_fail3["entry"]["confidence"], 0.2)

                # Anti-pattern now surfaces in warning bucket
                q_res = query_kb(bottlenecks=["sync-hazard"])
                self.assertIn("COM-bad-shared-experiment", [a["id"] for a in q_res["anti_patterns"]])

                # 4. Evolution audit log & rollback_last / revert_event
                hist = get_kb_evolution_history(limit=20)
                self.assertGreaterEqual(hist["total_events"], 6)
                rb1 = rollback_last()
                self.assertEqual(rb1["entry_id"], "COM-bad-shared-experiment")
                restored = get_kb_entry("COM-bad-shared-experiment")
                self.assertIsNotNone(restored)
                self.assertEqual(restored["status"], "unverified")
                self.assertEqual(restored["fails"], 2)

                # Revert promotion event removes linked snippet and restores pre-promotion state
                rb_promo = revert_event(fb2["event"]["event_id"])
                self.assertEqual(rb_promo["entry_id"], "COM-online-welford-softmax")
                demoted_back = get_kb_entry("COM-online-welford-softmax")
                self.assertEqual(demoted_back["status"], "unverified")
                self.assertEqual(demoted_back["wins"], 1)
                self.assertIsNone(get_kb_entry(snip_id))

                # Cannot revert already-reverted event
                with self.assertRaises(ValueError):
                    revert_event(fb2["event"]["event_id"])

    def test_project_readonly_skills_0400_and_claude_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            ws = Path(td) / "workspace"
            ws.mkdir()
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                res1 = project_readonly_skills(ws)
                self.assertTrue(res1["ok"])
                self.assertGreaterEqual(res1["file_count"], 7)

                skill_md = ws / ".agents" / "skills" / "parhelion-kb" / "SKILL.md"
                kb_idx = ws / ".agents" / "skills" / "parhelion-kb" / "KB_INDEX.md"
                snip_h = ws / ".agents" / "skills" / "parhelion-kb" / "snippets" / "SNP-zero-tpipe-ub-arena.h"
                for p in (skill_md, kb_idx, snip_h):
                    self.assertTrue(p.is_file())
                    self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o400)

                claude_skills = ws / ".claude" / "skills"
                self.assertTrue(claude_skills.is_symlink())
                self.assertEqual(claude_skills.resolve(), (ws / ".agents" / "skills").resolve())

                # Re-projecting over 0400 files must succeed cleanly
                res2 = project_readonly_skills(ws)
                self.assertTrue(res2["ok"])

    def test_cli_subcommands_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            ws = Path(td) / "ws"
            ws.mkdir()
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                rc, out, _ = _run_kb_cli(["kb", "list", "--pillar", "snippets", "--json"])
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(out)["count"], 5)

                rc, out, _ = _run_kb_cli(
                    ["kb", "query", "--bottleneck", "cold-launch-tax", "--regime", "S_SMALL_CORE", "--digest"]
                )
                self.assertEqual(rc, 0)
                self.assertIn("Parhelion 4-Pillar KB Digest", out)

                rc, out, _ = _run_kb_cli(["kb", "show", "SNP-direct-gm-tiling-64b", "--json"])
                self.assertEqual(rc, 0)
                self.assertIn("DirectGmTiling", json.loads(out)["entry"]["content"])

                # Unbacked CLI record returns error code 1
                rc, out, _ = _run_kb_cli(
                    [
                        "kb",
                        "record",
                        "--pillar",
                        "community",
                        "--title",
                        "No Metrics",
                        "--summary",
                        "Should fail",
                        "--problem",
                        "ConcatD",
                        "--sha",
                        "1234567",
                        "--verdict",
                        "win",
                        "--note",
                        "gut feeling",
                        "--json",
                    ]
                )
                self.assertEqual(rc, 1)
                self.assertFalse(json.loads(out)["ok"])

                # Backed CLI record + feedback + history + rollback
                rc, out, _ = _run_kb_cli(
                    [
                        "kb",
                        "record",
                        "--pillar",
                        "community",
                        "--title",
                        "Vectorized Mask Dispatch",
                        "--summary",
                        "Replace scalar branch with Compare+Select",
                        "--problem",
                        "ConcatD",
                        "--sha",
                        "deadbeef1",
                        "--verdict",
                        "win",
                        "--bottleneck",
                        "scalar-bound",
                        "--portability",
                        "remap-to-accel",
                        "--delta-raw",
                        "2.5",
                        "--json",
                    ]
                )
                self.assertEqual(rc, 0)
                created_id = json.loads(out)["entry"]["id"]

                rc, out, _ = _run_kb_cli(
                    [
                        "kb",
                        "feedback",
                        created_id,
                        "--verdict",
                        "anti-pattern",
                        "--problem",
                        "ConcatD",
                        "--sha",
                        "deadbeef2",
                        "--note",
                        "profiler regression on odd width",
                        "--json",
                    ]
                )
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(out)["entry"]["status"], "anti-pattern")

                rc, out, _ = _run_kb_cli(["kb", "history", "--limit", "5", "--json"])
                self.assertEqual(rc, 0)
                self.assertGreaterEqual(json.loads(out)["total_events"], 2)

                rc, out, _ = _run_kb_cli(["kb", "rollback", "--json"])
                self.assertEqual(rc, 0)
                self.assertTrue(json.loads(out)["ok"])

                rc, out, _ = _run_kb_cli(["kb", "project", str(ws), "--json"])
                self.assertEqual(rc, 0)
                self.assertTrue(json.loads(out)["ok"])

    def test_speedup_promotion_text_query_and_human_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            ensure_triad_kb_seeded(parhelion_home=p_home)

            # Promote via speedup >= 1.08 (no delta_raw needed)
            res = record_kb_feedback(
                "COM-warp-shuffle-to-5hd-ub",
                "win",
                {
                    "problem": "LayerNormV3",
                    "sha": "9988aabb",
                    "speedup": 1.32,
                    "note": "profiler 256B WholeReduce + 5HD speedup",
                },
                parhelion_home=p_home,
            )
            self.assertEqual(res["action"], "promote")
            self.assertEqual(res["entry"]["status"], "verified")

            # Free-text query finds TransDataTo5HD across snippets & community
            q_txt = query_kb(text="TransDataTo5HD", parhelion_home=p_home)
            ids = {r["id"] for r in q_txt["results"]}
            self.assertIn("SNP-transdata-5hd-transpose", ids)
            self.assertIn("COM-warp-shuffle-to-5hd-ub", ids)

            # Human-readable CLI output smoke check
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                for argv in (
                    ["kb", "list"],
                    ["kb", "query", "--text", "TransDataTo5HD"],
                    ["kb", "show", "COM-warp-shuffle-to-5hd-ub"],
                    ["kb", "history"],
                ):
                    rc, out, err = _run_kb_cli(argv)
                    self.assertEqual(rc, 0, msg=f"CLI {argv} failed: {err}")
                    self.assertTrue(out.strip())


if __name__ == "__main__":
    unittest.main()
