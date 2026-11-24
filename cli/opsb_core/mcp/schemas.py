"""JSON Schema definitions for the 9 focused Parhelion MCP tools (`parhelion_*`)."""
from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "parhelion_status",
        "description": "Live contest scores, active NPU telemetry (optional), quota & queue summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "Optional problem title filter"},
                "include_npu": {"type": "boolean", "description": "Probe live NPU via SSH (default false)"},
                "include_queue": {"type": "boolean", "description": "Include queue preview (default true)"},
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
        "description": "Query 4-Pillar KB (breakthrough + snippets + hardware + community + anti-patterns) by symptom, regime, or dtype.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bottlenecks": {"type": "array", "items": {"type": "string"}},
                "regime": {"type": "string"},
                "dtype": {"type": "string"},
                "pillar": {"type": "string", "enum": ["breakthrough", "snippets", "hardware", "community", "all"]},
                "text": {"type": "string"},
                "include_anti_patterns": {"type": "boolean"},
                "limit": {"type": "integer"},
                "digest": {"type": "boolean"},
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
        "name": "parhelion_test_plan",
        "description": "Preview 3-Phase WSPT NPU test plan (--build-only / --fast / full) or execute `opsb test`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string"},
                "sha": {"type": "string"},
                "mode": {"type": "string", "enum": ["build-only", "fast", "full"]},
                "execute": {"type": "boolean"},
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
                "strategic_only": {"type": "boolean", "description": "Wake only on strategic escalations & results (default true)"},
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
        "description": "Worker-only: report autonomous progress, breakthrough (for cross-problem generalization), 6-shot stagnation, escape_zero, question, or final result.",
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
