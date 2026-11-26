"""JSON Schema definitions for the role-scoped Parhelion MCP tools (`parhelion_*`).

The whole table rides on every model call, so a description here is context, not documentation: one
sentence of what the tool does plus the constraint a caller must not get wrong. A parameter carries a
note only when its name, type and enum do not already say it. Column meanings of the report views live in
the tool (``parhelion_report(view="help")``), not in the schema. ``cli/tests/test_mcp_server.py`` pins the
serialized size of both role tables: a new tool or a longer description has to pay for itself there.
"""
from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "parhelion_status",
        "description": "Contest scores, quota and per-problem rows (brief). Pass an earlier reply's rev as if_rev for "
                       "{not_modified:true}; to wait for a change use parhelion_wait, not a loop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "if_rev": {"type": "string"},
                "include_npu": {"type": "boolean"},
                "include_queue": {"type": "boolean"},
                "format": {"type": "string", "enum": ["brief", "full"]},
                "verbose": {"type": "boolean"},
                "detail": {"type": "boolean"},
            },
        },
    },
    {
        "name": "parhelion_report",
        "description": "Read one report view instead of ad-hoc scripts over report.json or submit-queue-outcome.jsonl. Views: per_case, "
                       "diff, history, queue, board, bottleneck, env, help (what each column means). Data channel: board, tbest, receipts "
                       "and environment facts come from these views, never from ~/.parhelion or ~/.judge files or from platform internals.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["per_case", "diff", "history", "queue", "board", "bottleneck", "env", "help"]},
                "problem": {"type": "string", "description": "a worker may name only its own problem"},
                "sha": {"type": "string"},
                "base": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "cursor": {"type": "string"},
            },
            "required": ["view"],
        },
    },
    {
        "name": "parhelion_spill_read",
        "roles": ("orchestrator", "worker"),
        "description": "Read one window of a spilled reply from the returned path and next_offset; a worker reads only its own spill files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "parhelion_bench_diagnose",
        "description": "Diagnose +corr headroom strata, residual gaps, calibration trust & ESCAPE_LOCK state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "verbose": {"type": "boolean"},
            },
        },
    },
    {
        "name": "parhelion_pmu_card",
        "description": "PMU pipe bottleneck card: ours/torch ratio per bucket.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "top_n": {"type": "integer"},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_elf_cold3_check",
        "description": "Check ELF .o/.text/template instantiation redlines and 3-run cold-start DVFS spikes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_kb_query",
        "description": "Query the 5-pillar KB by symptom, regime, dtype or text: brief (default) summaries, format=full entries, paged by "
                       "cursor or offset/limit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bottlenecks": {"type": "array", "items": {"type": "string"}},
                "regime": {"type": "string"},
                "dtype": {"type": "string"},
                "pillar": {"type": "string", "enum": ["breakthrough", "snippets", "hardware", "community", "bugs", "all"]},
                "text": {"type": "string"},
                "include_anti_patterns": {"type": "boolean"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer", "minimum": 0},
                "cursor": {"type": "string"},
                "digest": {"type": "boolean"},
                "format": {"type": "string", "enum": ["brief", "full"]},
                "full": {"type": "boolean"},
            },
        },
    },
    {
        "name": "parhelion_kb_record",
        "description": "Propose a snippet, hardware law, community card or anti-pattern (evidence-gated).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pillar": {"type": "string", "enum": ["snippets", "hardware", "community"]},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "verdict": {"type": "string", "enum": ["win", "fail", "anti-pattern"]},
                "bottlenecks": {"type": "array", "items": {"type": "string"}},
                "regimes": {"type": "array", "items": {"type": "string"}},
                "dtypes": {"type": "array", "items": {"type": "string"}},
                "portability": {"type": "string", "enum": ["native-accel", "portable", "remap-to-accel", "anti-pattern"]},
                "code": {"type": "string"},
                "doc_markdown": {"type": "string"},
                "delta_raw": {"type": "number"},
                "delta_us": {"type": "number"},
                "note": {"type": "string"},
            },
            "required": ["pillar", "title", "summary", "problem", "sha", "verdict"],
        },
    },
    {
        "name": "parhelion_kb_feedback",
        "description": "Record win/fail/anti-pattern evidence on a KB entry; drives promotion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["win", "fail", "anti-pattern"]},
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "delta_raw": {"type": "number"},
                "delta_us": {"type": "number"},
                "note": {"type": "string"},
                "promote_snippet_code": {"type": "string"},
            },
            "required": ["entry_id", "verdict", "problem", "sha"],
        },
    },
    {
        "name": "parhelion_test",
        "description": "Submit one platform test run (request_id within 2s) and follow it with parhelion_test_wait; the result also "
                       "reaches your inbox. The level decides how much device time the run costs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "format": {"type": "string", "enum": ["brief", "full"]},
                "sha": {"type": "string"},
                "level": {"type": "string", "enum": ["smoke", "standard", "final"], "description": "smoke: quick check; standard (default): all cases plus warm timing; final: candidate validation"},
                "cases": {"type": "array", "items": {"type": "string"}},
                "remeasure": {"type": "boolean"},
                "force_rerun": {"type": "boolean"},
                "reason": {"type": "string"},
                "build_only": {"type": "boolean"},
                "compare": {"type": "string"},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_test_wait",
        "description": "Wait up to wait_sec (default 30, max 45) for a test request and return progress or the digest; parhelion_wait covers several at once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "wait_sec": {"type": "number", "minimum": 1, "maximum": 45},
                "format": {"type": "string", "enum": ["brief", "full"]},
            },
            "required": ["request_id"],
        },
    },
    {
        "name": "parhelion_test_cancel",
        "description": "Cancel a running test request (a worker may cancel only its own).",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
        },
    },
    {
        "name": "parhelion_test_plan",
        "description": "Deprecated: preview the test command, or execute=true to submit and wait 40s. Prefer parhelion_test.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "mode": {"type": "string", "enum": ["build-only", "fast", "full"]},
                "level": {"type": "string", "enum": ["smoke", "standard", "final"]},
                "execute": {"type": "boolean"},
                "cases": {"type": "array", "items": {"type": "string"}},
                "bucket": {"type": "string"},
                "experiment": {"type": "string"},
                "profile": {"type": "string"},
                "timing": {"type": "string", "enum": ["off", "perf", "all"]},
                "repeats": {"type": "integer", "minimum": 1},
                "remeasure": {"type": "boolean"},
                "suite": {"type": "string", "enum": ["adaptive", "all"]},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_submit_gate",
        "description": "Evaluate the 4-stage submission gate: a call is a preview, enqueue=true queues the SHA when it passes. Give "
                       "hypothesis_id and predicted_effect, or the platform derives one from the commit message.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "note": {"type": "string"},
                "hypothesis_id": {"type": "string"},
                "predicted_effect": {"type": "object"},
                "enqueue": {"type": "boolean"},
                "force": {"type": "boolean"},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_escape_status",
        "description": "Escape locks: per WA/RE receipt the reproducer state, the reviewed-trial binding and the next step; a worker sees "
                       "its own problem only.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {"problem": {"type": "string"}},
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_escape_rebind",
        "description": "Bind the active escape lock's one reviewed trial to another repair SHA, only while the bound repair was neither "
                       "submitted nor queued.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "receipt": {"type": "string"},
                "sha": {"type": "string"},
                "review": {"type": "string"},
            },
            "required": ["problem", "receipt", "sha"],
        },
    },
    {
        "name": "parhelion_escape_reproducer",
        "description": "The reproducer variants for a WA/RE receipt and the parhelion_test calls that confirm and verify a repair.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "receipt": {"type": "string"},
            },
            "required": ["problem", "receipt"],
        },
    },
    {
        "name": "parhelion_note",
        "description": "Write one ledger entry and get its id (L-nnnn); fields are checked per type. Platform fields are rejected; a status "
                       "change is a new row that needs supersedes or id.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["hypothesis", "experiment", "insight", "failure", "decision", "constraint"]},
                "claim": {"type": "string", "description": "one assertive sentence, at most 240 characters"},
                "why": {"type": "string"},
                "scope": {"type": "object"},
                "sha": {"type": "string"},
                "base_sha": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "enum": ["inferred", "speculative"]},
                "links": {"type": "array", "items": {"type": "string"}},
                "supersedes": {"type": "array", "items": {"type": "string"}},
                "id": {"type": "string"},
                "revisit_if": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "queued", "testing", "confirmed", "refuted", "inconclusive", "abandoned"]},
                "predicted_effect": {"type": "object"},
                "test_plan": {"type": "string"},
                "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                "actionable_next": {"type": "string"},
                "est_gain": {"type": "object"},
                "cases": {"type": "array", "items": {"type": "string"}},
                "approach": {"type": "string"},
                "why_failed": {"type": "string"},
                "cost_spent": {"type": "string"},
                "retry_conditions": {"type": "string"},
                "chosen": {"type": "string"},
                "rejected": {"type": "string"},
                "reason": {"type": "string"},
                "text": {"type": "string"},
                "rule_source": {"type": "string"},
                "lesson": {"type": "string"},
                "problem": {"type": "string", "description": "orchestrator only; a worker's problem is bound"},
                "worker": {"type": "string"},
            },
            "required": ["type"],
        },
    },
    {
        "name": "parhelion_ledger",
        "description": "One ledger view, one line per entry: open hypotheses by priority, unexploited insights, failures, one case, or since an id.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "description": "open | unexploited | failures | case:<id> | since:<id>"},
                "problem": {"type": "string"},
                "worker": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                "offset": {"type": "integer", "minimum": 0},
                "cursor": {"type": "string"},
            },
        },
    },
    {
        "name": "parhelion_recall",
        "description": "Search the ledger, compaction handoffs and this worker's session log; results carry a file#line source.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                "problem": {"type": "string"},
                "worker": {"type": "string"},
                "refresh": {"type": "boolean"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "parhelion_harness_pool",
        "description": "Primary orchestrator, external Harnesses, squads and strategic alerts.",
        "roles": ("orchestrator",),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "parhelion_sprint_start",
        "description": "On a contest start, launch or reuse one worker per problem on the active Harness; preview checks readiness without launching.",
        "roles": ("orchestrator",),
        "inputSchema": {"type": "object", "properties": {
            "driver": {"type": "string"}, "preview": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["solo", "dual", "full"], "description": "solo (default): 1 per problem; dual adds algorithm/dataflow; full adds bench-author"},
            "problems": {"type": "array", "items": {"type": "string"}},
            "enable_auto_submit": {"type": "boolean", "description": "only when the user asks for automatic submission"}
        }},
    },
    {
        "name": "parhelion_agent_spawn",
        "description": "Spawn a non-blocking autonomous external Harness worker (blocks active_driver self-dispatch; no concurrency cap).",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "driver": {"type": "string", "enum": ["antigravity", "codex", "claude", "deepseek", "pi", "gemini"]},
                "task": {"type": "string"},
                "role_label": {"type": "string"},
                "problem": {"type": "string"},
                "collab_mode": {"type": "string", "enum": ["orthogonal", "pipeline", "solo"]},
                "axis": {"type": "string"},
                "worktree_mode": {"type": "string", "enum": ["isolated", "readonly", "shared"]},
                "track": {"type": "string"},
            },
            "required": ["driver", "task"],
        },
    },
    {
        "name": "parhelion_agent_send",
        "description": "Send a strategic adjustment or mid-turn interrupt to a worker.",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "message": {"type": "string"},
                "delivery": {"type": "string", "enum": ["queue", "interrupt"]},
            },
            "required": ["agent_id", "message"],
        },
    },
    {
        "name": "parhelion_agent_wait",
        "description": "Block (<=45s) until strategic events or worker results arrive. Prefer parhelion_wait, which also covers tests, seats and receipts.",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_seq": {"type": "integer"},
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "problem": {"type": "string"},
                "strategic_only": {"type": "boolean"},
                "timeout_sec": {"type": "number"},
            },
        },
    },
    {
        "name": "parhelion_wait",
        "description": "Block until something you watch changes and return only the changes since `since`: inbox, test runs, seats, judge receipts. "
                       "Prefer this over looping parhelion_status, agent_wait or test_wait; omit timeout and the wait lengthens itself (max 100s).",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "rev from the previous wait; omit to watch from now"},
                "until": {"type": "array", "items": {"type": "string", "enum": ["inbox", "test", "agent", "judge"]}},
                "timeout": {"type": "number", "minimum": 0, "maximum": 100},
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "problem": {"type": "string"},
                "strategic_only": {"type": "boolean"},
            },
        },
    },
    {
        "name": "parhelion_agent_peek",
        "description": "Orchestrator only: look inside one worker without shelling out; use it instead of cat/tail/ls/git diff --stat on its "
                       "worktree. what: files (two levels), file (one path **relative to that worktree**; absolute and .. refused; optional lines "
                       "range), log_tail (last lines, credentials redacted), diff_stat. Answers are capped at 8 KB.",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "what": {"type": "string", "enum": ["files", "file", "log_tail", "diff_stat"]},
                "path": {"type": "string"},
                "lines": {"type": "string"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "parhelion_agent_messages",
        "description": "Message history (per worker or Problem Squad), worktree git diff --stat, and commit.",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "problem": {"type": "string"},
                "since_seq": {"type": "integer"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer", "minimum": 0},
                "cursor": {"type": "string"},
                "ack": {"type": "boolean"},
            },
        },
    },
    {
        "name": "parhelion_agent_stop",
        "description": "Terminate a worker process group (-pgid), optionally removing its worktree.",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "cleanup_worktree": {"type": "boolean"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "parhelion_agent_report",
        "description": "Report progress or a question; kind=result with the SHA, evidence and next idea is the compaction checkpoint.",
        "roles": ("worker",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["progress", "breakthrough", "stagnation", "escape_zero", "question", "result", "error"]},
                "summary": {"type": "string"},
                "no_gain_streak": {"type": "integer"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "parhelion_squad",
        "roles": ("worker",),
        "description": "List teammates, or send one claim-first peer envelope: recipient/to, refs, claim (<=280 chars), evidence "
                       "(path#anchor, run id or receipt id), ask; over 1200 chars becomes an attachment carrying only the claim and path.",
        "inputSchema": {"type": "object", "properties": {
            "recipient": {"type": "string"},
            "to": {"type": "string"},
            "claim": {"type": "string"},
            "refs": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "string"},
            "ask": {"type": "string"},
            "message": {"type": "string"},
            "request_id": {"type": "string"},
        }},
    },
    {
        "name": "parhelion_agent_inbox",
        "description": "Worker-only: pending orchestrator follow-ups and broadcasts; ack what you read.",
        "roles": ("worker",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_seq": {"type": "integer"},
                "ack_seq": {"type": "integer"},
            },
        },
    },
]
