"""Worker start-up pack (4.2 G): the brief plus a code-built, byte-budgeted context pack.

The pack is assembled by code, never by a model, and its five sections are ordered by how often a
worker needs them:

1. ``contract``   the existing brief text (``brief.compose_session_brief``); the 1.5 KB cap applies
   to this section only;
2. ``worktree``   the absolute worktree path plus the per-driver rule for passing ``workdir`` instead
   of ``cd X && ...``;
3. ``repo map``   research R11: **off by default**, because the study found generated overviews raised
   cost 20-23% without a measurable gain. When enabled, it is the compressed ``F`` fields of
   ``aoci.txt`` in the worktree, or a code-built one-line-per-file map when there is none;
4. ``files``      the 3-5 files to edit with line ranges, from ``git diff`` of the current best SHA
   against the problem-template baseline, plus the kernel entry files;
5. ``ledger``     ``ledger.digest.render`` plus the per-case evidence of ``services.state_card_ext``.

The whole pack is capped in UTF-8 **bytes** (``bytes_per_token`` * token budget, configurable through
``startup_pack.tokens`` or ``PARHELION_STARTUP_PACK_TOKENS``). When it does not fit, sections shrink
in the order 5, 3, 4; sections 1 and 2 are never cut because the task and the path are what make the
first platform call possible at all.
"""
from __future__ import annotations

import json as _json
import os
import re
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..ledger import digest as _digest
from ..ledger import store as _ledger_store
from ..ledger.gitref import git_out
from .brief import _template_layout, _truncate_utf8, resolve_problem_repo
from .change_targets import change_targets, section_files as _section_files, template_baseline
from .repo_map import MAP_MIN_BYTES, repo_map

BYTES_PER_TOKEN = 3.6  # a documented estimate, not a tokenizer: ~4 bytes/token for English, ~3 for code
DEFAULT_TOKENS = 6000
MIN_TOKENS = 1200
MAX_TOKENS = 24000
SECTION_ORDER = (1, 2, 3, 4, 5)
TRUNCATE_ORDER = (5, 3, 4)  # spec order: ledger first, then the repo map, then the file list
SECTION_NAMES = {1: "Task contract", 2: "Worktree", 3: "Repo map", 4: "Files to change",
                 5: "Problem ledger"}
SHRINK_STEPS = (0.6, 0.35, 0.15, 0.0)  # fraction of the original section kept, 0 = dropped
CONTRACT_MAX = 1500  # the existing brief cap; it constrains this section only
DIGEST_CHARS = 2600
CARD_MAX = 3400
MIN_SECTION_BYTES = {5: 0, 3: MAP_MIN_BYTES, 4: 0}

WORKDIR_TOOLS = {
    # driver kind: (tool name, takes a workdir argument, note)
    "deepseek": ("bash", True, ""),
    "codex": ("exec_command / shell", True, ""),
    "claude": ("Bash", False, "a directory change persists in one shell call, so use absolute paths"),
    "antigravity": ("terminal", False, "the ACP agent owns its tool surface"),
    "pi": ("bash", False, "the Pi driver does not forward a workdir argument"),
}


# --- configuration -------------------------------------------------------------------------------

def config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """``{tokens, repo_map, bytes_per_token}`` from config + env, clamped; never raises."""
    try:
        doc = cfg if cfg is not None else _cfg.ConfigRepository.load()
    except Exception:
        doc = {}
    section = (doc or {}).get("startup_pack") if isinstance(doc, dict) else None
    section = section if isinstance(section, dict) else {}
    raw_tokens = os.environ.get("PARHELION_STARTUP_PACK_TOKENS") or section.get("tokens")
    try:
        tokens = int(float(raw_tokens)) if raw_tokens not in (None, "") else DEFAULT_TOKENS
    except (TypeError, ValueError):
        tokens = DEFAULT_TOKENS
    env_map = os.environ.get("PARHELION_STARTUP_REPO_MAP")
    repo_map = (str(env_map).strip().lower() in ("1", "true", "yes") if env_map not in (None, "")
                else bool(section.get("repo_map", False)))
    return {"tokens": max(MIN_TOKENS, min(MAX_TOKENS, tokens)), "repo_map": repo_map,
            "bytes_per_token": BYTES_PER_TOKEN}


def budget_bytes(tokens: int) -> int:
    return int(tokens * BYTES_PER_TOKEN)


# --- section 1: the contract ---------------------------------------------------------------------

def section_contract(contract: str) -> str:
    """The existing brief text, capped at the 1.5 KB contract budget only."""
    return _truncate_utf8(str(contract or "").strip(), CONTRACT_MAX)


# --- section 2: the worktree and the workdir rule ------------------------------------------------

def section_worktree(worktree: Path | str, branch: str = "", driver: str = "") -> str:
    """Absolute worktree path and the per-driver rule that replaces a leading ``cd``."""
    path = str(Path(worktree).expanduser().resolve())
    tool, takes_workdir, note = WORKDIR_TOOLS.get(str(driver or "").strip().lower(),
                                                  ("shell", False, "pass absolute paths"))
    if takes_workdir:
        rule = (f"`{tool}` takes `workdir`: pass `workdir={path}` (or an absolute path inside it) and never "
                f"begin a command with `cd`.")
    else:
        rule = (f"`{tool}` has no `workdir` argument: quote absolute paths under `{path}`; "
                f"{note or 'do not begin a command with cd'}.")
    lines = [f"Worktree (absolute): `{path}`" + (f" on branch `{branch}`" if branch else "") + ".", rule]
    lines.append("Every shell call starts a fresh shell, so `cd X && ...` never carries over; on another "
                 "harness use absolute paths. Edit only this worktree and leave other checkouts alone.")
    return "\n".join(lines)


# --- section 3: the repository map (R11: off by default) -----------------------------------------

def section_repo_map(worktree: Path | str, max_bytes: int = 7000, enabled: bool = False) -> dict[str, Any]:
    """The repo-map section; empty unless ``enabled`` (research R11 keeps it off by default)."""
    if not enabled:
        return {"text": "", "source": "disabled", "entries": 0}
    return repo_map(worktree, max_bytes=max(MAP_MIN_BYTES, max_bytes))


# --- section 4: the files to change --------------------------------------------------------------

def section_files(targets: dict[str, Any]) -> str:
    """Rendered by ``change_targets.section_files``; re-exported here for the pack and its tests."""
    return _section_files(targets)


# --- section 5: the problem ledger ---------------------------------------------------------------

def ledger_rows(workspace: Path | str, problem: str) -> list[dict[str, Any]]:
    try:
        return _ledger_store.read_problem(Path(workspace), problem)
    except Exception:
        return []


def section_ledger(workspace: Path | str, problem: str, agent_id: str | None = None,
                   max_chars: int = DIGEST_CHARS) -> str:
    """The code-built digest, the per-case evidence and the platform state card; never raises."""
    ws = Path(workspace).expanduser().resolve()
    parts: list[str] = []
    rows = ledger_rows(ws, problem)
    try:
        digest = _digest.render(rows, own=str(agent_id or ""), max_chars=max_chars, header="") if rows else ""
    except Exception:
        digest = ""
    parts.append(digest or "Ledger: no rows yet; record hypotheses with parhelion_note as you go.")
    try:
        from ..services import state_card_ext
        card_data = state_card_ext.data(ws, problem, agent_id)
        # A fresh worker has no receipt and no case row: the extended card would be all "n/a", and the
        # compact platform card below already carries the head, the suite and the gate line.
        if rows or card_data.get("cases") or card_data.get("receipts"):
            card = state_card_ext.render(card_data)
            parts.append("Per-case evidence (extended state card):\n" + _truncate_utf8(card, CARD_MAX))
    except Exception:
        pass
    try:
        from ..services import state_card
        parts.append(state_card.build(ws, problem, agent_id))
    except Exception:
        pass
    return "\n".join(p for p in parts if p)


# --- budget ------------------------------------------------------------------------------------

def _sizes(sections: dict[int, str]) -> dict[int, int]:
    return {sid: len(text.encode("utf-8")) for sid, text in sections.items()}


def fit(sections: dict[int, str], budget: int) -> tuple[dict[int, str], list[dict[str, Any]]]:
    """Shrink sections in ``TRUNCATE_ORDER`` until the UTF-8 total fits ``budget`` bytes."""
    out = dict(sections)
    sizes = _sizes(out)
    total = sum(sizes.values())
    cuts: list[dict[str, Any]] = []
    for sid in TRUNCATE_ORDER:
        original = sizes.get(sid, 0)
        if total <= budget or not original:
            continue
        floor = MIN_SECTION_BYTES.get(sid, 0)
        for fraction in SHRINK_STEPS:
            if total <= budget:
                break
            target = max(floor, int(original * fraction))
            current = len(out[sid].encode("utf-8"))
            if target >= current:
                continue
            marker = f"\n...(section {sid} {SECTION_NAMES[sid]} cut to {int(fraction * 100)}%; "
            marker += "read the source directly or call the matching tool)"
            if target <= len(marker.encode("utf-8")):
                body = ""
            else:
                body = _truncate_utf8(out[sid], target - len(marker.encode("utf-8")))
            out[sid] = (body + marker) if body else ""
            new_size = len(out[sid].encode("utf-8"))
            total -= current - new_size
            cuts.append({"section": sid, "from": current, "to": new_size, "fraction": fraction})
            if not out[sid]:
                break
    return out, cuts


def render(sections: dict[int, str], tokens: int) -> str:
    parts = [f"# STARTUP PACK (code-built; budget ~{tokens} tokens, {int(tokens * BYTES_PER_TOKEN)} bytes)"]
    for sid in SECTION_ORDER:
        text = sections.get(sid) or ""
        if not text:
            continue
        parts.append(f"## {sid}. {SECTION_NAMES[sid]}\n{text}")
    return "\n\n".join(parts)


def record_size(workspace: Path | str, pack: dict[str, Any], *, agent_id: str | None = None,
                role: str = "worker") -> bool:
    """Record the pack size in the P03 MCP meter so ``opsb replay metrics`` counts it (never raises)."""
    try:
        from ..metrics import mcp_meter
        return mcp_meter.record_call(
            workspace, agent_id=agent_id, role=role, tool="startup_pack", args={"sections": list(pack["sections"])},
            result_text=_json.dumps({"bytes": pack["bytes"], "tokens": pack["tokens"],
                                     "budget_bytes": pack["budget_bytes"],
                                     "sections": {k: v["bytes"] for k, v in pack["sections"].items()},
                                     "repo_map": pack["repo_map_source"]}, separators=(",", ":")),
            ms=0.0, is_error=False)
    except Exception:
        return False


def build(
    workspace: Path | str,
    problem: str,
    *,
    contract: str,
    worktree: Path | str | None = None,
    branch: str = "",
    role: str = "worker",
    agent_id: str | None = None,
    driver: str = "",
    tokens: int | None = None,
    repo_map_enabled: bool | None = None,
    best_sha: str = "",
) -> dict[str, Any]:
    """Assemble the pack and report its per-section byte cost (used by tests and the replay)."""
    ws = Path(workspace).expanduser().resolve()
    cfg = config()
    token_budget = int(tokens or cfg["tokens"])
    _, repo_dir, _ = resolve_problem_repo(ws, problem)
    wt = Path(worktree).expanduser().resolve() if worktree else repo_dir
    want_map = cfg["repo_map"] if repo_map_enabled is None else bool(repo_map_enabled)
    sections: dict[int, str] = {1: section_contract(contract), 2: section_worktree(wt, branch, driver)}
    if want_map:
        sections[3] = section_repo_map(wt, max_bytes=int(budget_bytes(token_budget) * 0.35),
                                       enabled=True)["text"]
    files = change_targets(wt, best_sha=best_sha) if wt.is_dir() else {"files": [], "base": "", "best": ""}
    sections[4] = section_files(files)
    sections[5] = section_ledger(ws, problem, agent_id)
    fitted, cuts = fit(sections, budget_bytes(token_budget))
    text = render(fitted, token_budget)
    sizes = _sizes(fitted)
    return {
        "text": text,
        "sections": {SECTION_NAMES[sid]: {"text": fitted[sid], "bytes": sizes[sid]}
                     for sid in SECTION_ORDER if (fitted.get(sid) or "").strip()},
        "bytes": len(text.encode("utf-8")),
        "tokens": round(len(text.encode("utf-8")) / BYTES_PER_TOKEN, 1),
        "budget_bytes": budget_bytes(token_budget),
        "repo_map_source": (repo_map(wt)["source"] if want_map and wt.is_dir() else "disabled"),
        "cuts": cuts,
        "changed": {"base": files.get("base", ""), "best": files.get("best", ""),
                    "files": [f["path"] for f in files.get("files") or []]},
    }


__all__ = [
    "BYTES_PER_TOKEN",
    "DEFAULT_TOKENS",
    "SECTION_ORDER",
    "TRUNCATE_ORDER",
    "build",
    "budget_bytes",
    "change_targets",
    "code_entries",
    "config",
    "fit",
    "ledger_rows",
    "record_size",
    "repo_map",
    "section_contract",
    "section_files",
    "section_ledger",
    "section_repo_map",
    "section_worktree",
    "template_baseline",
]
