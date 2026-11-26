"""The ``ledger_core`` seed (4.2 C, P05): the current handoff plus a ledger summary plus the kept stage tail.

At a cut the platform would have written a ledger from the events before it. This module rebuilds
that ledger in memory with the platform's own ``ledger.auto.apply_*`` functions (so replay and
production share one code path) from the logged tool results and judge prompts:

- a ``parhelion_test`` / ``parhelion_test_wait`` result that settled -> measurement + experiment;
- a ``parhelion_submit_gate`` call with ``enqueue`` -> a platform hypothesis (note, else the logged commit message);
- a judge prompt ``<title> <sha7> · <status> · score <s> · submission <id>`` -> measurement + the verdict on
  the SHA's hypothesis, with the gain against the best earlier score in the log.

Only platform events enter. Insights, files and conclusions a worker would write through ``parhelion_note``
(P06) are not reconstructed, so this scheme measures what the platform's own records recover.
The narrative is the logged one (the 4.2 prompt is not replayable), and the checkpoint applies the tail-keeping
budget of ``ledger.checkpoint``.
"""
from __future__ import annotations

import re
from typing import Any

from ..ledger import auto, checkpoint, gitref, store, summary
from . import actions, probes
from .loader import SEED_PREFIX, ToolCall, Turn
from .seeds import CHECKPOINT_TAIL, CutContext, register

RECEIPT_LINE = re.compile(r"^(?P<title>\S+) (?P<sha>[0-9a-f]{7,40}) · (?P<status>[A-Za-z ]+?) · score (?P<score>\S+) · "
                          r"submission (?P<sid>\S+)", re.M)
STAGE_KINDS = ("result", "breakthrough", "stagnation", "escape_zero")
SETTLED = ("finished", "failed")


def _test_record(call: ToolCall) -> dict[str, Any] | None:
    body = actions.platform_result(call)
    if call.name not in ("parhelion_test", "parhelion_test_wait") or body.get("status") not in SETTLED:
        return None
    sha = str(body.get("sha7") or call.args.get("sha") or "")
    if not body.get("request_id") or not gitref.valid_sha(sha):
        return None
    return {"request_id": body["request_id"], "problem": body.get("problem") or "replay", "sha": sha,
            "level": body.get("level"), "status": body["status"], "digest": body.get("digest"),
            "error": body.get("error")}


def _receipts(turn: Turn) -> list[dict[str, Any]]:
    out = []
    for m in RECEIPT_LINE.finditer(turn.prompt):
        status, score = m.group("status").strip(), m.group("score")
        try:
            value = float(score) if status in ("Pass", "Accepted") else 0.0
        except ValueError:
            continue  # an unscored Pass: the platform waits for the score as well
        out.append({"submission_id": m.group("sid"), "sha": m.group("sha"), "status": status,
                    "official_score": value, "title": m.group("title")})
    return out


def reconstruct(turns: list[Turn]) -> store.MemoryLedger:
    """The ledger the platform would hold after the given turns."""
    led = store.MemoryLedger()
    commits = probes.commit_map(turns)
    best = 0.0
    for turn in turns:
        led.clock = lambda ts=turn.ts: ts
        for call in turn.tools:
            record = _test_record(call)
            if record:
                led.clock = lambda ts=call.ts: ts
                auto.apply_test(led, record, subject=commits.get(record["sha"][:7], ""))
            elif call.name == "parhelion_submit_gate" and call.args.get("enqueue"):
                body = actions.platform_result(call)
                sha = str(body.get("sha") or call.args.get("sha") or "")
                if gitref.valid_sha(sha):
                    led.clock = lambda ts=call.ts: ts
                    auto.apply_gate(led, sha, note=str(call.args.get("note") or ""),
                                    subject=commits.get(sha[:7], ""), estimate=body.get("estimate") or {},
                                    enqueued=bool(body.get("enqueued")))
        led.clock = lambda ts=turn.ts: ts
        for receipt in _receipts(turn):
            score = receipt["official_score"]
            auto.apply_receipt(led, receipt, gain=round(score - best, 2), prior=best)
            best = max(best, score)
    return led


def _card(seed: str) -> str:
    """The STATE block of a logged checkpoint: from its first ``STATE`` line to the closing line."""
    lines = seed.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("STATE ")), None)
    if start is None:
        return ""
    end = next((i for i in range(start, len(lines)) if lines[i].strip() == CHECKPOINT_TAIL), len(lines))
    return "\n".join(lines[start:end])


def _stage(turns: list[Turn]) -> str:
    """What ``_stage_result`` held at the cut: the last stage report of the worker, else its last reply."""
    for turn in reversed(turns):
        for call in reversed(turn.tools):
            if call.name == "parhelion_agent_report" and str(call.args.get("kind") or "") in STAGE_KINDS:
                return str(call.args.get("summary") or "").strip()[-4000:]
    replies = [t.reply for t in turns if t.reply.strip() and t.kind != "handoff"]
    return (replies[-1][-checkpoint.STAGE_FALLBACK_CHARS:] if replies else "").strip()


def build_ledger_core(ctx: CutContext) -> str:
    before = ctx.before
    led = reconstruct(before)
    block = summary.render(led.rows(), own=led.worker)
    keep = checkpoint.build(_card(ctx.cut.seed), _stage(before), checkpoint.SEED_BUDGET_DEFAULT)
    parts = [f"{SEED_PREFIX} and await the next task.", ctx.cut.handoff, block, "Last reported result:\n" + keep]
    return "\n".join(p for p in parts if p)


register("ledger_core", build_ledger_core)
