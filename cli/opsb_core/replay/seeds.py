"""Seed builders for the compaction replay: what a new context would start from at a cut.

A builder takes a ``CutContext`` and returns the seed text. Registered schemes:

``current``             the platform's present seed: the logged ``Preserve this checkpoint``
                        prompt (handoff plus ``_checkpoint``) verbatim, or the same composition
                        rebuilt from the logged handoff and the last reply tail;
``current_plus_ledger`` ``current`` plus a ledger summary rebuilt from pre-cut platform events
                        (test digests, gate verdicts, commits, judge feedback, knowledge records);
``full_history``        every prompt, reply, thought, tool input and result before the cut (upper bound).

``ledger_core``         ``current``'s handoff, a ledger summary rebuilt with the platform's own ledger code from the
                        pre-cut events, and the stage-result tail kept (``replay/ledger_seed.py``, P05);

P06 adds its scheme with ``register("full_c", builder)``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from . import actions, probes
from .loader import SEED_PREFIX, AgentLog, Cut, Turn, before_turns

LEDGER_MAX_CHARS = 4000
CHECKPOINT_MAX_CHARS = 4000
STAGE_TAIL_CHARS = 1500
CHECKPOINT_TAIL = "Continue from the state card. Do not re-read files you already summarized."


@dataclass
class CutContext:
    log: AgentLog
    cut: Cut

    @property
    def before(self) -> list[Turn]:
        return before_turns(self.log, self.cut)


Builder = Callable[[CutContext], str]
_REGISTRY: dict[str, Builder] = {}


def register(name: str, builder: Builder) -> None:
    _REGISTRY[str(name)] = builder


def get(name: str) -> Builder:
    if name not in _REGISTRY:
        raise KeyError(f"unknown seed scheme {name!r}; known: {', '.join(names())}")
    return _REGISTRY[name]


def names() -> list[str]:
    return list(_REGISTRY)


def build_current(ctx: CutContext) -> str:
    if ctx.cut.seed:
        return ctx.cut.seed
    replies = [t.reply for t in ctx.before if t.reply.strip() and t.kind != "handoff"]
    stage = (replies[-1][-STAGE_TAIL_CHARS:] if replies else "").strip()
    room = max(0, CHECKPOINT_MAX_CHARS - len(CHECKPOINT_TAIL) - 1)
    checkpoint = "\n".join([stage[:room], CHECKPOINT_TAIL])[:CHECKPOINT_MAX_CHARS]
    return f"{SEED_PREFIX} and await the next task.\n{ctx.cut.handoff}\nLast reported result:\n{checkpoint}"


def _test_line(body: dict) -> str | None:
    dg = body.get("digest")
    if not isinstance(dg, dict):
        return None
    summ = dg.get("summary") or {}
    counts = ",".join(f"{k}={v}" for k, v in sorted((summ.get("counts") or {}).items()))
    base = dg.get("baseline") or {}
    speed = base.get("geomean_speedup")
    vs = f" geomean {speed:.3f}x vs {base.get('sha') or '?'}" if isinstance(speed, (int, float)) else ""
    return f"test {body.get('sha7')} {body.get('level')}: {summ.get('passed')}/{summ.get('total')} {counts}{vs}"


def ledger_lines(turns: list[Turn]) -> list[str]:
    """One line per platform fact before the cut, newest last, de-duplicated."""
    lines: list[str] = []
    commits = probes.commit_map(turns)
    for sha, msg in commits.items():
        lines.append(f"commit {sha}: {msg[:160]}")
    for t in turns:
        if probes.JUDGE_BAD.search(t.prompt) or "official" in t.prompt.lower():
            first = next((ln for ln in t.prompt.splitlines() if ln.strip()), "")
            lines.append(f"judge: {first[:200]}")
        for call in t.tools:
            body = actions.platform_result(call)
            if call.name in ("parhelion_test", "parhelion_test_wait"):
                line = _test_line(body)
                if line:
                    lines.append(line)
            elif call.name == "parhelion_submit_gate" and body.get("status_tag"):
                est = body.get("estimate") or {}
                lines.append(f"gate {body.get('short_sha')}: {body.get('status_tag')} expected "
                             f"{est.get('delta_expected')} ({str(body.get('gate_reason') or '')[:100]})")
            elif call.name == "parhelion_kb_record":
                lines.append(f"kb {call.args.get('verdict') or ''} {call.args.get('sha') or ''}: "
                             f"{str(call.args.get('title') or '')[:160]}")
            elif call.name == "parhelion_agent_report" and call.args.get("kind"):
                lines.append(f"report {call.args.get('kind')} {call.args.get('sha') or ''}: "
                             f"{str(call.args.get('summary') or '')[:200]}")
    seen: set[str] = set()
    out = []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return out


def build_current_plus_ledger(ctx: CutContext) -> str:
    lines = ledger_lines(ctx.before)
    kept: list[str] = []
    size = 0
    for ln in reversed(lines):  # keep the newest facts within the budget
        if size + len(ln) + 1 > LEDGER_MAX_CHARS:
            break
        kept.append(ln)
        size += len(ln) + 1
    omitted = len(lines) - len(kept)
    head = f"Ledger ({len(kept)} entries" + (f", {omitted} older omitted" if omitted else "") + "):"
    return build_current(ctx) + "\n" + "\n".join([head] + list(reversed(kept)))


def build_full_history(ctx: CutContext) -> str:
    parts: list[str] = []
    for t in ctx.before:
        parts.extend([t.prompt, t.thought, t.reply])
        for call in t.tools:
            parts.append(f"{call.name} {json.dumps(call.args, ensure_ascii=False, default=str)}")
            parts.append(call.result)
    return "\n".join(p for p in parts if p)


register("current", build_current)
register("current_plus_ledger", build_current_plus_ledger)
register("full_history", build_full_history)

from . import ledger_seed  # noqa: E402,F401  registers ``ledger_core`` (P05); it imports this module's names
