# Task Sequence Scheduler & 4-Stage Submission Gate — Architecture & Mathematical Design

Embedded scheduling, admission-control, and statistical gating engine (`ops-supervisor/scheduler/`) powering **`OpsSupervisor.app`** and the **`opsb`** CLI across 10 concurrent Accel soc-a operator agents.

---

## 1. Bottleneck Topology & Empirical Pathology Analysis

Our multi-agent operator workflow traverses three heterogeneous bottlenecks with sharply asymmetric service rates and failure penalties:

```
10 Operator Agents
   │  git push
   ▼
[Stage A: 32-Core aarch64 Host CPU] ── cmake + compiler + walk_model.py (~25–45s, 32-way parallel)
   │  custom_opp_ubuntu_aarch64.run
   ▼
[Stage B: Accel soc-a NPU Die Lock] ── 1–2 physical dies (85.7s – 861.5s exclusive per run)
   │  EXTENDED_PASSED + profiler Task Duration
   ▼
[Stage C: Online Judging Platform]   ── 1 req / 190s rate limit · 50/day hard cap · mode = "latest"
```

### 1.1 Empirical Audit of 274 Server Runs & Live Lock Starvation
Across all 274 completed server runs in `results/*/*/result.json`:
- **`110` (`40.1%`)** passed full extended + profiling (`PASS_FULL`).
- **`39` (`14.2%`)** failed during `git` checkout or CMake/`compiler` C++ compilation **without executing a single NPU kernel**, yet held `/srv/work-device0.lock` exclusively under the monolithic `run-npu.sh` wrapper.
- **`106` (`38.7%`)** failed sample or boundary correctness (`34` sample mismatches, `72` extended failures), paying full batch setup overhead when a `<90s` fast-lane subset would catch the defect.
- **`6` (`2.2%`)** exited with `exit_code = 0` despite missing `EXTENDED_PASSED` (e.g., `PadV3Grad 8b40181` missing `sys.exit()`), proving `exit_code == 0` alone is unsafe.
- **`11` (`4.0%`)** aborted mid-run with `exit_code = 255` (SSH transport drop under host load).
- **Live Spin-Poll vs. Kernel Waitqueue Convoy Pathology**: `tools/npu-test.py` polls `flock(LOCK_EX | LOCK_NB)` with `time.sleep(10)`, whereas ad-hoc `flock -w 3600` callers enter the Linux kernel VFS waitqueue and wake *immediately* upon lock release. Live telemetry on `ssh npu` caught `ConfusionTransposeD` (`d154c3f`) starving for **104 minutes (`6,285s`)** and `StridedSliceGrad` (`b08510f`) starving for **88 minutes (`5,335s`)** behind younger `flock` arrivals.

### 1.2 Empirical Contest Scoring & Jitter Ground Truth (`results/scoring-rules.md`)
- **Single-problem objective** (50 equally weighted testcases: $|\mathcal{V}|=5$ public, $|\mathcal{H}|=45$ hidden):
  $$S_p = 100 \cdot \frac{1}{50}\sum_{i=1}^{50} \frac{t_{\text{best},i}}{t_i} = \underbrace{2\sum_{i \in \mathcal{V}} r_i}_{S_{p,\text{vis}} \le 10} + \underbrace{2\sum_{j \in \mathcal{H}} r_j}_{S_{p,\text{hid}} \le 90}, \qquad r_i = \min\!\left(1, \frac{t_{\text{best},i}}{t_i}\right)$$
- **Contest total**: $S_{\text{total}} = \sum_{p=1}^{10} d_p \cdot S_p$ where $d_p \in \{1, 2, 3\}$ and $\sum_p d_p = 23$ ($\max = 2300$).
- **Catastrophic `latest` mode**: `ranking_submission_mode = "latest"` with **zero partial credit**. Any single failing hidden case (`WA`/`TLE`/`RE`) zeroes $S_p \to 0$ on the live board.
- **Noise vs. Drift Decomposition**:
  - Hidden-case re-timing jitter across identical-source submissions has $\sigma_{\text{jitter}} \approx \mathbf{2.70\text{ pts}}$ (range $-5.73 \dots +1.59$ pts across 6 measured same-SHA pairs).
  - Pure site-wide `tbest` downward drift is only $\mu_{\text{drift}} \approx \mathbf{-0.08\text{ pts/hr}}$ ($-0.01 \dots -0.26\text{ pts/hr}$).
  - **Host `exec` floor trap**: Python wall-clock `exec` below $\sim 120\,\mu\text{s}$ measures host `aclop` enqueue/OS scheduling jitter ($7.8\text{--}103\,\mu\text{s}$ under CPU load), **not** device execution; only `profiler` `Task Duration(us)` tracks judge timing.

---

## 2. 3-Phase Disaggregated NPU Runner & `PersistentArenaRunner` (`npu_scheduler.py`, `tools/npu-test.py`, `~/.opsb/runtime/`)

### 2.1 Why Raw `device0.lock` Duty Cycle Was `0.0061%` (`61 ppm`)
Auditing 5 representative full server runs (`4,084.1s` / `68.1m` exclusive lock hold on `soc-a`) showed true NPU hardware kernel execution totaled only **`248.04 ms` (`0.0061%` duty cycle)**:
- **`70.3%` (`2,872.9s`)**: `acl_runner.Runner` spawning `680` isolated Python subprocesses (`4.15s/case` for `python3` + `aclInit`), originally used to avoid `libnnopbase.so` (`NnopbaseMatchArgs`) stale executor-cache hits when freed `aclTensor*` / device addresses were immediately recycled.
- **`7.2%` (`292.2s`) + `14.2%` pure compile failures**: `build.sh` deleting `CMakeCache.txt` (line 120) and compiling C++/KernelC inside `device0.lock` while 30.6 of 32 host CPU cores sat idle.

### 2.2 Three-Phase Disaggregated Execution Pipeline
Both `opsb test` (`scheduler.cli_bridge schedule-test`) and `tools/npu-test.py` share a single unified 3-Phase engine (`strict lock order: repo-<Title>.lock < validation.lock(SH) < device0.lock(EX)` — deadlock-free):

1. **Phase A — `A_CPU_BUILD_AND_PREFILTER` (`0.0s` `device0.lock`, 10-way parallel across 32 CPU cores)**:
   - Acquires per-problem `/srv/work-repo-<Title>.lock` only and writes live heartbeat `~/.opsb/cpu_builds/<Title>.json`.
   - Performs detached `git checkout <SHA>`, runs pure-host `validation/walk_model.py` (when present), compiles `build.sh -j4` (**without `-e`**, zero `aclInit`), and pre-compiles `examples/build`.
   - Any compile error exits immediately with **`lock_waste_sec = 0.0`**. Passing `--build-only` exits `0` right after Phase A for sub-25s compile checks.
2. **Phase B — `B_NPU_ARENA_DIE_EXEC` (Cooperative WSPT Ticket $\to$ `13s–25s` Exclusive Die Slot)**:
   - Enters `/tmp/opsb-npu-tickets/<pid>.json`, acquires `device{0,1}.lock`, and updates `~/.opsb/die_session.json` (`acl_setup` $\to$ `npu_exec` $\to$ `profiler`).
   - Transforms `validation/run-npu.sh` in-memory to replace the redundant `build.sh -j4 -e` rebuild with `bash examples/run.sh` against Phase A's pre-built `libcust_opapi.so`.
   - Injects `OPSB_ARENA_ACCEL=1` and `PYTHONPATH=/home/developer/.opsb/runtime:$PYTHONPATH`. `sitecustomize.py` / `arena_accel.py` installs a `sys.meta_path` post-import hook on `acl_runner`, routing `Runner.run` in-process (`~2 ms/case` vs. `4,150 ms/case`) through a **Pointer Keepalive Ring** (`>= 96` cases or `>= 2 GiB` HBM + `64B` tail guard pad before `aclrtSynchronizeStream` + flush), eliminating `NnopbaseMatchArgs` address-reuse collisions while falling back to `--case` subprocesses on any non-precision ACL error.
3. **Phase C — `C_POST_LOCK_LEDGER` (`0.0s` `device0.lock`)**:
   - Releases `device0.lock` immediately upon process exit, records `{phase_a_cpu_sec, phase_b_die_sec, npu_exec_sec, acl_host_sec, lock_waste_sec, arena_cases_inproc, rc}` in `~/.opsb/die_ledger.jsonl` (powering 10-minute duty-cycle bars and `slots_per_hr`), and writes `result.json`.

Active processes are classified into six semantic tags via read-only `/proc/locks` + `/proc/<pid>` + `~/.opsb/*` heartbeats (never touching `.lock` files):
`BUILD` · `FAST-LANE` · `PRECISION` · `PROFILER` · `STRESS` · `WAITING-DIE`.

### 2.2 Two-Queue MLFQ + WSPT Value-Density + Anti-Starvation Aging
We combine **Multi-Level Feedback Queue** rules (Corbató 1962; Arpaci-Dusseau *OSTEP* Ch. 8; Gu et al. *Tiresias* NSDI'19) with **Smith's WSPT Rule** ($w_j / p_j$, Smith 1956) and **Gittins Index** value-density weighting:

- **`Q0` (`FAST-LANE`, Interactive Probe Queue)**: Quantum $q_0 = 180\text{s}$ (`FAST_LANE=1`, expected duration $\hat{p}_{p,0} = \text{clamp}(0.30\,\hat{p}_{p,1}, 45\text{s}, 90\text{s})$). Runs sample + targeted boundary shapes + quick `profiler`. If an active `Q0` task exceeds $q_0 = 180\text{s}$, it is demoted to `Q1` (*OSTEP Rule 4a*). `FAST_LANE=1` artifacts are strictly barred from online submission at Stage 1.
- **`Q1` (`BATCH-GATE`, Full Validation Queue)**: Quantum $q_1 = 1200\text{s}$ (`FAST_LANE=0`, duration $\hat{p}_{p,1}$ learned dynamically from non-queued `result.json` medians). Runs full process-isolated `extended.py` + determinism/stress + full spectrum `profiler`.
- **WSPT + Aging Priority Score**:
  $$P_j(t) = \underbrace{\frac{d_p \cdot H_p^{\text{eff}} \cdot \psi_{\text{cb}}(p)}{\hat{p}_j}}_{\text{WSPT value density (w-pts/sec)}} + \underbrace{\frac{w_j(t)}{\tau_{\text{age}}}}_{\text{Aging boost}}$$
  where:
  - $H_p^{\text{eff}} = \max(0, S_{p,\text{top1}} - S_{p,\text{curr}})$ is empirical recoverable headroom against the site-wide rank-1 frontier $S_{p,\text{top1}}$ (boosted to $\ge S_{p,\text{best}} + 25$ if $S_{p,\text{curr}} = 0$),
  - $\psi_{\text{cb}}(p) = 0.25$ if problem $p$'s circuit breaker is `OPEN` (dampening exhausted baselines) and $1.0$ otherwise,
  - $\tau_{\text{age}} = 600\text{s}$ ($+1.0$ priority unit per 10 minutes waited), plus **OSTEP Rule 5 Promotion** ($Q_1 \to Q_0^*$ once $w_j \ge 1800\text{s}$), guaranteeing bounded wait times.

### 2.3 Measured WSPT Value-Density Ledger Across All 10 Problems

| Rank | Problem | Diff $d_p$ | Board $S_p$ | Site Top-1 | Headroom $H_p$ | $d_p H_p$ | Med $Q_1$ $\hat{p}_1$ | $Q_1$ WSPT | $Q_0$ WSPT | Scheduler Policy |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | **CTCLossV2** | $\times 2$ | 35.15 | 80.44 | 45.29 | 90.58 | 85.7s | **1.057** | **2.013** | `Q0/Q1 HIGH_DENSITY` (34× Multilabel!) |
| 2 | **ConcatD** | $\times 3$ | 52.46 | 86.48 | 34.02 | 102.06 | 134.3s | **0.760** | **2.268** | `Q0/Q1 HIGH_DENSITY` |
| 3 | **QuantUpdateScatter** | $\times 2$ | 44.76 | 90.11 | 45.35 | 90.70 | 160.8s | **0.564** | **1.882** | `Q0/Q1 HIGH_DENSITY` |
| 4 | **SyncBatchNormGatherStats** | $\times 2$ | 54.93 | 92.19 | 37.26 | 74.52 | 153.7s | **0.485** | **1.617** | `Q0/Q1` (requires non-AIV baseline switch) |
| 5 | **LayerNormV3** | $\times 2$ | 26.30 | 72.79 | 46.49 | 92.98 | 298.5s | **0.312** | **1.039** | `Q0/Q1 HIGH_DENSITY` |
| 6 | **StridedSliceGrad** | $\times 3$ | 44.58 | 89.50 | 44.92 | 134.76 | 591.3s | 0.228 | **1.497** | `Q0 -> Q1 SPLIT_ITERATE` (6.6× faster in `Q0`) |
| 7 | **ConfusionTransposeD** | $\times 3$ | 42.29 | 85.96 | 43.67 | 131.01 | 631.3s | 0.208 | **1.456** | `Q0 -> Q1 SPLIT_ITERATE` (7.0× faster in `Q0`) |
| 8 | **PadV3Grad** | $\times 3$ | 28.07 | 73.99 | 45.92 | 137.76 | 861.5s | 0.160 | **1.531** | `Q0 -> Q1 SPLIT_ITERATE` (9.6× faster in `Q0`) |
| 9 | **PReluGradUpdate** | $\times 1$ | 55.69* | 84.68 | 28.99 | 28.99 | 249.6s | 0.116 | 0.387 | Instant #1 (`CRITICAL_RESTORE`) whenever $S_p=0$ |
| 10 | **MultilabelMarginLoss** | $\times 2$ | 53.08 | **65.55** | **12.47** | **24.94** | **803.0s** | **0.031** | 0.277 | `DEPRIORITIZE (NEAR_FRONTIER, Rank #32)` |

> **Key Insight**: Without frontier-grounded WSPT, `MultilabelMarginLoss` consumed $803\text{s}$ per `Q1` slot and 8 online submissions chasing diminishing returns only $12.5\text{ pts}$ below site-wide #1 (`0.031 w-pts/s`), while `PadV3Grad` (`137.8` weighted pts headroom) and `CTCLossV2` (`1.057 w-pts/s`) waited behind it. Routing `PadV3Grad` / `StridedSliceGrad` / `ConfusionTransposeD` iterations through `Q0` lifts their value density by **6.6×–9.6×** (`1.46–1.53 w-pts/s`).

---

## 3. Expected Score Estimator & 4-Stage Submission Gate (`estimator.py`, `gate.py`)

### 3.1 `tbest(t)` Non-Stationarity & Contemporary Score Calibration
Because $S_p(\text{SHA}, t) = 2\sum_{i=1}^{50} t_{\text{best},i}(t)/\tau_i(\text{SHA})$ decays monotonically as site-wide $t_{\text{best},i}(t)$ tightens ($\gamma_p(\text{11-10}\to\text{11-18}) \in [0.9473, 0.9872]$, i.e., $-0.55\text{ to }-2.52\text{ pts}$ over 8 days on untouched rows), comparing $S_{\text{live}}(t_{\text{now}})$ against raw historical $S(\text{SHA}^*, t_0)$ creates phantom restore gaps. `ScoreEstimator` enforces three invariants:

1. **Observed Active Decay Factor ($\gamma_p$)**: Matching `score-snapshot.json`'s `(submission_id, submission_time)` against `submit-queue-outcome.jsonl` identifies $(\text{active\_sha}, S_{\text{judged}}, t_{\text{active}})$ to $<0.5\text{s}$ and computes:
   $$\gamma_p = \text{clamp}\!\left(\frac{S_{\text{live}}(t_{\text{now}})}{S_{\text{judged}}(\text{active\_sha}, t_{\text{active}})}, \; 0.5, \; 1.0\right)$$
2. **Active-Board & C++ Source Identity Rule**: Whenever `candidate_sha == active_sha` (or its comment-stripped `op_host/` + `op_kernel/` C++ fingerprint equals `active_sha`, e.g., doc-translation commits `SyncBN 9c743dc`, `SSG b08510f`), its contemporary score is **identically** $S_{\text{live}}(t_{\text{now}})$ (`true_restore_gain = 0.0`).
3. **Superseded Historical SHA Discounting**: Any historical `Pass` draw $(s_k, t_k)$ of $\text{SHA} \ne \text{active\_sha}$ is discounted by $\gamma_p$ over $[t_{\text{active}}, t_{\text{now}}]$ plus residual drift $-0.015\text{ pts/hr}$ strictly over any pre-active gap $\max(0, t_{\text{active}} - t_k)$:
   $$\tilde{s}_k(t_{\text{now}}) = \max\!\left(0.0, \; s_k \cdot \gamma_p - 0.015 \cdot \max\!\left(0.0, \frac{t_{\text{active}} - t_k}{3600}\right)\right)$$
   - **`SyncBatchNormGatherStats`** (`544439e` active: $57.45 \to 54.93$, $\gamma_p = 0.9561$): identity $\implies$ `contemporary_best = 54.93`, `true_restore_gain = 0.00`.
   - **`StridedSliceGrad`** (`c0630a2` active: $47.06 \to 44.58$, $\gamma_p = 0.9473$; superseded `45eb08f` $48.96$): $\tilde{s} = 48.96 \times 0.9473 - 0.03 = \mathbf{46.35}$ (`+1.77 < 2.50` $\implies$ held, **not** a restore target).
   - **`ConcatD`** (`2dd9acd` active: $46.29$; superseded `899a2dc` raw $52.87 / 52.49$): $\tilde{s} = \mathbf{49.71}$ (`+3.42 >= 2.50` $\implies$ **sole genuine `closing_restore_target`** across all 10 problems).

### 3.2 Same-`RUNTIME_SOC` Hardware-Epoch Pairing (`soc-b` vs. `soc-a`)
Pre-Nov-18 logs ran on `RUNTIME_SOC=soc-b` (`aclrtRecordEvent` / `exec=` host-enqueue floor $\approx 7.0\,\mu\text{s}$), whereas Nov-18 logs run on `RUNTIME_SOC=soc-a` (host-enqueue floor $\approx 11.1\text{--}12.5\,\mu\text{s}$):
- **Same-SoC Baseline Selection**: When `cand_sha` runs on `soc-a` while `active_sha` was validated on `soc-b`, `_select_same_soc_baseline()` automatically pairs against an `soc-a` baseline log with matching C++ source fingerprint (`7926c36` for `PadV3Grad`, `b08510f` for `SSG`, `9c743dc` for `SyncBN`) or the earliest passing `soc-a` baseline (`af5b55a` for `PReluGradUpdate`, `cca8ba3` for `Multilabel`, `4eee530` for `ConcatD`).
- **Telemetry Fidelity Hierarchy**:
  1. Hardware `profiler` `Task Duration` (`MS_PROF`, `KPERF_JSON`, `KPERF`, `KERNEL_JSON`, `KERNEL_PERF`, `PERF`, and `PROF_SUMMARY` `median_us` + `min_us`) strictly overrides `PERF_JSON` `dev_us` (`PReluGradUpdate 44ba022` vs `af5b55a`: `12.24/12.02 -> 9.92/9.04 us` = `1.281x` raw / `1.239x` eff $\implies$ `READY`, `+9.43` pts!).
  2. Same-SoC `exec=` pairing (`> 11.5 us` above the `11.1 us` `soc-a` floor) accurately captures `PadV3Grad` pipeline speedups without cross-SoC floor distortion.
  3. Over common cases $\mathcal{C}$, $k_{\text{geo}} = \exp\!\left(\frac{1}{|\mathcal{C}|}\sum_{c \in \mathcal{C}} \ln \text{clamp}(k_c, 0.25, 3.5)\right)$ and $k_{\text{eff}} = 1 + \gamma_{\text{cov}}(k_{\text{geo}} - 1) - 0.03\,N_{\text{reg}}$ project onto $S_{p,\text{vis}} + S_{p,\text{hid}}$ via smooth concave saturation $r'(k) = \min\!\left(0.98, \frac{r \cdot k}{1 + 0.35\,r\max(0, k-1)}\right)$.

### 3.3 Closed-Loop Simplex MAP Benchmark Calibration & Escape Lock (`calibration.py`, `bench-specs/`)
Declarative benchmark specifications in `ops-supervisor/bench-specs/<Problem>/{cases_correctness.json,cases_perf.json}` decouple adversarial correctness from stratified score-spectrum fitting across all 10 problems:
- **Adversarial `6×5 + D5` Correctness Grid (`cases_correctness.json`)**: `G0_DEGEN`, `G1_ODD_ALIGN` (`1B..129B` primes), `G2_CORE_TILE` (`1,3,39,40,41` cores & `tile ± 1`), `G3_HIGH_RANK`, `G4_DTYPE_CROSS`, `G5_EDGE_ATTR` crossed with `D0_STD`, `D1_OVERFLOW`, `D2_SUBNORM`, `D3_CANCEL` ($1000 + 0.01z$), `D4_ULP_INF`, plus `D5_ESCAPE_REG` permanent online-escape reproducers (`0x5A`/`NaN` canary + `32B` guard bands).
- **Stratified `K=13..18` Hidden Proxy Spectrum (`cases_perf.json`)**: `PUBLIC` (`10/100` pts, `P1..P5`) + `S_SMALL_CORE` (`0.30` hidden prior), `M_MID_UB` (`0.35` hidden prior covering `fp32/fp16/bf16/int8`), `L_HBM_TILED` (`0.20`), and `X_EXTREME` (`0.15`, **capped at $w_k \le 0.04$/bucket**).
- **Simplex MAP Model (`<200 ms` across all 10 problems, pure stdlib)**:
  $$\hat{S}_{\text{hid}}(\text{SHA}) = 90 \sum_{k=1}^{K} \hat{w}_k \cdot \min\!\left(1,\, \frac{\hat{\beta}_p \theta_k^{(0)}}{\tau_k(\text{SHA})}\right), \qquad \theta_k^{(0)} = \min\!\left(0.98\min_m \tau_{m,k},\; \max(\tau_{\text{floor}}, \eta_p \tau_k^{\text{torch}})\right)$$
  1. **Tier-1 Tightness Scalar ($\hat{\beta}_p$)**: Exact 1D bisection guarantees zero anchor bias on the active board commit.
  2. **Tier-2 Capped Simplex MAP ($\hat{w} \in \Delta^{K-1}, 0.005 \le w_k \le u_k$)**: 3-pass joint Block-Coordinate + Entropic Mirror Descent (Exponentiated Gradient with exact water-filling Bregman projection) minimizing Huber loss ($\delta = 2.70\text{ pts}$) + $\lambda_{\text{KL}} D_{\text{KL}}(w \parallel w^{(0)}) + \frac{\lambda_2}{2}\|w - w^{(0)}\|_2^2$, plus diagonal Laplace covariance $\Sigma_w \to \sigma_{\text{epistemic}}$. Across all 10 problems (`results/<Problem>/calibration.json`), **fitted mean MAE reaches `1.88 pts`** (`ConcatD X_EXTREME` automatically pruned `0.150 -> 0.020`, `mae = 0.94 pts`; `PadV3Grad` `0.06 pts`; `CTCLossV2` `0.13 pts`; `QuantUpdateScatter` `0.63 pts`; `StridedSliceGrad` `0.75 pts`).
  3. **Three Closed-Loop Residual Triggers**: `UNPAIRED_BUCKETS` (flags missing `profiler`/`bf16` buckets), `PROLOGUE_TAX_ALERT` / `PRUNE_PHANTOM_WEIGHT` (small-core dispatch tax vs. synthetic tail), and **`ESCAPE_LOCK`** in Gate Stage 1 (`SAFETY`) whenever the latest online submission zeroed (`WA`/`TLE`/`RE`) after passing local S1 — blocking unproven new commits (while still permitting proven baseline restores) until cleared by `verify-escape --title <T> --bad-sha <S1> --good-sha <S2>`.

### 3.4 The 4-Stage Submission Gate & Token-Bucket Reserve

| Stage | Name | Pass Predicate | Failure Tag / Action |
|---|---|---|---|
| **S1** | `SAFETY` | `result.json` `exit_code == 0`, 40-char SHA git tree verified, `FAST_LANE != 1`, `EXTENDED_PASSED` present, zero `EXTENDED_FAILED`/`VALIDATION_FAILED`/`STRESS_CASE_FAIL`/`Wrong Answer:`, all `mismatched=0/N`, `GATE_DET ... identical=True`. Kernel commits touching `SetAtomicAdd`/`SyncAll` with no prior online `Pass` **must** carry `STRESS_DONE failures=0` or `DETERMINISTIC`. | `REJECTED_GATE` (never enqueued; even `override approve` sdkot bypass S1) |
| **S2** | `NOISE_FLOOR` | $\Delta_{\text{expected}} \ge +2.50\text{ pts}$ ($\approx 0.93\,\sigma_{\text{jitter}}$), **OR** $S_{p,\text{curr}} = 0$ / `status != "Pass"` (`CRITICAL_RESTORE`), **OR** explicit `force=True` with $\Delta_{\text{expected}} \ge 0$ (and `cand_sha != active_sha`). | `HOLD_LOW_GAIN` (parked in `held_items` to preserve daily quota) |
| **S3** | `CIRCUIT_BREAKER` | Rolling $12\text{h}$ window compares **decay-adjusted contemporary scores** (`discount_draw_to_contemporary`) so 8-day-old peaks never penalize new-epoch passes; trips at $\ge 6$ consecutive non-improving shots or $\ge 2$ consecutive zeroing errors (`WA`/`TLE`/`RE`). `CRITICAL_RESTORE` passes via `HALF_OPEN`. | `CIRCUIT_OPEN` (`Switch Baseline` required) |
| **S4** | `QUOTA_RESERVE` | Dual Token Bucket: ① Rate bucket ($1\text{ token} / 190\text{s}$ $\to$ `RATE_WAIT`); ② Beijing-day (`UTC+8` `00:00` reset) 50-shot budget reserves $\max(2, N_{\text{true\_restore}} + 1)$ slots strictly for problems with `true_restore_gain >= 2.5` or $S_p = 0$. Approved queue drains by `(CRITICAL_RESTORE desc, d_p * delta_expected desc)`. | `HOLD_LOW_GAIN` (if inside closing reserve) or `RATE_WAIT` |

---

## 4. Causal Retrospective Validation on Historical Submissions (`retrospect`)

Replaying all **65 chronological records** (`49` live platform submissions) in `results/submit-queue-outcome.jsonl` strictly causally (zero lookahead — at index $i$, only records $1 \dots i-1$ are visible):

| Metric | Historical Un-Gated | With 4-Stage Gate | Improvement |
|---|---:|---:|---|
| Live submissions consumed | 49 | **27** | **22 slots saved (`44.9%`)** |
| Catastrophic zeroings (`WA`/`TLE`/`RE` $\to 0$) | 5 | **1*** | **4 / 5 zeroings prevented (`80%`)** |
| Board score regressions (`Pass` $< S_{\text{prev}}$) | 12 | **3** | **9 / 12 regressions prevented (`75.0%`)** |
| Genuine breakthroughs held ($\Delta_{\text{actual}} \ge +2.5$ pts) | — | **0** | **100% recall on real gains (incl. `LayerNormV3 +16.53`, `CTCLossV2 +15.67`, `PadV3Grad +10.78`)** |
| Hit 50/day `daily_limit` blocking `CTCLossV2 02c2048` (`+5.76` pts)? | **Yes** (rejected ×2, delayed 15h) | **No** (`~29/50` used) | **`+11.52` weighted pts landed same day** |
| Calibrated MAP MAE / Causal Replay MAE (`32` `Pass` pairs) | Naive RMSE `8.66` pts | **Calibrated MAE `1.88` pts · Causal MAE `3.01` / RMSE `4.25` pts** | Halves multi-regime RMSE down to $\sigma_{\text{jitter}} = 2.70\text{ pts}$ floor |

*\*The single unprevented zeroing (#18 `PReluGradUpdate 0761133`) was a transient platform-side TLE flake on an already-validated baseline while restoring a missing board row (`prev_board = 0`), immediately recovered on #19 (`56.56`).*

### Key Concrete Catches in Replay
1. **Stage 1 (`SAFETY`) Caught 3 Catastrophic Zeroings Before Upload**:
   - `#4 MultilabelMarginLoss f77146d` (`SetAtomicAdd` out-of-bounds $\to$ **TLE, 0 pts**) & `#9 af84e85` (16-bit `SyncAll` scratch race $\to$ **Runtime Error, `-46.09` pts**): both used cross-core primitives (`SetAtomicAdd`/`SyncAll`) without `STRESS_DONE` verification — saving both the zeroing shots and their panic rollbacks.
   - `#30 LayerNormV3 07ae054` (**Wrong Answer, `-27.16` pts**): modified golden reference and lacked `EXTENDED_PASSED` (`rc=0` silent trap) — saving 3 slots (#30, #36, #38).
2. **Stage 2 (`NOISE_FLOOR`) Prevented 9 Score Regressions & 18 Noise Slots**:
   - `#12 ConcatD a458798` (`est +0.28 < +2.5` $\to$ actual **`-2.42` pts**), averting the 4-shot panic rollback spiral (#12–#15, including `#14` WA zeroing!).
   - `#24 PadV3Grad d3160b6` (`+0.03` pts) & `#25 StridedSliceGrad c48e3cf` (`+1.42` pts): previously bypassed `skipped` entries due to 7-char vs. 40-char SHA mismatch; canonical 40-char resolution + S2 holds both.
   - `#27 ConcatD 3c15ca9` (11-band `-5.5%`, `est +0.30` $\to$ actual **`-5.13` pts / `-15.39` w-pts**): held!
   - `#28 MultilabelMarginLoss 197e082` (2 tail bands `4.3x`, 11 anchors `1.0x`, `est +2.30` $\to$ actual **`-0.99` pts**): held!
   - `#34 PReluGradUpdate 2d5837e` (`est +0.49` $\to$ actual **`-2.36` pts**) & `#45 StridedSliceGrad c0630a2` (`est +0.86` $\to$ actual **`-1.90` pts**): held!
   - `#52 ConcatD 2dd9acd` (live Nov 18 shot: `est +0.39` $\to$ actual **`-6.20` pts / `-18.60` w-pts**): held!
3. **Zero False Holds on Real Gains**:
   - Approved `#17 Multilabel 59f8065` (`+12.68` pts), `#20 ConcatD 899a2dc` (`+1.86` new peak), `#44 PadV3Grad 0fc367d` (`+3.23` pts, after filtering `<150 us` host-enqueue plateaus), `#46 ConcatD 899a2dc` closing restore (`est +2.87`, actual **`+5.71` pts**), and `#50 CTCLossV2 02c2048` (`+37.18` / `+5.76` pts).

---

## 5. Unified CLI & `OpsSupervisor.app` Bridge (`scheduler/cli_bridge.py`)

Dependency-free Python 3 stdlib CLI returning pure JSON on `stdout` (`PYTHONPATH=ops-supervisor`):

```bash
# Full live snapshot (NPU MLFQ+WSPT queue, 4-stage submit queue, quota bucket, problem priorities)
python3 -m scheduler.cli_bridge status --workspace /Users/winbeau/Competitions/ops-bench

# Evaluate 4-stage gate & score estimator for any commit
python3 -m scheduler.cli_bridge evaluate --title CTCLossV2 --sha 02c2048 --speedup 1.35

# Enqueue via 4-stage gate (READY / CRITICAL_RESTORE -> submit-queue.jsonl; HOLD -> state)
python3 -m scheduler.cli_bridge submit --title PadV3Grad --sha 7926c36 --note "..." [--force] [--dry-run]

# Single-consumer WSPT priority drain (CRITICAL_RESTORE > difficulty * delta_expected, 190s pacing)
python3 -m scheduler.cli_bridge drain [--once | --watch] [--dry-run]

# Operator / UI overrides & threshold tuning
python3 -m scheduler.cli_bridge override --title ConcatD --sha 2dd9acd --action <approve|skip|rollback>
python3 -m scheduler.cli_bridge config --noise-floor 2.5 --reserve 5 --q0-quantum 180 --aging-tau 600

# 3-Phase NPU validation (`--build-only` 0s-lock CPU compile | `--fast` Q0 | full Q1 PersistentArenaRunner)
python3 -m scheduler.cli_bridge schedule-test --title PadV3Grad [--build-only] [--fast] [--dry-run]

# Closed-loop Simplex MAP calibration, 6x5 bench coverage, residual diagnostics & escape verification
python3 -m scheduler.cli_bridge calibrate [--title ConcatD]
python3 -m scheduler.cli_bridge bench-status [--title ConcatD]
python3 -m scheduler.cli_bridge diagnose --title ConcatD
python3 -m scheduler.cli_bridge verify-escape --title MultilabelMarginLoss --bad-sha af84e85 --good-sha 59f8065

# Causal historical replay & accuracy verification
python3 -m scheduler.cli_bridge retrospect --workspace /Users/winbeau/Competitions/ops-bench
```
