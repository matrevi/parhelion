"""Unit tests for Parhelion v3.1.0: 4-Pillar KB (Hardware pillar + migration), 16KB L1I ELF guard, and version alignment."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_DIR.parent
for p in (str(CLI_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import opsb_core  # noqa: E402
from opsb_core.fast_feedback.elf_guard import evaluate_elf_guard  # noqa: E402
from opsb_core.knowledge import (  # noqa: E402
    VALID_PILLARS,
    WRITABLE_PILLARS,
    classify_community_entry_is_hardware,
    ensure_triad_kb_seeded,
    get_kb_entry,
    list_kb_entries,
    migrate_hardware_entries_from_community,
    project_readonly_skills,
    query_kb,
    record_kb_entry,
    record_kb_feedback,
)
from opsb_core.mcp.handlers_kb import tool_parhelion_kb_query, tool_parhelion_kb_record  # noqa: E402
from opsb_core.mcp.server import SERVER_INFO  # noqa: E402
from scheduler.pairwise_anchor import evaluate_pairwise_anchor  # noqa: E402


class FourPillarKBAndV31Tests(unittest.TestCase):
    def test_four_pillars_and_v310_version_alignment(self) -> None:
        self.assertEqual(VALID_PILLARS, ("breakthrough", "snippets", "hardware", "community"))
        self.assertEqual(WRITABLE_PILLARS, ("snippets", "hardware", "community"))
        self.assertEqual(opsb_core.__version__, "3.1.0")
        self.assertEqual(SERVER_INFO["version"], "3.1.0")

    def test_record_query_feedback_and_legacy_id_lookup_on_hardware_pillar(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            ws = Path(td) / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                seeded = ensure_triad_kb_seeded()
                self.assertIn("hardware", seeded["seeded_pillars"])

                # Legacy COM-* ID resolves transparently to migrated HW-* seed entry
                legacy = get_kb_entry("COM-10core-dvfs-bimodal-trap")
                self.assertIsNotNone(legacy)
                assert legacy is not None
                self.assertEqual(legacy["id"], "HW-10core-dvfs-bimodal-trap")
                self.assertEqual(legacy["pillar"], "hardware")

                # Record a brand-new hardware law via MCP handler
                rec = tool_parhelion_kb_record(
                    {
                        "pillar": "hardware",
                        "title": "Accel soc-a 64B Slot Stride Anti-False-Sharing",
                        "summary": "Keep cross-core reduction slots 64B aligned when cores > 16.",
                        "problem": "MultilabelMarginLoss",
                        "sha": "13c554d",
                        "verdict": "win",
                        "delta_raw": 3.2,
                        "bottlenecks": ["sync-hazard"],
                        "note": "online verified",
                    },
                    ws,
                )
                self.assertTrue(rec["ok"])
                ent = rec["entry"]
                self.assertEqual(ent["pillar"], "hardware")
                self.assertTrue(ent["id"].startswith("HW-"))
                self.assertEqual(ent["status"], "verified")

                # Query specifically within hardware pillar via MCP
                q = tool_parhelion_kb_query({"pillar": "hardware", "text": "64B", "limit": 5}, ws)
                ids = [e["id"] for e in q["results"]]
                self.assertIn(ent["id"], ids)

                # Feedback on legacy ID updates the hardware entry in place
                fb = record_kb_feedback(
                    "COM-10core-dvfs-bimodal-trap",
                    "fail",
                    {"problem": "ConcatD", "sha": "abcdef1", "verdict": "fail", "delta_us": 1.2, "note": "profiler"},
                )
                self.assertEqual(fb["entry"]["id"], "HW-10core-dvfs-bimodal-trap")
                self.assertEqual(fb["entry"]["pillar"], "hardware")

                # Read-only skill projection includes dedicated Hardware Laws section
                proj = project_readonly_skills(ws)
                self.assertTrue(proj["ok"])
                kb_index_md = (ws / ".agents" / "skills" / "parhelion-kb" / "KB_INDEX.md").read_text(encoding="utf-8")
                self.assertIn("## Accel Hardware Laws & Silicon Findings", kb_index_md)
                self.assertIn("hardware=", kb_index_md)

    def test_migration_separates_hardware_discoveries_from_community_schemes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_home = Path(td) / "parhelion_home"
            com_dir = p_home / "community"
            (com_dir / "entries").mkdir(parents=True, exist_ok=True)
            entries = [
                {
                    "id": "COM-online-welford-softmax",
                    "pillar": "community",
                    "title": "Online Welford / Softmax Single-Pass Reduction",
                    "summary": "General single-pass reduction algorithm.",
                    "portability": "portable",
                    "status": "unverified",
                    "doc_path": "entries/COM-online-welford-softmax.md",
                },
                {
                    "id": "COM-arch-v1-hbm-stack-escape-trap",
                    "pillar": "community",
                    "title": "arch-v1 HBM-stack escape trap on noinline >7-arg callees",
                    "summary": "Measured on Accel soc-a: >7-arg noinline spills to HBM stack.",
                    "portability": "anti-pattern",
                    "status": "anti-pattern",
                    "wins": 0,
                    "fails": 2,
                    "evidence": [{"problem": "PadV3Grad", "sha": "f13032c", "verdict": "anti-pattern", "delta_us": 4.2}],
                    "doc_path": "entries/COM-arch-v1-hbm-stack-escape-trap.md",
                },
            ]
            (com_dir / "index.json").write_text(json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8")
            (com_dir / "entries" / "COM-arch-v1-hbm-stack-escape-trap.md").write_text("# Old\nBody text.\n", encoding="utf-8")

            is_hw_com, _ = classify_community_entry_is_hardware(entries[0])
            is_hw_trap, _ = classify_community_entry_is_hardware(entries[1])
            self.assertFalse(is_hw_com)
            self.assertTrue(is_hw_trap)

            dry = migrate_hardware_entries_from_community(parhelion_home=p_home, dry_run=True)
            self.assertEqual(dry["to_move_count"], 1)
            self.assertEqual(dry["remaining_community_count"], 1)

            res1 = migrate_hardware_entries_from_community(parhelion_home=p_home, dry_run=False)
            self.assertEqual(res1["migrated_count"], 1)
            self.assertEqual(res1["community_total"], 1)
            self.assertGreaterEqual(res1["hardware_total"], 5)
            self.assertFalse((com_dir / "entries" / "COM-arch-v1-hbm-stack-escape-trap.md").exists())
            self.assertTrue((p_home / "hardware" / "entries" / "HW-arch-v1-hbm-stack-escape-trap.md").is_file())

            # Legacy ID resolves after migration
            resolved = get_kb_entry("COM-arch-v1-hbm-stack-escape-trap", parhelion_home=p_home)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved["id"], "HW-arch-v1-hbm-stack-escape-trap")
            self.assertEqual(resolved["pillar"], "hardware")
            self.assertEqual(len(resolved["evidence"]), 1)

            # Second run is strictly idempotent
            res2 = migrate_hardware_entries_from_community(parhelion_home=p_home, dry_run=False)
            self.assertEqual(res2["migrated_count"], 0)
            self.assertEqual(list_kb_entries(pillar="community", parhelion_home=p_home)["count"], 1)

    def test_elf_guard_16kb_aiv_l1i_hot_symbol_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            run_dir = ws / "results" / "PadV3Grad" / "abcdef1234567890"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "result.json").write_text(
                json.dumps(
                    {
                        "exit_code": 0,
                        "kernel_o_count": 2,
                        "kernel_o_max_bytes": 60000,
                        "kernel_o_max_text_bytes": 22000,
                        "max_hot_symbol_bytes": 18432,
                        "top_symbols": [{"symbol": "pad_v3_grad_0", "bytes": 18432}],
                        "template_instantiations": 12,
                    }
                ),
                encoding="utf-8",
            )
            res = evaluate_elf_guard(ws, "PadV3Grad", sha="abcdef1234567890")
            codes = {a["code"] for a in res["alerts"]}
            self.assertIn("L1I_HOT_ENTRY_WARN", codes)
            self.assertEqual(res["status"], "warn")

    def test_pairwise_anchor_avoids_triple_discount_on_regime_mismatch(self) -> None:
        buckets = [f"b{i}" for i in range(10)]
        base_rob = {b: 20.0 for b in buckets}
        cand_rob = {b: 8.0 for b in buckets}
        pred = evaluate_pairwise_anchor(
            cand_sha="cand1234",
            cand_tel={"robust_cases": cand_rob, "modality": "profiler", "regime": "l1"},
            anchor_sha="base1234",
            anchor_tel={"robust_cases": base_rob, "modality": "profiler", "regime": "l0"},
            anchor_kind="latest",
            anchor_sc=60.0,
            r_anchor=0.60,
            current_score=60.0,
            frontier_top1=90.0,
            cal={"pairwise_count": 2, "transfer_gamma": 0.85, "calibration_trust": 0.6},
            spec_ids=buckets,
        )
        self.assertIsNotNone(pred)
        assert pred is not None
        self.assertGreater(pred["estimated_score"], 66.0)


if __name__ == "__main__":
    unittest.main()
