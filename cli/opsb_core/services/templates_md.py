"""Markdown protocol & spec templates for clean AGENTS.md-first workspaces."""

WORKSPACE_AGENTS_MD = """# {contest_title} · Agent Protocol (`AGENTS.md`-First)

This workspace uses a **zero-noise directory layout**. All NPU server locking, remote builds,
`profiler` kernel timing, 4-stage submission gating, and rate-limited judging run through the
global **`opsb`** CLI and **`Parhelion` (`OpsSupervisor.app`)**. Do **not** write custom SSH or
submission scripts inside operator directories.

## Quick CLI Reference (`opsb`)

```bash
opsb status                          # Contest leaderboard, NPU die status & queues
opsb test <Problem> --fast           # Fast-lane smoke build + public cases on NPU
opsb test <Problem>                  # Full extended boundary + determinism + profiler gate
opsb history <Problem>               # 50-case ratios (tbest / ours) & submission timeline
opsb submit <Problem> <SHA> --note ""# Evaluate 4-Stage Gate & enqueue commit
```

## Scoring & 4-Stage Gate Summary

- **Single-problem score**: `100 * mean_{{i=1..50}}(tbest_i / time_i)` (`5` public + `45` hidden).
- **Zero partial credit**: Any `WA` / `RE` / `TLE` / `CE` zeroes the entire problem (`0` pts); leaderboard mode is `latest`.
- **4-Stage Gate**: `G1 Safety` (`EXTENDED_PASSED` + determinism) -> `G2 Noise Floor` (`Δexpected >= 2.7` pts) -> `G3 Circuit Breaker` (`12h / 6-shot`) -> `G4 Daily Quota Reserve`.
"""

PROBLEM_AGENTS_MD = """# {problem} · Operator Agent Guide

- **Operator**: `{problem}` (Difficulty weight: `×{difficulty}`)
- **Remote Repo**: `https://gitcode.com/{git_owner}/{repo_slug}.git` (private)
- **Editable Scope**: `op_host/`, `op_kernel/`, `tests/`, `DESIGN.md`, `SCORE_HISTORY.md`

## Standard Loop (Zero Skills Required)

1. Read `PROBLEM.md` for tensor signatures, attributes, and tolerances (`fp32: 1e-4`, `fp16/bf16: 1e-3`, `int: 0`).
2. Commit and push to `origin/main` before testing on NPU (`{npu_host}`).
3. Run `opsb test {problem} --fast` for quick verification, then `opsb test {problem}` for full extended + `profiler` validation.
4. Run `opsb history {problem}` and `opsb submit {problem} $(git rev-parse HEAD) --note "<before->after us>"`.
"""

PROBLEM_SPEC_MD = """# {problem} · Problem Specification

- **Problem ID**: `{problem_id}` · **Difficulty**: `×{difficulty}` · **Target**: `soc-a` (`SDK {sdk_version}`)

## Mathematical Semantics & Tolerance Contract
- `float32`: `atol = 1e-4, rtol = 1e-4`
- `float16 / bfloat16`: `atol = 1e-3, rtol = 1e-3`
- `integer`: exact (`0` mismatch)
"""

DESIGN_MD = """# {problem} · Architecture & Tiling Design

## 1. Roofline & Bottleneck Notes (Accel soc-a)
- Vector cores: up to `40` AIV (`192 KB` UB per AIV, `32 B` DMA alignment).

## 2. Experiment Ledger (`profiler` Device `Task Duration(us)`)
| Commit | Hypothesis | Small (`µs`) | Mid (`µs`) | Large (`µs`) | Gate |
|---|---|---|---|---|---|
"""

SCORE_HISTORY_MD = """# {problem} · Online Score History

| UTC Time | Commit | Public Pts (`/10`) | Hidden Pts (`/90`) | Score (`/100`) | Note |
|---|---|---|---|---|---|
"""
