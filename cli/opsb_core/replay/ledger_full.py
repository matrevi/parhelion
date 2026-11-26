"""The ``ledger_full`` seed (4.2 C #5, P06): the code-assembled seed of ``ledger/seed.py``.

What the replay can rebuild from a logged session is the same material ``ledger_core`` uses: the ledger
rebuilt from platform events (``replay/ledger_seed.reconstruct``), the logged handoff narrative, the
logged state block and the stage tail. The sections the production seed adds that no logged session can
supply are the ones P06 introduced:

- the fixed rules block (contest guide, admin directives, platform rules) — a replay has no workspace;
- the extended state card (per-case speedup, gap to tbest, targets) — the logged STATE block stands in;
- model-authored entries (insights, failures, decisions) — those sessions predate ``parhelion_note``;
- the worktree block (``git status``, ``git diff --stat``, edited paths) — a replay has no worktree.

So the replay measures the seed's *composition* (order, caps, the action directive, the raw tail) on the
material that both schemes share, and the report says so.
"""
from __future__ import annotations

from typing import Any

from ..ledger import checkpoint, digest, seed as _seed
from .ledger_seed import _card, _stage, reconstruct
from .seeds import CutContext, register

DIGEST_CHARS = 2600
DIGEST_MIN_CHARS = 240
TAIL_ROUNDS = 2
ROUND_CHARS = 2000


def rounds(turns: list[Any], count: int) -> str:
    """The worker's last ``count`` replies verbatim (``recent_rounds`` of the production seed)."""
    out: list[str] = []
    for turn in reversed(turns):
        if turn.kind == "handoff" or not turn.reply.strip():
            continue
        out.append(turn.reply.strip()[-ROUND_CHARS:])
        if len(out) >= count:
            break
    return "\n\n".join(reversed(out))


def build_ledger_full(ctx: CutContext) -> str:
    """Rules (empty in a replay), card, capped digest, narrative, raw tail, action directive."""
    before = ctx.before
    led = reconstruct(before)
    rows = led.rows()
    block = digest.render(rows, own=led.worker, max_chars=DIGEST_CHARS)
    missing = digest.ids_missing(block, rows, led.worker)
    if missing:
        block += "\nAlso open: " + ", ".join(missing)
    block_min = digest.render(rows, own=led.worker, max_chars=DIGEST_MIN_CHARS)
    tail = "\n\n".join(p for p in (rounds(before, TAIL_ROUNDS), _stage(before)) if p)
    return _seed.assemble(rules=[], card=_card(ctx.cut.seed), ledger=block, ledger_min=block_min,
                          narrative=ctx.cut.handoff, tail=tail, worktree="",
                          budget=checkpoint.SEED_BUDGET_DEFAULT)


register("ledger_full", build_ledger_full)
