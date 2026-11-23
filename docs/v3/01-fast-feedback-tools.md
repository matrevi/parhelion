# Track 1 Spec · Fast-Feedback Tools (`elf_guard`, `cold3_probe`, `pmu_torch_card`, `bench_scaffold`)

> **Branch**: `v3/fast-tools` | **Worktree**: `.worktrees/v3-fast-tools`
> **Owned Files**: `cli/opsb_core/services/fast_feedback_service.py`, `cli/opsb_core/commands/tools_cmd.py`, `runtime/npu-test.py`, `cli/tests/test_fast_feedback.py`

---

## 1. Goals
Close the 4 highest-cost feedback blind spots observed on soc-a / Judge:
1. **`elf_guard`**: Detect bloated `.o`/`.text` (> 32 KB I-cache tax) and excessive template instantiations (420s judge compile TLE) immediately after every build.
2. **`cold3_probe`**: Quantify true un-warmed 3-run cold-start latency (`cold_3run_mean_us`, `first_iter_us`, `cold_spread_us`) matching Judge's 3-run scoring protocol, catching 10-core DVFS bimodal spikes early.
3. **`pmu_torch_card`**: Produce a single concise, machine-readable + human-readable diagnostic card combining `ours / torch` per-bucket ratios with PMU pipe counters (`MTE2`, `MTE3`, `PIPE_V`, `Scalar`, `BankConflict`) and Top-3 actionable bottleneck buckets.
4. **`bench_scaffold`**: Generate a canonical full-spectrum `cases_perf.json` + `cases_correctness.json` skeleton (`PUBLIC`, `S_SMALL_CORE`, `M_MID_UB`, `L_HBM_TILED`, `X_EXTREME`, multi-dtype `fp32/fp16/bf16`, unaligned tails, `NaN/Inf/0` adversarial cases) into `~/.parhelion/workspaces/<ws-id>/bench-specs/<Title>/` without overwriting existing buckets (strictly append-only).

---

## 2. Service API (`cli/opsb_core/services/fast_feedback_service.py`)

Pure Python 3 stdlib (plus existing `opsb_core.config` & `scheduler.estimator` helpers). Must never require live SSH for offline analysis of logged results.

### 2.1 `evaluate_elf_guard(workspace: Path, title: str, sha: str | None = None, thresholds: dict | None = None) -> dict`
- Reads `results/<Title>/<SHA>/result.json` + `validation.log` (or `build.log`) for latest or explicit `sha`.
- Extracts:
  - `kernel_o_count`, `kernel_o_max_bytes`, `kernel_o_sum_bytes`, `kernel_o_max_text_bytes`
  - `phase_a_cpu_sec` (build time)
  - `template_instantiation_count` (parsed from `KERNEL_BINARY_STATS` or static scan of `op_kernel/` `REGISTER_TILING_KEY` / `TILING_KEY_IS` / `impl` macros when available)
- Default Redline Thresholds (overridable via `config.json` -> `fast_tools.elf_guard`):
  - `warn_text_bytes = 24576` (24 KB — 75% of 32 KB I-Cache), `fail_text_bytes = 32768` (32 KB)
  - `warn_o_max_bytes = 98304` (96 KB), `fail_o_max_bytes = 147456` (144 KB)
  - `warn_build_sec = 90.0`, `fail_build_sec = 150.0` (420s judge wall-clock TLE margin)
  - `warn_templates = 48`, `fail_templates = 72`
- Returns structured verdict:
  ```json
  {
    "title": "...", "sha": "...", "status": "ok | warn | redline",
    "metrics": { ... }, "thresholds": { ... },
    "alerts": [{"code": "ICACHE_TEXT_BLOAT", "severity": "redline", "message": "...", "suggested_kb_tags": ["noinline", "elf-diet"]}]
  }
  ```

### 2.2 `evaluate_cold3_probe(workspace: Path, title: str, sha: str | None = None) -> dict`
- Reads `COLD_START_STATS` / `COLD3_STATS` and `first_iter_us` / `cold_spread_median_us` / per-case `[run1, run2, run3]` samples from `validation.log` and `result.json`.
- Computes:
  - `first_iter_us`, `cold_3run_mean_us`, `warm_median_us`, `cold_tax_us` (`cold_3run_mean - warm_median`), `cold_spread_median_us`, `bimodal_spike_detected` (`True` if `run1 / run3 >= 1.45` and `run1 - run3 >= 1.5 us`, catching 10-core DVFS spikes).
- Returns status (`ok | warn | redline`) + actionable `alerts` with `suggested_kb_tags` (e.g. `["dvfs-10core-spike", "zero-tpipe", "direct-gm-tiling"]`).

### 2.3 `build_pmu_torch_card(workspace: Path, title: str, sha: str | None = None, top_n: int = 5) -> dict`
- Parses latest passing (or specified) `validation.log` + `bench-specs/<Title>/cases_perf.json` + `results/<Title>/calibration.json`:
  - Extracts per-bucket `ours_us`, `torch_us` (from log or spec `tau_torch_us`), `ratio_vs_torch = ours_us / torch_us` (`>1.0` means slower than PyTorch built-in!).
  - Extracts any logged PMU counters (`PipeUtilization`, `aiv_vec_ratio`, `aiv_mte2_ratio`, `aiv_mte3_ratio`, `aiv_scalar_ratio`, `aiv_ub_bank_conflict` / `ResourceConflictRatio`).
  - Classifies dominant hardware bottleneck (`mte2-bound`, `mte3-bound`, `vec-bound`, `scalar-bound`, `bank-conflict`, `cold-launch-tax`, `balanced`, `unknown`).
  - Ranks Top-N worst buckets by `headroom_score = w_hat * max(0, ratio_vs_torch - 0.8)` (or raw `ratio_vs_torch` when `w_hat` unavailable).
  - Attaches `elf_guard` + `cold3` summary and emits a crisp `markdown_card` (<= 25 lines, optimized for injection into Harness user messages!).

### 2.4 `scaffold_bench_specs(workspace: Path, title: str, operator_family: str = "elementwise", dtypes: list[str] | None = None, apply: bool = False) -> dict`
- Target dir: `workspace_bench_specs_dir(workspace) / title` (`~/.parhelion/workspaces/<ws-id>/bench-specs/<Title>/`).
- Supports `operator_family` presets: `elementwise`, `reduction`, `transpose`, `gather_scatter`, `pad_slice`, `sequence_dp`.
- Generates canonical stratified buckets across all 5 strata (`PUBLIC`, `S_SMALL_CORE`, `M_MID_UB`, `L_HBM_TILED`, `X_EXTREME`) covering `fp32/fp16/bf16`, unaligned tail widths (`17`, `31`, `63`, `127`), and adversarial correctness specs (`empty/tiny`, `NaN/Inf/subnormal`, `non-contiguous/odd-prime`, `max-broadcast`).
- **Strict Append-Only Rule**: If `cases_perf.json` or `cases_correctness.json` already exists, NEVER delete or mutate existing bucket IDs; only report/append missing IDs when `apply=True`.

---

## 3. Runner Enhancement (`runtime/npu-test.py`)
- Add `--cold3` flag to `runtime/npu-test.py` (passed into `REMOTE_3PHASE` config `cold3=True`) to record explicit un-warmed 3-run timings (`COLD3_STATS {...}`) alongside existing `COLD_START_STATS`.
- In `REMOTE_3PHASE` Phase A, count `TILING_KEY` / kernel template instantiations from compiled object symbols (`nm`/`objdump` or source scan fallback) and emit `template_instantiations` in `KERNEL_BINARY_STATS` + `result.json`.
- Keep 100% backwards-compatible with existing `validation.log` / `result.json` consumers.

---

## 4. CLI Subcommands (`cli/opsb_core/commands/tools_cmd.py`)
Export `register_fast_tools_subcommands(bn_sub)` registering under `opsb bench`:
- `opsb bench elf [problem] [--sha SHA] [--workspace W] [--json]`
- `opsb bench cold3 [problem] [--sha SHA] [--workspace W] [--json]`
- `opsb bench pmu <problem> [--sha SHA] [--top N] [--workspace W] [--json]`
- `opsb bench scaffold <problem> [--family elementwise|reduction|transpose|gather_scatter|pad_slice|sequence_dp] [--dtypes fp32,fp16,bf16] [--apply] [--workspace W] [--json]`

---

## 5. Verification & Tests (`cli/tests/test_fast_feedback.py`)
- Test `elf_guard` ok / warn / redline transitions across synthetic logs & `result.json`.
- Test `cold3_probe` bimodal 10-core DVFS spike detection vs clean steady runs.
- Test `pmu_torch_card` parsing of real-format `profiler` + `torch` ratios + PMU metrics + compact `markdown_card` generation.
- Test `bench_scaffold` append-only guarantee (existing buckets untouched, new buckets appended cleanly under isolated `PARHELION_HOME`).
- Run `~/.parhelion/venv/bin/python -m pytest cli/tests/test_fast_feedback.py -q` — 100% pass, zero network/SSH calls.
