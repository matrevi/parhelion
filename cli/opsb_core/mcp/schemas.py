"""JSON Schema definitions for the role-scoped Parhelion MCP tools (`parhelion_*`)."""
from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "parhelion_status",
        "description": "Compact contest scores, quota and per-problem rows (under 2KB). verbose=true returns the full structure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Optional problem title filter"},
                "include_npu": {"type": "boolean", "description": "Probe live device telemetry via SSH (default false)"},
                "include_queue": {"type": "boolean", "description": "Include up to 5 queue items (default false)"},
                "verbose": {"type": "boolean", "description": "Return the full uncompacted structure (default false)"},
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
                "build_only": {"type": "boolean", "description": "Compile only, no device time"},
                "compare": {"type": "string", "description": "A/B paired run against this baseline commit, or 'board': both builds alternate in one device job and the result reports per-case speedup intervals"},
            },
            "required": ["problem"],
        },
    },
    {
        "name": "parhelion_test_wait",
        "description": "Wait up to wait_sec (default 30, max 45) for a parhelion_test request; returns progress or the compact result digest.",
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
        "description": "Evaluate 4-Stage Submission Gate (G1-G4 + ESCAPE_LOCK) and optionally enqueue validated commit.",
        "roles": ("orchestrator", "worker"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "note": {"type": "string"},
                "enqueue": {"type": "boolean"},
                "force": {"type": "boolean"},
            },
            "required": ["problem"],
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
        "description": "Block on daemon condition (<=45s cap) until strategic events (breakthrough/stagnation/escape_zero/question/directive) or worker results arrive.",
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
        "description": "List teammates for your problem, or send one teammate a finding, report path or calibration update.",
        "inputSchema": {"type": "object", "properties": {
            "recipient": {"type": "string", "description": "agent_id, teammate role name (e.g. algorithm, dataflow, bench-author) or all; omit all arguments to list teammates"},
            "message": {"type": "string"},
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
