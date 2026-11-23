"""Non-destructive Triad KB template seeding and read-only (`0400`) skill projection."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import SUPERVISOR_ROOT, read_json
from .store import (
    WRITABLE_PILLARS,
    _atomic_write_text,
    _copy_if_missing,
    _with_index_lock,
    triad_kb_dirs,
)


def ensure_triad_kb_seeded(
    parhelion_home: Path | None = None,
    templates_root: Path | None = None,
) -> dict[str, Any]:
    """Non-destructively seed snippets/ and community/ from templates/ if missing."""
    dirs = triad_kb_dirs(parhelion_home)
    home = dirs["home"]
    tpl_root = Path(templates_root).expanduser().resolve() if templates_root is not None else (SUPERVISOR_ROOT / "templates")

    home.mkdir(parents=True, exist_ok=True)
    copied_files: list[str] = []
    seeded_pillars: list[str] = []
    skipped_pillars: list[str] = []

    for pillar in WRITABLE_PILLARS:
        src_dir, dst_dir = tpl_root / pillar, dirs[pillar]
        (dst_dir / ("code" if pillar == "snippets" else "entries")).mkdir(parents=True, exist_ok=True)
        src_idx, dst_idx = src_dir / "index.json", dst_dir / "index.json"

        if not dst_idx.is_file():
            if src_idx.is_file():
                def _seed_idx(cur: dict[str, Any], _src: Path = src_idx) -> tuple[dict[str, Any], bool]:
                    loaded = read_json(_src, {"schema_version": 1, "entries": []})
                    if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                        return loaded, True
                    return cur, False

                if _with_index_lock(dst_idx, _seed_idx):
                    seeded_pillars.append(pillar)
                    copied_files.append(str(dst_idx))
            else:
                _with_index_lock(dst_idx, lambda cur: (cur, True))
                seeded_pillars.append(pillar)
        else:
            skipped_pillars.append(pillar)

        if src_dir.is_dir():
            for sub_name in ("code", "entries"):
                src_sub = src_dir / sub_name
                if src_sub.is_dir():
                    for src_file in sorted(src_sub.iterdir()):
                        if src_file.is_file() and _copy_if_missing(src_file, dst_dir / sub_name / src_file.name):
                            copied_files.append(str(dst_dir / sub_name / src_file.name))
            for src_file in sorted(src_dir.iterdir()):
                if src_file.is_file() and src_file.suffix in (".h", ".md"):
                    dst_file = dst_dir / src_file.name
                    if _copy_if_missing(src_file, dst_file):
                        copied_files.append(str(dst_file))

    return {
        "ok": True,
        "parhelion_home": str(home),
        "templates_root": str(tpl_root),
        "seeded_pillars": seeded_pillars,
        "skipped_pillars": skipped_pillars,
        "copied_files": copied_files,
        "copied_count": len(copied_files),
    }


def project_readonly_skills(
    workspace: Path,
    target_rel: str = ".agents/skills",
    parhelion_home: Path | None = None,
) -> dict[str, Any]:
    """Project canonical read-only (0400) KB skill files into <workspace>/<target_rel>/parhelion-kb/."""
    from .query import _load_all_entries

    ws = Path(workspace).expanduser().resolve()
    dirs, entries = _load_all_entries(parhelion_home=parhelion_home, auto_seed=True)
    dest_dir = ws / target_rel / "parhelion-kb"
    snippets_dest = dest_dir / "snippets"
    dest_dir.mkdir(parents=True, exist_ok=True)
    snippets_dest.mkdir(parents=True, exist_ok=True)

    written_files: list[str] = []
    skill_md = (
        "---\n"
        "name: parhelion-kb\n"
        "description: Read-only canonical KernelC 4-Pillar Knowledge Base index (Breakthroughs, Snippets, Hardware Laws, Community & Anti-Patterns).\n"
        "---\n\n"
        "# Parhelion 4-Pillar Knowledge Base (`parhelion-kb`)\n\n"
        "- Query dynamically by bottleneck/regime/dtype via `opsb kb query` or MCP `parhelion_kb_query`.\n"
        "- Pillar distinction: `hardware` = silicon/doc/compiler-discovered hardware laws & traps; `community` = ready-made community/cross-platform acceleration algorithms; `snippets` = reusable KernelC `.h` blocks; `breakthrough` = verified online wins.\n"
        "- Record new verified patterns or negative anti-patterns only with verifiable `problem`, `sha`, `verdict`, and measurable metrics (`opsb kb record` / `opsb kb feedback`).\n"
        "- Inspect `KB_INDEX.md` and `snippets/*.h` in this directory for read-only verified building blocks.\n"
    )
    skill_path = dest_dir / "SKILL.md"
    _atomic_write_text(skill_path, skill_md, mode=0o400)
    written_files.append(str(skill_path))

    anti = [e for e in entries if e["status"] == "anti-pattern" or e["portability"] == "anti-pattern"]
    snips = [e for e in entries if e["pillar"] == "snippets" and e not in anti]
    hws = [e for e in entries if e["pillar"] == "hardware" and e not in anti]
    bts = [e for e in entries if e["pillar"] == "breakthrough" and e not in anti]
    coms = [e for e in entries if e["pillar"] == "community" and e not in anti]

    idx_lines = [
        "# Parhelion 4-Pillar KB Snapshot Index (Read-Only)",
        "",
        f"- Total entries: `{len(entries)}` (`breakthrough={len(bts)}`, `snippets={len(snips)}`, `hardware={len(hws)}`, `community={len(coms)}`, `anti-patterns={len(anti)}`)",
        "",
        "## Anti-Patterns (DO NOT REPEAT)",
        *(f"- `{e['id']}` [{e['pillar']}] (fails={e['fails']}): {e['title']} — {e['summary']}" for e in anti),
        "",
        "## Accel Hardware Laws & Silicon Findings",
        *(f"- `{e['id']}` [{e['status']}|conf={e['confidence']:.2f}]: {e['title']}" for e in hws),
        "",
        "## Verified KernelC Snippets",
    ]
    for e in snips:
        idx_lines.append(f"- `{e['id']}` (conf={e['confidence']:.2f}, {e['status']}): {e['title']} (`snippets/{e['id']}.h`)")
        abs_doc = e.get("abs_doc_path")
        if abs_doc and Path(abs_doc).is_file():
            dst_h = snippets_dest / f"{e['id']}.h"
            _atomic_write_text(dst_h, Path(abs_doc).read_text(encoding="utf-8", errors="replace"), mode=0o400)
            written_files.append(str(dst_h))

    idx_lines.extend([
        "",
        "## Community Cards",
        *(f"- `{e['id']}` [{e['portability']}|{e['status']}|conf={e['confidence']:.2f}]: {e['title']}" for e in coms),
    ])
    if bts:
        idx_lines.extend([
            "",
            "## Top Verified Breakthroughs",
            *(
                f"- `{e['id']}` ({e.get('problem', '')} {float(e.get('delta_raw', 0.0)):+.2f} pts): {e['title']}"
                for e in sorted(bts, key=lambda x: float(x.get("delta_raw", 0.0)), reverse=True)[:15]
            ),
        ])

    kb_idx_path = dest_dir / "KB_INDEX.md"
    _atomic_write_text(kb_idx_path, "\n".join(idx_lines) + "\n", mode=0o400)
    written_files.append(str(kb_idx_path))

    claude_skills = ws / ".claude" / "skills"
    claude_symlink_status = "unchanged"
    canonical_skills = (ws / target_rel).resolve()
    if not claude_skills.exists() and not claude_skills.is_symlink():
        claude_skills.parent.mkdir(parents=True, exist_ok=True)
        rel_target = os.path.relpath(canonical_skills, claude_skills.parent)
        claude_skills.symlink_to(rel_target)
        claude_symlink_status = f"created->{rel_target}"
    elif claude_skills.is_symlink():
        claude_symlink_status = f"existing->{os.readlink(claude_skills)}"
    elif claude_skills.is_dir():
        sub_link = claude_skills / "parhelion-kb"
        if not sub_link.exists() and not sub_link.is_symlink():
            rel_sub = os.path.relpath(dest_dir, claude_skills)
            sub_link.symlink_to(rel_sub)
            claude_symlink_status = f"linked-sub->{rel_sub}"
        else:
            claude_symlink_status = "existing-dir"

    return {
        "ok": True,
        "workspace": str(ws),
        "parhelion_home": str(dirs["home"]),
        "target_dir": str(dest_dir),
        "file_mode": "0400",
        "written_files": written_files,
        "file_count": len(written_files),
        "claude_skills_symlink": claude_symlink_status,
    }
