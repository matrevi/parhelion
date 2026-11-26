"""JSON Schema definitions for the role-scoped Parhelion MCP tools (`parhelion_*`)."""
from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "parhelion_status",
        "description": "Compact contest scores, quota and per-problem rows (under 2KB). verbose=true returns the full structure. Pass the last rev as if_rev to get {not_modified:true} when nothing changed; to wait for changes use parhelion_wait instead of calling status in a loop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Optional problem title filter"},
                "if_rev": {"type": "string", "description": "rev from a previous status reply with the same arguments"},
                "include_npu": {"type": "boolean", "description": "Probe live device telemetry via SSH (default false)"},
                "include_queue": {"type": "boolean", "description": "Include up to 5 queue items (default false)"},
                "verbose": {"type": "boolean", "description": "Return the full uncompacted structure (default false)"},
                "detail": {"type": "boolean", "description": "Include the extended state card: the per-case speedup, gap to tbest, targets and last receipts (default false)"},
            },
        },
    },
    {
        "name": "parhelion_bench_diagnose",
        "description": "Diagnose +corr headroom strata, residual gaps, calibration trust & ESCAPE_LOCK state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Optional operator problem title"},
                "sha": {"type": "string", "description": "Optional commit SHA"},
                "verbose": {"type": "boolean", "description": "Include the coverage grid (default false)"},
            },
        },
    },
    {
        "name": "parhelion_pmu_card",
        "description": "Generate compact ours/torch ratio and PMU pipe bottleneck diagnostic card.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Operator problem title"},
                "sha": {"type": "string", "description": "Optional commit SHA"},
                "top_n": {"type": "integer", "description": "Top worst buckets to surface (default 5)"},
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
                "problem": {"type": "string", "description": "Operator problem title"},
                "sha": {"type": "string", "description": "Optional commit SHA"},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_kb_query",
        "description": "Query the 5-pillar KB by symptom, regime, dtype or text. Returns top 3 compact entries (600-char summaries); full=true returns complete entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bottlenecks": {"type": "array", "items": {"type": "string"}},
                "regime": {"type": "string"},
                "dtype": {"type": "string"},
                "pillar": {"type": "string", "enum": ["breakthrough", "snippets", "hardware", "community", "bugs", "all"]},
                "text": {"type": "string"},
                "include_anti_patterns": {"type": "boolean"},
                "limit": {"type": "integer", "description": "Maximum entries (default 3)"},
                "digest": {"type": "boolean"},
                "full": {"type": "boolean", "description": "Return complete entries instead of compact summaries"},
            },
        },
    },
    {
        "name": "parhelion_kb_record",
        "description": "Propose a new snippet, hardware law (silicon/doc-discovered Accel behavior), community acceleration card, or anti-pattern (strictly evidence-gated).",
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
        "description": "Record win/fail/anti-pattern evidence on an existing KB entry to drive Bayesian promotion/demotion.",
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
        "description": "Submit a platform test run and get a request_id within 2s. Then call parhelion_test_wait; the result also arrives in your inbox when it ends.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string", "description": "Commit to test (default HEAD)"},
                "level": {"type": "string", "enum": ["smoke", "standard", "final"], "description": "smoke: quick self-check; standard (default): all correctness cases plus warm timing; final: candidate validation"},
                "cases": {"type": "array", "items": {"type": "string"}, "description": "Optional case IDs to restrict the run"},
                "remeasure": {"type": "boolean", "description": "Measure again instead of reusing a recent passing result"},
                "force_rerun": {"type": "boolean", "description": "Run again even though this SHA and level already have a formal run; requires reason"},
                "reason": {"type": "string", "description": "Why a forced rerun or a near-duplicate candidate is worth the device time"},
                "build_only": {"type": "boolean", "description": "Compile only, no device time"},
                "compare": {"type": "string", "description": "A/B paired run against this baseline commit, or 'board': both builds alternate in one device job and the result reports per-case speedup intervals"},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_test_wait",
        "description": "Wait up to wait_sec (default 30, max 45) for a parhelion_test request; returns progress or the compact result digest. To wait for several runs or for messages at once, use parhelion_wait.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "wait_sec": {"type": "number", "minimum": 1, "maximum": 45},
            },
            "required": ["request_id"],
        },
    },
    {
        "name": "parhelion_test_cancel",
        "description": "Cancel a running parhelion_test request (workers may cancel only their own).",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
        },
    },
    {
        "name": "parhelion_test_plan",
        "description": "Deprecated: preview the test command, or execute=true to submit and wait 40s. Prefer parhelion_test + parhelion_test_wait.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "mode": {"type": "string", "enum": ["build-only", "fast", "full"], "description": "build-only, fast (smoke) or full (standard)"},
                "level": {"type": "string", "enum": ["smoke", "standard", "final"]},
                "execute": {"type": "boolean"},
                "cases": {"type": "array", "items": {"type": "string"}},
                "bucket": {"type": "string"},
                "experiment": {"type": "string"},
                "profile": {"type": "string", "description": "Path to a shared experimental case profile in this contest workspace."},
                "timing": {"type": "string", "enum": ["off", "perf", "all"]},
                "repeats": {"type": "integer", "minimum": 1},
                "remeasure": {"type": "boolean", "description": "Request a fresh measurement for a reference suite even when matching successful evidence is recent."},
                "suite": {"type": "string", "enum": ["adaptive", "all"], "description": "Default adaptive reference probe. Supply cases or bucket to target an experiment. Use all for formal candidate validation."},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_submit_gate",
        "description": "Evaluate 4-Stage Submission Gate (G1-G4 + ESCAPE_LOCK) and optionally enqueue validated commit. Name the hypothesis this candidate tests (hypothesis_id) and its predicted_effect; without them the platform generates the hypothesis from the commit message and says which id it created.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "note": {"type": "string"},
                "hypothesis_id": {"type": "string", "description": "Ledger id (L-nnnn) of the hypothesis this candidate tests"},
                "predicted_effect": {"type": "object", "description": "Expected effect, e.g. {delta_mu: 0.8, delta_sigma: 0.4} or {case05: '-8..-12%'}"},
                "enqueue": {"type": "boolean"},
                "force": {"type": "boolean"},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_escape_status",
        "description": "Escape locks of a problem: per WA/RE receipt the reproducer state (pending, reproduced, fixed), "
                       "the reviewed-trial binding and whether it was used, and the next step. Workers: own problem only.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {"problem": {"type": "string", "description": "Operator problem title"}},
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_escape_rebind",
        "description": "Bind the active escape lock's one reviewed trial to another repair SHA; allowed only while the "
                       "bound repair was neither submitted nor queued. Refusals return the reason and change nothing.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Operator problem title"},
                "receipt": {"type": "string", "description": "Submission ID of the active escape lock"},
                "sha": {"type": "string", "description": "Repair commit with a formal run of the current suite"},
                "review": {"type": "string", "description": "Why this repair addresses the online failure"},
            },
            "required": ["problem", "receipt", "sha"],
        },
    },
    {
        "name": "parhelion_escape_reproducer",
        "description": "The local reproducer variants generated for a WA/RE receipt and the exact parhelion_test calls "
                       "that confirm the reproduction on the bad SHA and verify a repair.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Operator problem title"},
                "receipt": {"type": "string", "description": "Submission ID of the WA/RE receipt"},
            },
            "required": ["problem", "receipt"],
        },
    },
    {
        "name": "parhelion_note",
        "description": "Write one experiment-ledger entry and get its id back (L-nnnn). Types: hypothesis, experiment, insight, failure, decision, constraint. Platform fields (outcome, per-case times, measured confidence) are rejected; a status change is a new row and needs supersedes or id.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["hypothesis", "experiment", "insight", "failure", "decision", "constraint"]},
                "claim": {"type": "string", "description": "One assertive sentence, at most 240 characters"},
                "why": {"type": "string", "description": "Mechanism or reasoning, at most 300 characters"},
                "scope": {"type": "object", "description": "Where the claim applies: {cases, bucket, shape, dtype}"},
                "sha": {"type": "string"},
                "base_sha": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "enum": ["inferred", "speculative"], "description": "A model cannot claim measured"},
                "links": {"type": "array", "items": {"type": "string"}},
                "supersedes": {"type": "array", "items": {"type": "string"}, "description": "Ids this row replaces (a status update)"},
                "id": {"type": "string", "description": "Entry to update; read as supersedes"},
                "revisit_if": {"type": "string", "description": "Conditions under which a closed item should be re-opened"},
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
                "problem": {"type": "string", "description": "Orchestrator only; a worker's problem is bound"},
                "worker": {"type": "string", "description": "Orchestrator only: write into this worker's ledger"},
            },
            "required": ["type"],
        },
    },
    {
        "name": "parhelion_ledger",
        "description": "Read one ledger view, one line per entry: open hypotheses by priority, unexploited insights, failures, one case, or everything since an id. Lines carry the id, type, claim and the number that matters.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "description": "open | unexploited | failures | case:<id> | since:<id>"},
                "problem": {"type": "string", "description": "Orchestrator only; a worker's problem is bound"},
                "worker": {"type": "string", "description": "Orchestrator only: read this worker's ledger"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
            },
        },
    },
    {
        "name": "parhelion_recall",
        "description": "Search the ledger, the compaction handoffs and this worker's archived session log for what was already known. Results are snippets with their source as file#line.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                "problem": {"type": "string", "description": "Orchestrator only; a worker's problem is bound"},
                "worker": {"type": "string", "description": "Orchestrator only: search this worker's records"},
                "refresh": {"type": "boolean", "description": "Rebuild the index instead of using the cached one"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "parhelion_harness_pool",
        "description": "Inspect primary orchestrator (self-dispatch disabled), eligible external Harnesses, problem squads, and strategic alerts.",
        "roles": ("orchestrator",),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "parhelion_sprint_start",
        "description": "On a contest start request, launch or reuse workers per problem (one per problem by default). Defaults to the active Harness. A preview checks readiness without launching. Inspect blocked slots and worker messages.",
        "roles": ("orchestrator",),
        "inputSchema": {"type": "object", "properties": {
            "driver": {"type": "string"}, "preview": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["solo", "dual", "full"], "description": "solo (default): 1 worker per problem; dual adds algorithm/dataflow tracks; full also adds bench-author"},
            "problems": {"type": "array", "items": {"type": "string"}, "description": "Optional problem titles to staff; omit for every problem in the contest"},
            "enable_auto_submit": {"type": "boolean", "description": "Enable the existing queue consumer for the active contest when the user requests automatic submission. Defaults to preserving the switch."}
        }},
    },
    {
        "name": "parhelion_agent_spawn",
        "description": "Spawn a non-blocking autonomous external Harness worker (blocks active_driver self-dispatch; no concurrency cap; full bench+gate autonomy).",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "driver": {"type": "string", "enum": ["antigravity", "codex", "claude", "deepseek", "pi", "gemini"]},
                "task": {"type": "string", "description": "Initial task instruction for the external worker"},
                "role_label": {"type": "string", "description": "Short role badge (e.g. impl, audit, recon)"},
                "problem": {"type": "string", "description": "Optional operator problem title (groups into Problem Squad)"},
                "collab_mode": {"type": "string", "enum": ["orthogonal", "pipeline", "solo"], "description": "Squad concurrency philosophy"},
                "axis": {"type": "string", "description": "Orthogonal exploration axis or pipeline stage tag (e.g. L1I-pack, ring4-ub, audit)"},
                "worktree_mode": {"type": "string", "enum": ["isolated", "readonly", "shared"]},
                "track": {"type": "string", "description": "Optional git worktree branch/track name"},
            },
            "required": ["driver", "task"],
        },
    },
    {
        "name": "parhelion_agent_send",
        "description": "Send a strategic adjustment, generalization hint, or mid-turn interrupt to an external Harness worker.",
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
        "description": "Block (<=45s cap) until strategic events (breakthrough/stagnation/escape_zero/question/directive) or worker results arrive. Prefer parhelion_wait, which also covers tests, seats and receipts and returns only changes.",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_seq": {"type": "integer"},
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "problem": {"type": "string", "description": "Optional Problem Squad filter"},
                "strategic_only": {"type": "boolean", "description": "Wake on strategic events (default true). Use false to include ordinary replies."},
                "timeout_sec": {"type": "number", "description": "Wait timeout in seconds (default 25, max 45)"},
            },
        },
    },
    {
        "name": "parhelion_wait",
        "description": "Block until something you watch changes, then return only the changes since `since` as one-line summaries: inbox messages, test runs, seat status, judge receipts (a worker sees its own; the orchestrator sees all). Pass the returned rev as `since` next time. With no change it returns {changed:false, waiting_for, idle_streak, next_timeout}; omit timeout so the wait lengthens itself (max 100s). Use this instead of polling status, agent_wait or test_wait; read full text with agent_messages, agent_inbox or test_wait.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "rev from the previous parhelion_wait; omit to watch from now"},
                "until": {"type": "array", "items": {"type": "string", "enum": ["inbox", "test", "agent", "judge"]},
                          "description": "Sources to wake on (default all four)"},
                "timeout": {"type": "number", "minimum": 0, "maximum": 100, "description": "Seconds to block; default grows while nothing changes"},
                "agent_ids": {"type": "array", "items": {"type": "string"}, "description": "Orchestrator only: limit to these workers"},
                "problem": {"type": "string", "description": "Orchestrator only: limit to one problem"},
                "strategic_only": {"type": "boolean", "description": "Orchestrator only: wake on strategic messages only"},
            },
        },
    },
    {
        "name": "parhelion_agent_messages",
        "description": "Read message history (per-worker or aggregated by Problem Squad), worktree git diff --stat, and commit SHA.",
        "roles": ("orchestrator",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "problem": {"type": "string", "description": "Optional Problem Squad filter"},
                "since_seq": {"type": "integer"},
                "limit": {"type": "integer"},
                "ack": {"type": "boolean"},
            },
        },
    },
    {
        "name": "parhelion_agent_stop",
        "description": "Terminate an external Harness worker process group (-pgid) and optionally remove its isolated worktree.",
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
        "description": "Report progress or a question. At a completed stage use kind=result with the SHA, evidence and next idea; this is a checkpoint for context compaction.",
        "roles": ("worker",),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["progress", "breakthrough", "stagnation", "escape_zero", "question", "result", "error"]},
                "summary": {"type": "string"},
                "no_gain_streak": {"type": "integer", "description": "Optional count of consecutive attempts without positive gain (>=6 escalates)"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "parhelion_squad",
        "roles": ("worker",),
        "description": "List teammates for your problem, or send them a claim-first peer envelope: recipient/to, "
                       "refs (ledger ids), claim (<= 280 chars), evidence (path#anchor, test run id or receipt id), "
                       "ask?. A body above 1200 chars becomes an attachment at <workspace state>/peer/<msg_id>.md and "
                       "only its claim and path travel. Peer messages reach a teammate merged at its next turn "
                       "boundary; no arguments lists teammates.",
        "inputSchema": {"type": "object", "properties": {
            "recipient": {"type": "string", "description": "agent_id, teammate role name (e.g. algorithm, dataflow, bench-author) or all; omit all content arguments to list teammates"},
            "to": {"type": "string", "description": "Envelope alias of recipient; recipient wins when both are given."},
            "claim": {"type": "string", "description": "What the teammate should know, at most 280 characters, without the supporting text."},
            "refs": {"type": "array", "items": {"type": "string"},
                     "description": "Ledger ids this message cites (L-0001 or worker/L-0001); the recipient gets the ids, not a re-quote."},
            "evidence": {"type": "string", "description": "One reference the recipient can open: a path#anchor, a test run id or a receipt id."},
            "ask": {"type": "string", "description": "Optional explicit question or next step you want from the recipient."},
            "message": {"type": "string", "description": "Legacy free text: up to 280 characters becomes the claim; a longer body is delivered inline up to 1200 characters and stored as an attachment above that."},
            "request_id": {"type": "string", "description": "Reuse on retries to deliver only once."},
        }},
    },
    {
        "name": "parhelion_agent_inbox",
        "description": "Worker-only: read and optionally acknowledge pending orchestrator follow-up messages or cross-problem broadcasts.",
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
