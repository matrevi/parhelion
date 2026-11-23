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
        "description": "Query Triad KB (breakthrough + snippets + community + anti-patterns) by symptom, regime, or dtype.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bottlenecks": {"type": "array", "items": {"type": "string"}},
                "regime": {"type": "string"},
                "dtype": {"type": "string"},
                "pillar": {"type": "string", "enum": ["breakthrough", "snippets", "community", "all"]},
                "text": {"type": "string"},
                "include_anti_patterns": {"type": "boolean"},
                "limit": {"type": "integer"},
                "digest": {"type": "boolean"},
            },
        },
    },
    {
        "name": "parhelion_kb_record",
        "description": "Propose a new snippet, community card, or anti-pattern (strictly evidence-gated).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pillar": {"type": "string", "enum": ["snippets", "community"]},
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
]
