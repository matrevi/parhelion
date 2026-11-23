# Parhelion v3 Modular Refactor Blueprint (`<= 350 LOC` per module)

## Goal
Decompose the oversized Wave-1 Python modules (`fast_feedback_service.py` 1817 LOC, `triad_kb_service.py` 1514 LOC, `mcp/tools.py` 919 LOC, `antigravity_driver.py` 812 LOC, `codex_driver.py` 765 LOC, `drivers/base.py` 659 LOC) into clean, single-responsibility packages where **every module stays `<= 350 LOC`** (target `150–300 LOC`) while preserving 100% public API compatibility and 100% green `pytest`.

## Target Directory Layout (`cli/opsb_core/`)

### 1. `cli/opsb_core/fast_feedback/` (replaces 1817-LOC `services/fast_feedback_service.py`)
- `__init__.py` — re-exports public symbols
- `common.py` — thresholds, problem/log/result resolution, shared parsing helpers (`<= 280 LOC`)
- `elf_guard.py` — `evaluate_elf_guard`, template instantiation scan, redline alerts (`<= 280 LOC`)
- `cold3_probe.py` — `evaluate_cold3_probe`, 3-run stats & 10-core DVFS bimodal spike detection (`<= 250 LOC`)
- `pmu_torch_card.py` — `build_pmu_torch_card`, `ours/torch` ratios, PMU bottleneck classification, `markdown_card` (`<= 320 LOC`)
- `bench_scaffold.py` — `scaffold_bench_specs`, 6 operator family presets, strict append-only persistence (`<= 320 LOC`)
- `services/fast_feedback_service.py` — thin re-export facade (`<= 45 LOC`)

### 2. `cli/opsb_core/knowledge/` (replaces 1514-LOC `services/triad_kb_service.py`)
- `__init__.py` — re-exports public symbols
- `store.py` — dynamic `PARHELION_HOME` paths, locked atomic JSON/JSONL I/O, index loaders (`<= 260 LOC`)
- `seeding.py` — `ensure_triad_kb_seeded`, `project_readonly_skills` (`0400` `.agents/skills`) (`<= 240 LOC`)
- `query.py` — 3-pillar normalization, `query_kb`, `list_kb_entries`, `get_kb_entry`, `format_prompt_cache_digest` (`<= 320 LOC`)
- `evolution.py` — `validate_evidence`, `compute_confidence`, `record_kb_entry`, `record_kb_feedback`, auto-promote/demote (`<= 320 LOC`)
- `audit.py` — `kb-evolution.jsonl` history, `rollback_last`, `revert_event` (`<= 240 LOC`)
- `services/triad_kb_service.py` — thin re-export facade (`<= 50 LOC`)

### 3. `cli/opsb_core/drivers/` (decomposes `base.py`, `antigravity_driver.py`, `codex_driver.py`, `registry.py`)
- `base.py` — `DriverDiagnostic`, `SessionRecord`, `BaseDriver` ABC (`<= 220 LOC`)
- `macos_gui.py` — `inspect_macos_app_bundle`, `open_sqlite_readonly_wal`, `is_flock_held`, `stage_brief_and_clipboard`, `compact_prompt_for_url` (`<= 260 LOC`)
- `antigravity_acp.py` — pure `AcpSessionClient` (`agy acp` stdio JSON-RPC) (`<= 280 LOC`)
- `antigravity_driver.py` — `AntigravityDriver` GUI/CLI orchestration, project/plugin sync & DB observation (`<= 320 LOC`)
- `codex_appserver.py` — pure `CodexAppServerClient` (`codex app-server` stdio JSON-RPC) (`<= 280 LOC`)
- `codex_driver.py` — `CodexDriver` GUI/CLI orchestration, `.codex/config.toml` sync & `state_5.sqlite` observation (`<= 320 LOC`)
- `brief.py` — `prepare_session_worktree` + `compose_session_brief` (`<= 220 LOC`)
- `registry.py` — driver registry, `doctor_drivers`, `sync_driver_workspace`, `launch_driver_session`, `list_all_sessions` (`<= 280 LOC`)
- Keep `claude_driver.py`, `pi_driver.py`, `ext_drivers.py`, `session_store.py` as-is (all already `<= 210 LOC`).

### 4. `cli/opsb_core/mcp/` (decomposes 919-LOC `mcp/tools.py`)
- `server.py` — keep as-is (`175 LOC`)
- `schemas.py` — JSON schemas for the 9 MCP tools (`<= 240 LOC`)
- `handlers_bench.py` — `parhelion_status`, `parhelion_bench_diagnose`, `parhelion_pmu_card`, `parhelion_elf_cold3_check`, `parhelion_test_plan`, `parhelion_submit_gate` (`<= 300 LOC`)
- `handlers_kb.py` — `parhelion_kb_query`, `parhelion_kb_record`, `parhelion_kb_feedback` (`<= 240 LOC`)
- `tools.py` — thin registry assembling `schemas` + handlers (`<= 90 LOC`)

## Constraints & Verification
- Every Python file in `cli/opsb_core/{fast_feedback,knowledge,drivers,mcp}/` MUST be `<= 350 LOC` (verify with `wc -l`).
- Zero breaking changes to any existing CLI command, MCP tool, or test import path.
- Run `~/.parhelion/venv/bin/python -m pytest cli/tests scheduler/tests -q` — 100% pass, zero skipped regressions, zero personal hardcodes.
