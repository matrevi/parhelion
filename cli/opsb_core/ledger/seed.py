"""The code-assembled compaction seed (4.2 C #5, research R4): ``agents.seed_scheme=ledger_full``.

Nothing in it is written by the model except the validated narrative, so a failed handoff cannot
corrupt the rules, the ids or the numbers. Order (each section is a block of the finished text):

1. ``RULES``      the contest rules section verbatim, the admin directives sent to this worker, and the
                  platform rules line; every block is also pinned as a ``constraint`` ledger entry, so a
                  later compaction cannot drop it;
2. ``STATE``      the extended state card (``services.state_card``);
3. ``LEDGER``     the capped digest of ``ledger.digest``;
4. ``HANDOFF``    the model narrative, at most 1500 characters under the five fixed headings (``handoff``);
5. ``RAW TAIL``   the last rounds verbatim (bounded), then ``git status``, ``git diff --stat`` and the
                  paths of the files being edited;
6. the action directive, which replaces ``CHECKPOINT_TAIL``.

Budget: ``agents.seed_budget_chars`` (default 8000, clamped 3000..20000), targeting 6-10K characters.
When the text does not fit, the raw tail goes first, then the dead ends of the digest (the digest falls
back to bare ids), then the narrative shortens. The rules block and the ids of the live entries are never
cut.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

from .. import config as _cfg
from . import checkpoint, digest, gitref

LEGACY, CORE, FULL = "legacy", "ledger_core", "ledger_full"
SCHEMES = (LEGACY, CORE, FULL)
#: Set from the P06 replay (docs/plan-4.1-4.2/reports/P06-replay.md): ``ledger_full`` tied ``current``
#: (0.6419 against 0.6419, paired 95% CI [-0.032, +0.032]), so ``ledger_core`` stays the default.
DEFAULT_SCHEME = CORE
ACTION = ("Take the highest-priority open hypothesis or unexploited insight above and say its id; call "
          "parhelion_recall before re-deriving anything; then run the test. If you re-read a file, say why.")
PREFIX = "Preserve this checkpoint and await the next task."
HEADERS = {
    "rules": "RULES (verbatim; a constraint entry pins each block)",
    "state": "STATE",
    "ledger": "LEDGER (code-built from the ledger)",
    "handoff": "HANDOFF (the previous session's narrative, checked against the ledger)",
    "raw": "RAW TAIL (recent rounds, then the worktree)",
}
RULE_HEADING = re.compile(r"^\s*(?:rules?|constraints?|scoring rules?|计分规则|规则|约束|提交规则)\b", re.I)
RULE_WORD = re.compile(r"\b(rules?|constraints?|scoring)\b|规则|约束", re.I)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
RULES_MAX = 1500
DIRECTIVE_MAX = 2
TAIL_TURNS = 2
TAIL_CHARS = 1600
STATUS_MAX = 600
DIFF_MAX = 400
PATH_MAX = 400
MIN_LEDGER_CHARS = 240


def scheme(cfg: dict[str, Any] | None = None) -> str:
    """Validated ``agents.seed_scheme``; ``DEFAULT_SCHEME`` when unset, unknown or the config is unreadable."""
    try:
        doc = cfg if cfg is not None else _cfg.ConfigRepository.load()
    except Exception:
        doc = {}
    section = (doc or {}).get("agents") if isinstance(doc, dict) else None
    value = str(section.get("seed_scheme") or "").strip().lower() if isinstance(section, dict) else ""
    return value if value in SCHEMES else DEFAULT_SCHEME


def rules_text(workspace: Path | str) -> list[str]:
    """The rule blocks of the contest guide, verbatim: its rules sections, else the whole guide."""
    path = Path(workspace).expanduser() / "AGENTS.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    picked = [(h, body) for h, body in _sections(text) if h and RULE_WORD.search(h)]
    blocks = [f"## {h}\n{body}".strip() for h, body in picked] or [text.strip()]
    return [_clip_verbatim("\n\n".join(blocks), RULES_MAX)]


def _sections(text: str) -> list[tuple[str, str]]:
    """``[(heading, body)]`` of a markdown text; a pre-heading preamble has an empty heading."""
    out: list[tuple[str, str]] = []
    heading, body = "", []
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            out.append((heading, "\n".join(body).strip()))
            heading, body = m.group(1), []
        else:
            body.append(line)
    out.append((heading, "\n".join(body).strip()))
    return [(h, b) for h, b in out if h or b]


def platform_rules() -> str:
    from ..services.state_card import RULES_LINE
    return RULES_LINE


def _bus_rows(workspace: Path | str, agent_id: str, *, kind: str, limit: int) -> list[str]:
    """Bodies of the newest bus messages of one kind (read-only; a missing bus is not an error)."""
    import sqlite3
    from ..agents.bus_store import agent_bus_db_path
    try:
        db = agent_bus_db_path(Path(workspace))
    except OSError:
        return []
    if not agent_id or not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute("SELECT body FROM messages WHERE agent_id = ? AND kind = ? "
                            "ORDER BY seq DESC LIMIT ?", (str(agent_id), kind, int(limit))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [str(row[0] or "").strip() for row in reversed(rows) if str(row[0] or "").strip()]


def admin_directives(workspace: Path | str, agent_id: str, limit: int = DIRECTIVE_MAX) -> list[str]:
    """The last ``limit`` ``directive`` messages the bus sent this worker, quoted verbatim."""
    return [_clip_verbatim(text, 600) for text in _bus_rows(workspace, agent_id, kind="directive", limit=limit)]


def recent_rounds(workspace: Path | str, agent_id: str, *, turns: int = TAIL_TURNS) -> str:
    """The last ``turns`` replies of this worker verbatim (bounded by ``TAIL_CHARS``).

    The bus stores every turn's reply as a ``result`` message, so the raw tail of a compaction is the
    end of the conversation the new session would otherwise lose.
    """
    parts = _bus_rows(workspace, agent_id, kind="result", limit=turns)
    return checkpoint.tail_of("\n\n".join(parts), TAIL_CHARS) if parts else ""


def worktree_state(worktree: Path | str) -> str:
    """``git status --short``, ``git diff --stat`` and the edited paths of the worktree, bounded."""
    worktree = Path(worktree).expanduser()
    if not worktree.is_dir():
        return ""
    # Not ``.strip()``: it would eat the leading space that makes " M src/x" a modified file.
    status = (gitref.git_out(worktree, "status", "--short") or "").strip("\n").rstrip()
    diff = (gitref.git_out(worktree, "diff", "--stat", "HEAD") or "").strip("\n").rstrip()
    edited = [ln[3:].strip() for ln in status.splitlines() if len(ln) > 3 and ln[3:].strip()]  # "XY path"
    parts: list[str] = []
    if status:
        parts.append("git status:\n" + _clip_tail(status, STATUS_MAX))
    if diff:
        parts.append("git diff --stat:\n" + _clip_tail(diff, DIFF_MAX))
    if edited:
        parts.append("edited: " + _clip(", ".join(edited), PATH_MAX))
    return "\n".join(parts)


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def _clip_verbatim(text: str, limit: int) -> str:
    """Cut to ``limit`` characters without touching the whitespace: a rules block is quoted as written."""
    body = str(text or "").strip()
    return body if len(body) <= limit else body[: max(0, limit - 1)] + "…"


def _clip_tail(text: str, limit: int) -> str:
    text = text.strip("\n").rstrip()
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def rules_block(rules: Iterable[str]) -> str:
    return "\n".join(str(r).strip() for r in rules if str(r or "").strip())


def _render(parts: list[tuple[str, str]]) -> str:
    out: list[str] = []
    for header, body in parts:
        if not str(body).strip():
            continue
        out.append((header + "\n" + str(body).strip()) if header else str(body).strip())
    return "\n\n".join(out)


def assemble(*, rules: Iterable[str], card: str, ledger: str, narrative: str, tail: str,
             worktree: str = "", budget: int | None = None, action: str = ACTION,
             ledger_min: str = "") -> str:
    """The finished seed text, at most ``budget`` characters.

    The fixed sections come first; the narrative and the raw tail then fill what is left, the narrative
    taking at most half of it. When even that does not fit, the tail goes, then the digest falls back to
    its bare-id form (``ledger_min``), then the narrative shortens. The rules and the live ids never go.
    """
    budget = checkpoint.seed_budget() if budget is None else budget
    action = str(action or "").strip()
    narrative = str(narrative or "").strip()
    tail_parts = [p for p in (str(tail or "").strip(), worktree.strip()) if p]

    def rendered(ledger_variant: str, narr: str, raw: str) -> str:
        return _render([("", PREFIX), (HEADERS["rules"], rules_block(rules)), (HEADERS["state"], card),
                        (HEADERS["ledger"], ledger_variant), (HEADERS["handoff"], narr),
                        (HEADERS["raw"], raw), ("", action)])

    best = ""
    for ledger_variant in (ledger, ledger_min or ledger):
        empty = rendered(ledger_variant, "", "")
        room = budget - len(empty)  # what is left for the narrative and the raw tail, minus separators
        narr = narrative[: max(0, min(len(narrative), room // 2))] if narrative else ""
        tail_room = max(0, room - len(narr) - (2 if narr else 0) - 2)
        raw = checkpoint.tail_of("\n".join(tail_parts), tail_room) if tail_parts else ""
        text = rendered(ledger_variant, narr, raw)
        for _ in range(8):
            if len(text) <= budget:
                return text
            overflow = len(text) - budget
            if raw:
                size = max(0, len(raw) - overflow - 4)
                raw = checkpoint.tail_of("\n".join(tail_parts), size) if size else ""
            elif narr:
                narr = narr[: max(0, len(narr) - overflow - 4)]
            else:
                break
            text = rendered(ledger_variant, narr, raw)
        best = best or text
        if len(text) <= budget:
            return text
    return best


def build(workspace: Path | str, problem: str, agent_id: str, rows: list[dict[str, Any]], *,
          card: str = "", narrative: str = "", stage: str = "", worktree: Path | str = "",
          budget: int | None = None, rules: list[str] | None = None) -> str:
    """The production seed: rules, extended card, capped digest, narrative, raw tail, action directive."""
    ws = Path(workspace).expanduser()
    budget = checkpoint.seed_budget() if budget is None else budget
    fixed_rules = rules if rules is not None else (
        rules_text(ws) + admin_directives(ws, agent_id) + [platform_rules()])
    block = _digest(rows, agent_id, max(800, budget // 3))
    block_min = _digest(rows, agent_id, MIN_LEDGER_CHARS)
    # "the last rounds verbatim": the stored replies of the last turns and the last stage report.
    tail = "\n\n".join(p for p in (recent_rounds(ws, agent_id), str(stage or "").strip()) if p)
    card = "\n".join(p for p in (card or card_of(ws, problem, agent_id), guide_line(ws, agent_id)) if p)
    return assemble(rules=fixed_rules, card=card, ledger=block,
                    ledger_min=block_min, narrative=narrative, tail=tail,
                    worktree=worktree_state(worktree) if worktree else "", budget=budget)


def _digest(rows: list[dict[str, Any]], agent_id: str, max_chars: int) -> str:
    block = digest.render(rows, own=agent_id, max_chars=max_chars, header="")
    missing = digest.ids_missing(block, rows, agent_id)
    return block + ("\nAlso open: " + ", ".join(missing) if missing else "")


def guide_line(workspace: Path | str, agent_id: str) -> str:
    """4.4 P32: the seat's guide card as one line (id and status); the dossier itself never enters a seed."""
    try:
        from ..guidance.monitor import seed_line
        return seed_line(workspace, agent_id) if agent_id else ""
    except Exception:
        return ""


def card_of(workspace: Path, problem: str, agent_id: str = "") -> str:
    """The extended state card, or the simple one when this tree's card has no extension yet."""
    from ..services import state_card
    try:
        return state_card.build(workspace, problem, agent_id, extended=True)
    except TypeError:
        return state_card.build(workspace, problem, agent_id)


def pin(ledger: Any, rules: Iterable[str]) -> list[dict[str, Any]]:
    """Pin every fixed rule block as a ``constraint`` row (idempotent on the block's hash)."""
    out = []
    for text in rules:
        body = str(text or "").strip()
        if not body:
            continue
        ekey = "constraint:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        out.append(ledger.append("constraint", {"claim": body, "text": body, "rule_source": "seed"}, ekey=ekey))
    return out
