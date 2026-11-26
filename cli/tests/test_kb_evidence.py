"""P28 (4.3) knowledge v2: evidence levels, mechanism fields, promotion gate, process cards.

Stage A covers scopes 1, 2, 3, 5, 6 of `docs/v4.3/P28-kb-evidence.md`. The gate-reply
attachment (scope 4) lands in stage B after P23 merges.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_DIR.parent
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.knowledge.migration import migrate_evidence_classes  # noqa: E402
from opsb_core.knowledge.query import (  # noqa: E402
    EVIDENCE_CLASSES, STRONG_EVIDENCE_CLASSES, VALID_CATEGORIES,
    format_prompt_cache_digest, has_strong_evidence, query_kb,
)
from opsb_core.knowledge.store import VALID_PILLARS  # noqa: E402
from opsb_core.mcp.handlers_kb import tool_parhelion_kb_query  # noqa: E402
from opsb_core.services.triad_kb_service import (  # noqa: E402
    ensure_triad_kb_seeded, record_kb_entry, record_kb_feedback,
)

PROCESS_CARD_IDS = (
    "COM-process-latest-state-change",
    "COM-process-noise-is-not-a-breakthrough",
    "COM-process-restore-before-experiment",
    "COM-process-judge-time-shared-budget",
    "COM-process-correctness-before-speed",
    "COM-process-local-profiler-needs-pairing",
)

# Vocabulary that must never appear in a device-independent process card. Static by design: a
# configured developer machine must not be able to weaken this test.
DEVICE_AND_VENDOR_WORDS = (
    "ascend", "昇腾", "华为", "cann", "davinci", "达芬奇", "atlas", "kunpeng", "鲲鹏",
    "mindspore", "msprof", "openeuler", "aclrt", "910", "310p", "c220", "hisi",
    "寒武纪", "海光",
)
# Achieved contest scores are not knowledge; the cards must stay readable next season.
CONTEST_SCORE_LITERALS = ("752.3", "1717.1", "331.36", "420.97", "722.9", "542.0", "421.0")


def _home(td: Path) -> Path:
    return td / "parhelion_home"


def _write_index(home: Path, pillar: str, entries: list[dict]) -> None:
    p = home / pillar / "index.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8")


def _read_index(home: Path, pillar: str) -> list[dict]:
    raw = json.loads((home / pillar / "index.json").read_text(encoding="utf-8"))
    return raw["entries"]


def _legacy_hardware_entry(entry_id: str = "HW-legacy-local", *, status: str = "unverified") -> dict:
    """A pre-4.3 hardware entry: no evidence_class, no mechanism fields."""
    return {
        "id": entry_id, "pillar": "hardware", "title": "Legacy local claim", "summary": "uniqueclaim legacy local profiling",
        "category": "hardware-law", "tags": ["legacy"], "bottlenecks": ["general-perf"],
        "regimes": ["M_MID_UB"], "dtypes": ["float16"], "portability": "native-accel",
        "status": status, "prior_alpha": 2.0, "prior_beta": 1.0, "wins": 1, "fails": 0,
        "confidence": 0.7, "evidence": [
            {"problem": "TestOp", "sha": "abcdef1", "verdict": "win", "delta_us": -1.5,
             "note": "profiler local win", "recorded_at": "2026-11-20T00:00:00+00:00"},
        ],
        "related_snippets": [], "doc_path": f"entries/{entry_id}.md",
    }


def test_entry_fields_and_evidence_defaults_for_legacy_rows(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _write_index(home, "hardware", [_legacy_hardware_entry()])
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home)}, clear=False):
        res = query_kb(text="uniqueclaim", pillar="hardware", limit=5)
        entry = res["results"][0]
        # Entry-level defaults: mechanism unconfirmed, no trigger/cost/domain, no retraction.
        assert entry["mechanism_confirmed"] == "no"
        assert entry["trigger"] == "" and entry["cost"] == "" and entry["failure_domain"] == ""
        assert entry["applicable_problems"] == [] and entry["retracted_by"] == ""
        assert entry["retracted"] is False
        # Evidence-line defaults: provisional `local`, one draw, no known sigma, no repro.
        ev = entry["evidence"][0]
        assert ev["evidence_class"] == "local"
        assert ev["n_draws"] == 1 and ev["sigma_pts"] == 0.0 and ev["repro"] == ""
        assert entry["evidence_composition"] == {"local": 1, "paired": 0, "online_receipt": 0}
        assert entry["evidence_strong"] is False
        # A hardware candidate with local-only evidence is surfaced as 待验证, status untouched.
        assert entry["status"] == "unverified"
        assert entry["pending_validation"] is True


def test_new_entry_fields_round_trip_and_declared_evidence_class_wins(tmp_path: Path) -> None:
    home = _home(tmp_path)
    raw = _legacy_hardware_entry("HW-declared", status="verified")
    raw["evidence"][0]["evidence_class"] = "paired"
    raw["evidence"][0]["n_draws"] = 3
    raw["evidence"][0]["sigma_pts"] = 2.5
    raw["evidence"][0]["repro"] = "run-42"
    raw.update({
        "mechanism_confirmed": "confounded", "trigger": "C <= 2048", "cost": "one extra compile",
        "failure_domain": "does not hold for unaligned tails", "applicable_problems": ["TestOp"],
        "retracted_by": "HW-newer",
    })
    _write_index(home, "hardware", [raw])
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home)}, clear=False):
        entry = query_kb(text="uniqueclaim", pillar="hardware", limit=5)["results"][0]
        assert entry["mechanism_confirmed"] == "confounded"
        assert (entry["trigger"], entry["cost"], entry["failure_domain"]) == (
            "C <= 2048", "one extra compile", "does not hold for unaligned tails")
        assert entry["applicable_problems"] == ["TestOp"]
        assert entry["retracted_by"] == "HW-newer" and entry["retracted"] is True
        assert entry["evidence_composition"] == {"local": 0, "paired": 1, "online_receipt": 0}
        assert entry["evidence_strong"] is True
        assert entry["pending_validation"] is False
        assert entry["evidence"][0]["n_draws"] == 3
        assert entry["evidence"][0]["sigma_pts"] == 2.5
        assert entry["evidence"][0]["repro"] == "run-42"


def test_categories_add_three_values_without_new_pillars() -> None:
    assert EVIDENCE_CLASSES == ("local", "paired", "online_receipt")
    assert STRONG_EVIDENCE_CLASSES == ("paired", "online_receipt")
    for cat in ("scoring-law", "measurement", "process"):
        assert cat in VALID_CATEGORIES
    # Pillars are a front-end contract; 4.3 must not add one.
    assert VALID_PILLARS == ("breakthrough", "snippets", "hardware", "community", "bugs")
    with pytest.raises(ValueError):
        record_kb_entry("community", {
            "title": "Bad category", "summary": "s", "problem": "P", "sha": "abcdef1", "verdict": "win",
            "delta_raw": 2.0, "category": "not-a-category",
        })


def test_write_path_records_levels_and_rejects_bad_values(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home)}, clear=False):
        res = record_kb_entry("community", {
            "id": "COM-test-levels", "title": "TestOp level card", "summary": "paired evidence card",
            "category": "measurement", "problem": "TestOp", "sha": "abcdef1", "verdict": "win",
            "delta_raw": 2.0, "evidence_class": "paired", "n_draws": 4, "sigma_pts": 1.5, "repro": "opsb test --sha abcdef1",
            "mechanism_confirmed": "yes", "trigger": "when sigma > 1", "cost": "one draw",
            "failure_domain": "single-case problems", "applicable_problems": ["TestOp", "Other"],
        })
        entry = res["entry"]
        assert entry["category"] == "measurement"
        assert entry["evidence"][0]["evidence_class"] == "paired"
        assert entry["evidence"][0]["n_draws"] == 4 and entry["evidence"][0]["repro"].startswith("opsb test")
        assert entry["mechanism_confirmed"] == "yes"
        assert entry["applicable_problems"] == ["TestOp", "Other"]

        bad = dict(title="Bad", summary="s", problem="P", sha="abcdef1", verdict="win", delta_raw=2.0)
        for patch in ({"evidence_class": "online"}, {"n_draws": 0}, {"sigma_pts": -1}, {"mechanism_confirmed": "maybe"}):
            with pytest.raises(ValueError):
                record_kb_entry("community", {**bad, **patch})


def test_hardware_promotion_requires_paired_or_online_evidence(tmp_path: Path) -> None:
    home = _home(tmp_path)
    base = {
        "pillar": "hardware", "title": "Local only law", "summary": "local profiler claim",
        "problem": "TestOp", "sha": "abcdef1", "verdict": "win", "delta_raw": 3.0,
        "trigger": "t", "cost": "c", "failure_domain": "f",
    }
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home)}, clear=False):
        ensure_triad_kb_seeded()
        local = record_kb_entry("hardware", {**base, "id": "HW-local-only"})
        assert local["entry"]["status"] == "unverified"
        assert local["promotion_blocked"], "local-only hardware win must not promote"
        assert local["entry"]["pending_validation"] is True

        paired = record_kb_entry("hardware", {**base, "id": "HW-paired", "title": "Paired law", "evidence_class": "paired"})
        assert paired["promotion_blocked"] is None
        assert paired["entry"]["status"] == "verified"

        # Feedback path: a local win on a candidate is held; a strong win promotes.
        held = record_kb_feedback("HW-local-only", "win", {
            "problem": "TestOp", "sha": "abcdef1", "verdict": "win", "delta_raw": 3.0, "note": "online off-board note",
        })
        assert held["entry"]["status"] == "unverified"
        assert held["transition"] == "held-local-evidence"
        assert held["promotion_blocked"]

        promoted = record_kb_feedback("HW-local-only", "win", {
            "problem": "TestOp", "sha": "abcdef1", "verdict": "win", "delta_raw": 3.0,
            "evidence_class": "online_receipt",
        })
        assert promoted["entry"]["status"] == "verified"
        assert promoted["promotion_blocked"] is None


def test_other_pillars_keep_the_bayesian_gate(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home)}, clear=False):
        ensure_triad_kb_seeded()
        res = record_kb_entry("community", {
            "id": "COM-bayes-window", "title": "Bayesian window", "summary": "community card with a local win",
            "category": "process", "problem": "TestOp", "sha": "abcdef1", "verdict": "win", "delta_raw": 2.0,
        })
        assert res["promotion_blocked"] is None
        assert res["entry"]["status"] == "verified", "non-hardware pillars must keep the 4.2.1 gate"


def test_migration_is_idempotent_and_never_downgrades(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home)}, clear=False):
        ensure_triad_kb_seeded()
        before = {e["id"]: e["status"] for e in _read_index(home, "hardware")}
        verified_before = [i for i, s in before.items() if s == "verified"]
        assert verified_before, "template seeds carry pre-4.3 promoted hardware laws"

        first = migrate_evidence_classes()
        assert first["backfilled"] > 0
        assert first["hardware_promoted_kept"] == len(verified_before)
        assert {e["id"]: e["status"] for e in _read_index(home, "hardware")} == before
        rows = [ev for e in _read_index(home, "hardware") for ev in (e.get("evidence") or [])]
        assert rows and all(ev.get("evidence_class") == "local" for ev in rows)

        snapshot = (home / "hardware" / "index.json").read_bytes()
        second = migrate_evidence_classes()
        assert (second["backfilled"], second["entries_touched"]) == (0, 0)
        assert (home / "hardware" / "index.json").read_bytes() == snapshot
        assert {e["id"]: e["status"] for e in _read_index(home, "hardware")} == before

        dry = migrate_evidence_classes(dry_run=True)
        assert dry["backfilled"] == 0


def test_query_brief_marks_local_only_hardware_as_pending(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _write_index(home, "hardware", [_legacy_hardware_entry()])
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home)}, clear=False):
        q = query_kb(text="uniqueclaim", pillar="hardware", limit=5)
        digest = format_prompt_cache_digest(q, max_chars=2400)
        assert "|ev=local:1" in digest and "待验证" in digest

        brief = tool_parhelion_kb_query({"pillar": "hardware", "text": "uniqueclaim"}, tmp_path)
        row = brief["results"][0]
        assert row["validation"] == "pending"
        assert row["evidence"] == {"local": 1, "paired": 0, "online_receipt": 0}


def test_process_cards_seed_idempotently_and_keep_edited_files(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home)}, clear=False):
        first = ensure_triad_kb_seeded()
        assert first["copied_count"] >= 25
        idx = {e["id"]: e for e in _read_index(home, "community")}
        for card_id in PROCESS_CARD_IDS:
            assert card_id in idx, f"{card_id} must be registered in templates/community/index.json"
            assert Path(home / "community" / "entries" / f"{card_id}.md").is_file()
            assert idx[card_id]["category"] in ("scoring-law", "measurement", "process")
            assert idx[card_id]["evidence"] == []
        assert {idx[c]["category"] for c in PROCESS_CARD_IDS} == {"scoring-law", "measurement", "process"}

        edited = home / "community" / "entries" / f"{PROCESS_CARD_IDS[0]}.md"
        edited.write_text("EDITED BY THE TEAM\n", encoding="utf-8")
        second = ensure_triad_kb_seeded()
        assert second["copied_count"] == 0
        assert edited.read_text(encoding="utf-8") == "EDITED BY THE TEAM\n"


def test_process_cards_stay_device_free_and_score_free() -> None:
    entries_dir = REPO_ROOT / "templates" / "community" / "entries"
    index = json.loads((REPO_ROOT / "templates" / "community" / "index.json").read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in index["entries"]}
    for card_id in PROCESS_CARD_IDS:
        body = (entries_dir / f"{card_id}.md").read_text(encoding="utf-8")
        record = json.dumps(by_id[card_id], ensure_ascii=False)
        blob = f"{body}\n{record}".lower()
        for word in DEVICE_AND_VENDOR_WORDS:
            assert word.lower() not in blob, f"{card_id} mentions device/vendor term {word!r}"
        for literal in CONTEST_SCORE_LITERALS:
            assert literal not in blob, f"{card_id} carries an achieved contest score {literal!r}"
        # Every card must state a rule, a measurable trigger, a failure domain and a source.
        for heading in ("## 规则", "## 可测触发条件", "## 反例与失效域", "## 来源"):
            assert heading in body, f"{card_id} is missing {heading}"
        entry = by_id[card_id]
        assert entry["mechanism_confirmed"] in ("yes", "no", "confounded")
        assert entry["trigger"] and entry["cost"] and entry["failure_domain"]
        assert "docs/v4.3/research/antigravity/" in body


def test_evidence_composition_helper_counts_all_classes() -> None:
    evidence = [
        {"evidence_class": "paired"}, {"evidence_class": "online_receipt"},
        {"evidence_class": "online"}, {}, None,
    ]
    assert has_strong_evidence(evidence) is True
    assert has_strong_evidence([{}, {"evidence_class": "local"}]) is False


def test_evidence_class_vocabulary_is_pinned_for_p27() -> None:
    """P27 defines the same three values in `evidence.py::CLASSES`; this pins our side of the contract.

    The two branches land independently, so neither can import the other yet. If someone renames a
    value here, P27's message levels and these KB levels silently stop being the same vocabulary.
    """
    assert EVIDENCE_CLASSES == ("local", "paired", "online_receipt")
    assert (EVIDENCE_CLASSES[0], EVIDENCE_CLASSES[1], EVIDENCE_CLASSES[2]) == ("local", "paired", "online_receipt")
    assert STRONG_EVIDENCE_CLASSES == ("paired", "online_receipt")
    from opsb_core.mcp import handlers_kb
    from opsb_core.mcp.schemas import TOOL_SCHEMAS

    assert handlers_kb.EVIDENCE_CLASSES is EVIDENCE_CLASSES  # one definition on this branch
    for name in ("parhelion_kb_record", "parhelion_kb_feedback"):
        schema = next(t for t in TOOL_SCHEMAS if t["name"] == name)
        assert schema["inputSchema"]["properties"]["evidence_class"]["enum"] == ["local", "paired", "online_receipt"]


def test_gate_hint_module_stays_small() -> None:
    """New modules are capped at 350 lines (10-DSH §2); the gate hint matcher is one of them."""
    hints_py = REPO_ROOT / "cli" / "opsb_core" / "knowledge" / "hints.py"
    assert len(hints_py.read_text(encoding="utf-8").splitlines()) <= 350
