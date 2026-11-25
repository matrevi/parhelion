"""Idempotent migration separating Accel hardware-discovered laws from community acceleration patterns.

Boundary between pillars:
- `community` (`COM-*`): ready-made community / cross-platform acceleration algorithms and
  CUDA-to-Accel remapping schemes (e.g., Online Welford/Softmax, Diagonal L2 Swizzle,
  CUDA warp-shuffle/float4/grid-stride remaps, general operator algorithmic designs).
- `hardware` (`HW-*`): hardware laws, ISA/intrinsic quirks, Compiler/LLVM compiler & ABI
  redlines, L1I/L2/UB micro-architecture traps, DVFS/core-scaling laws, and silicon-verified
  anti-patterns discovered by probing Accel hardware docs and running silicon/online tests.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from ..config import SUPERVISOR_ROOT, read_json, utc_now
from ..device_env import device_text
from .evolution import _render_community_markdown
from .seeding import ensure_triad_kb_seeded
from .store import (
    _append_evolution_event,
    _atomic_write_text,
    _copy_if_missing,
    _new_event_id,
    _resolve_doc_path,
    _with_index_lock,
    triad_kb_dirs,
)

# Canonical off-the-shelf community / CUDA-to-Accel remap entries that ALWAYS stay in `community`.
_CANONICAL_COMMUNITY_IDS: frozenset[str] = frozenset({
    "com-online-welford-softmax",
    "com-diagonal-l2-swizzle",
    "com-warp-shuffle-to-5hd-ub",
    "com-vectorized-float4-to-32b-burst",
    "com-cuda-grid-stride-to-divisor-cores",
    "com-cuda-fine-branch-anti-pattern",
})

# Strong hardware/silicon/ISA/compiler/SDK-runtime law markers discovered via docs or silicon benchmarks.
# Entry blobs are lower-cased before matching, so the configured part is lower-cased too.
_SOC_TOKEN = re.escape(device_text("soc").lower())
_HARDWARE_DISCOVERY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("silicon-dvfs-core-wall", r"\b(10-?core|setblockdim\s*\(|halfcores|dvfs|dispatch wall|launch floor|32c/40c|>20-core|20-40 core|slotstride|false sharing|quartercores|byte-range split)\b"),
    ("l1i-elf-linker-abi", r"\b(16\s*kb|l1i|0x0000|aiv l1i|i-?cache|icache|comdat|multi-tu|linker layout|elf diet|\.o_max|symtab|hbm-?stack|stack spill|register-?spill|sroa|<=?7-?arg|7-?arg|3-reg-arg|zero-stack|hang fix|unroll\s*\(\s*disable\s*\)|inner-?loop\s+noinline|never call noinline|always_inline|preload\s*\()\b"),
    ("isa-intrinsic-microarch", rf"\b(dav[-_]c220|accel\s*{_SOC_TOKEN}?|sdk\s*9\.1|copy_gm_to_ubuf_align|dmainpadded|event_id\s*0|8-slot|ring5|zero-exit-drain|set_va_reg_sb|scatter_vnchwconv|vtranspose\s+16x16|bank-stagger|vector_dup|uint8_t\s+repeat|14-bit\s+param1|hardware\s+leftpad|fetcheventid|mte3->mte2|self-bypass|180s\s+npu\s+hang|idle-core\s+early\s+return|capability\s+bailout|scalar-ub-read|v_s\s+pipe|zero-v_s)\b"),
    ("mte-pipeline-silicon-disproof", r"\b(axismid\s+128kb|pre-queued\s+mte2|pack3c\s+anti-pattern|round-2\s+disproofs|inline\s+multi-branch\s+raw\s+vector)\b"),
)


def classify_community_entry_is_hardware(entry: dict[str, Any]) -> tuple[bool, str]:
    """Return `(is_hardware, reason)` classifying a `community` entry cleanly."""
    eid = str(entry.get("id") or "").strip().lower()
    if eid in _CANONICAL_COMMUNITY_IDS:
        return False, "canonical-community-scheme"

    portability = str(entry.get("portability") or "").strip().lower()
    if portability == "remap-to-accel":
        return False, "cuda-remap-community-scheme"

    title = str(entry.get("title") or "")
    summary = str(entry.get("summary") or "")
    tags = " ".join(str(t) for t in (entry.get("tags") or []))
    bottlenecks = " ".join(str(b) for b in (entry.get("bottlenecks") or []))
    blob = f"{title}\n{summary}\n{tags}\n{bottlenecks}".lower()

    matched_reasons: list[str] = []
    for reason, pat in _HARDWARE_DISCOVERY_PATTERNS:
        if re.search(pat, blob):
            matched_reasons.append(reason)

    if matched_reasons:
        return True, "+".join(matched_reasons)
    return False, "algorithmic-community-pattern"


def _to_hardware_id(old_id: str) -> str:
    if old_id.upper().startswith("HW-"):
        return old_id
    if old_id.upper().startswith("COM-"):
        return "HW-" + old_id[4:]
    return f"HW-{old_id}"


def _extract_markdown_body(md_path: Path | None) -> str:
    if md_path is None or not md_path.is_file():
        return ""
    raw_md = md_path.read_text(encoding="utf-8", errors="replace")
    parts = raw_md.split("---\n", 2) if raw_md.startswith("---\n") else []
    body = parts[2] if len(parts) == 3 else raw_md
    body = re.split(r"\n## Evidence Ledger\n", body, maxsplit=1)[0]
    body = re.sub(r"^#\s+.*\n+", "", body.lstrip(), count=1).strip()
    return body


def migrate_hardware_entries_from_community(
    parhelion_home: Path | None = None,
    *,
    dry_run: bool = False,
    seed_missing_templates: bool = True,
    templates_root: Path | None = None,
) -> dict[str, Any]:
    """Idempotently move hardware-discovered entries from `community/` to `hardware/`.

    - Preserves all evidence, confidence, wins/fails, tags, and markdown bodies.
    - Renames `COM-*` -> `HW-*` and stores `legacy_id` so lookups/feedback by old IDs keep working.
    - Optionally merges missing canonical hardware seed entries from `templates/hardware/index.json`.
    """
    dirs = triad_kb_dirs(parhelion_home)
    home = dirs["home"]
    tpl_root = Path(templates_root).expanduser().resolve() if templates_root is not None else (SUPERVISOR_ROOT / "templates")
    if not dry_run:
        ensure_triad_kb_seeded(parhelion_home=home, templates_root=tpl_root)

    com_dir, hw_dir = dirs["community"], dirs["hardware"]
    com_idx_path, hw_idx_path = com_dir / "index.json", hw_dir / "index.json"
    com_raw = read_json(com_idx_path, {"schema_version": 1, "entries": []})
    hw_raw = read_json(hw_idx_path, {"schema_version": 1, "entries": []})

    com_entries = [e for e in (com_raw.get("entries") if isinstance(com_raw, dict) else []) if isinstance(e, dict)]
    hw_entries = [e for e in (hw_raw.get("entries") if isinstance(hw_raw, dict) else []) if isinstance(e, dict)]

    to_move: list[dict[str, Any]] = []
    stay_community: list[dict[str, Any]] = []
    classifications: list[dict[str, Any]] = []

    for ent in com_entries:
        is_hw, reason = classify_community_entry_is_hardware(ent)
        old_id = str(ent.get("id") or "")
        new_id = _to_hardware_id(old_id) if is_hw else old_id
        classifications.append({
            "old_id": old_id,
            "new_id": new_id,
            "target_pillar": "hardware" if is_hw else "community",
            "reason": reason,
            "title": str(ent.get("title") or ""),
        })
        if is_hw:
            to_move.append(ent)
        else:
            stay_community.append(ent)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "parhelion_home": str(home),
            "community_before": len(com_entries),
            "hardware_before": len(hw_entries),
            "to_move_count": len(to_move),
            "remaining_community_count": len(stay_community),
            "classifications": classifications,
        }

    (hw_dir / "entries").mkdir(parents=True, exist_ok=True)
    migrated_ids: list[dict[str, str]] = []
    docs_to_write: list[tuple[Path, str]] = []
    old_docs_to_unlink: list[Path] = []

    def _merge_into_hw(cur: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
        existing = [e for e in (cur.get("entries") or []) if isinstance(e, dict)]
        by_id = {str(e.get("id") or "").lower(): i for i, e in enumerate(existing)}
        by_legacy = {str(e.get("legacy_id") or "").lower(): i for i, e in enumerate(existing) if e.get("legacy_id")}
        moved_log: list[dict[str, str]] = []

        for raw_ent in to_move:
            old_id = str(raw_ent.get("id") or "").strip()
            new_id = _to_hardware_id(old_id)
            old_doc = _resolve_doc_path(com_dir, str(raw_ent.get("doc_path") or ""), old_id, "community")
            body = _extract_markdown_body(old_doc)

            hw_ent = copy.deepcopy(raw_ent)
            hw_ent["id"] = new_id
            if old_id.lower() != new_id.lower():
                hw_ent["legacy_id"] = old_id
            hw_ent["pillar"] = "hardware"
            if str(hw_ent.get("category") or "").strip() in ("", "vectorization"):
                hw_ent["category"] = "hardware-law"
            if str(hw_ent.get("portability") or "").strip() == "portable":
                hw_ent["portability"] = "native-accel"
            hw_ent["doc_path"] = f"entries/{new_id}.md"

            idx_hit = by_id.get(new_id.lower())
            if idx_hit is None:
                idx_hit = by_legacy.get(old_id.lower())

            if idx_hit is not None:
                # Merge richer live evidence/stats over template seed if target already exists
                prev = existing[idx_hit]
                prev_evs = list(prev.get("evidence") or [])
                new_evs = list(hw_ent.get("evidence") or [])
                if len(new_evs) >= len(prev_evs):
                    existing[idx_hit] = hw_ent
            else:
                by_id[new_id.lower()] = len(existing)
                by_legacy[old_id.lower()] = len(existing)
                existing.append(hw_ent)

            docs_to_write.append((hw_dir / "entries" / f"{new_id}.md", _render_community_markdown(hw_ent, body=body)))
            if old_doc and old_doc.is_file() and old_doc.parent == (com_dir / "entries").resolve():
                old_docs_to_unlink.append(old_doc)
            moved_log.append({"old_id": old_id, "new_id": new_id})

        # Optionally merge canonical template hardware laws that do not exist yet
        if seed_missing_templates:
            tpl_hw_dir = tpl_root / "hardware"
            tpl_hw_idx = tpl_hw_dir / "index.json"
            if tpl_hw_idx.is_file():
                tpl_loaded = read_json(tpl_hw_idx, {"entries": []})
                for tpl_ent in (tpl_loaded.get("entries") if isinstance(tpl_loaded, dict) else []) or []:
                    if not isinstance(tpl_ent, dict) or not tpl_ent.get("id"):
                        continue
                    tid = str(tpl_ent["id"]).lower()
                    tleg = str(tpl_ent.get("legacy_id") or "").lower()
                    if tid in by_id or (tleg and tleg in by_legacy):
                        continue
                    by_id[tid] = len(existing)
                    existing.append(copy.deepcopy(tpl_ent))
                    src_md = tpl_hw_dir / str(tpl_ent.get("doc_path") or f"entries/{tpl_ent['id']}.md")
                    if src_md.is_file():
                        _copy_if_missing(src_md, hw_dir / "entries" / src_md.name)

        cur["entries"] = existing
        return cur, moved_log

    migrated_ids = _with_index_lock(hw_idx_path, _merge_into_hw)

    for dst_p, md_text in docs_to_write:
        _atomic_write_text(dst_p, md_text)

    moved_old_set = {m["old_id"].lower() for m in migrated_ids}
    if moved_old_set:
        def _prune_community(cur: dict[str, Any]) -> tuple[dict[str, Any], int]:
            before = [e for e in (cur.get("entries") or []) if isinstance(e, dict)]
            kept = [e for e in before if str(e.get("id") or "").lower() not in moved_old_set]
            cur["entries"] = kept
            return cur, len(before) - len(kept)

        _with_index_lock(com_idx_path, _prune_community)
        for old_p in old_docs_to_unlink:
            old_p.unlink(missing_ok=True)

        event = {
            "event_id": _new_event_id(),
            "timestamp": utc_now().isoformat(),
            "action": "migrate",
            "transition": "community->hardware",
            "pillar": "hardware",
            "entry_id": f"bulk:{len(migrated_ids)}",
            "migrated_ids": migrated_ids,
        }
        _append_evolution_event(home / ".kb-evolution.lock", dirs["evolution_log"], event)

    final_com = len((read_json(com_idx_path, {"entries": []}) or {}).get("entries") or [])
    final_hw = len((read_json(hw_idx_path, {"entries": []}) or {}).get("entries") or [])
    return {
        "ok": True,
        "dry_run": False,
        "parhelion_home": str(home),
        "migrated_count": len(migrated_ids),
        "migrated_ids": migrated_ids,
        "hardware_total": final_hw,
        "community_total": final_com,
    }
