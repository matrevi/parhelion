"""Optional fallback skill installer targeting `dsh` and `codex` ONLY (never `claude`)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..config import SUPERVISOR_ROOT

ALLOWED_TARGETS: dict[str, Path] = {
    "dsh": Path.home() / ".dsh" / "skills",
    "codex": Path.home() / ".codex" / "skills",
}

SKILL_NAMES = ["kernelc-deep-reference"]


def get_skills_status() -> dict[str, Any]:
    targets_out: dict[str, Any] = {}
    for target, base_dir in ALLOWED_TARGETS.items():
        installed = [s for s in SKILL_NAMES if (base_dir / s / "SKILL.md").is_file()]
        targets_out[target] = {
            "target": target,
            "skills_dir": str(base_dir),
            "installed_skills": installed,
            "all_installed": len(installed) == len(SKILL_NAMES),
        }
    return {
        "mode": "agents-md-first-with-optional-fallback-skill",
        "available_skills": SKILL_NAMES,
        "allowed_targets": ["dsh", "codex"],
        "targets": targets_out,
    }


def install_skills(targets_csv: str) -> dict[str, Any]:
    requested = [t.strip().lower() for t in targets_csv.split(",") if t.strip()]
    for req in requested:
        if "claude" in req:
            raise ValueError(
                "Refused: Claude Code (~/.claude/) registration is strictly disabled; only 'dsh' and 'codex' are supported."
            )
        if req not in ALLOWED_TARGETS:
            raise ValueError(f"Unsupported target '{req}'. Allowed targets: dsh, codex")

    installed_records = []
    src_root = SUPERVISOR_ROOT / "skills"
    for target in requested:
        dest_root = ALLOWED_TARGETS[target]
        dest_root.mkdir(parents=True, exist_ok=True)
        for s_name in SKILL_NAMES:
            src_skill = src_root / s_name
            dst_skill = dest_root / s_name
            if dst_skill.is_symlink() or dst_skill.is_file():
                dst_skill.unlink()
            elif dst_skill.is_dir():
                shutil.rmtree(dst_skill)
            shutil.copytree(src_skill, dst_skill)
            installed_records.append({"target": target, "skill": s_name, "path": str(dst_skill)})
    return {"ok": True, "installed": installed_records, "status": get_skills_status()}
