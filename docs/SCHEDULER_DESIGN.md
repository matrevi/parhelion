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

> **Three lanes today.** `Q0` (tier 2) and `Q1` (tier 1) are the operator lanes below; the
> non-preemptible `B0` `BACKFILL` lane (tier 0) that pairs missing baseline buckets is
> specified in §6 and can only ever run when no operator ticket is waiting.
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

> **S1 log verdicts are structured, not substring scans** (`gate.py::parse_log_verdicts`,
> §7). A `git checkout` echo quoting an older commit subject
> (`Previous HEAD position was 39fea6c … GATE_FAIL`) used to reject a genuinely passing
> source; `VALIDATION_PASSED` / `EXTENDED_RC=0` is now diagnostic only, because
> `run-npu.sh` prints it unconditionally.

| Stage | Name | Pass Predicate | Failure Tag / Action |
|---|---|---|---|
| **S1** | `SAFETY` | `result.json` `exit_code == 0`, 40-char SHA git tree verified, `FAST_LANE != 1`, `EXTENDED_PASSED` present, **line-anchored** log verdicts (`^EXTENDED_FAILED`, `^EXTENDED_CASES_FAILED`, `^VALIDATION_FAILED`, `^STRESS_CASE_FAIL`, `^NON_DETERMINISTIC`, `^GATE_FAIL`, `^Wrong Answer:`, `^EXTENDED_RC=[1-9]`) all absent, all `mismatched=0/N`, `GATE_DET ... identical=True`. Kernel commits touching `SetAtomicAdd`/`SyncAll` with no prior online `Pass` **must** carry `STRESS_DONE failures=0` or `DETERMINISTIC`. | `REJECTED_GATE` (never enqueued; even `override approve` sdkot bypass S1) |
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

# B0 backfill lane (lowest priority, auto-detect + dedup + idempotent baseline bucket pairing)
python3 -m scheduler.cli_bridge backfill [--title <T>] [--auto | --force] [--dry-run] [--offline]
python3 -m scheduler.backfill_status --json [--offline] [--dispatch [--force] [--dry-run]]
```

---

## 6. B0 Backfill Lane — Automatic Baseline Bucket Pairing (`backfill.py`, `backfill_status.py`)

### 6.1 The pathology: an orange banner nobody clicks
`bench-specs/<Title>/cases_perf.json` is append-only, so **every spectrum revision strands the pinned baseline**: the new bucket ids have no paired hardware timing for `ledger.active_board_sha`, `BenchmarkCalibrator` sets `unpaired_bucket_count > 0`, `UNPAIRED_BUCKETS` fires, and the UI shows *“N perf bucket(s) missing baseline pairing → Backfill (~20s)”* — a **manual** button that only works if a human notices, and that silently degrades `predict_candidate()` whenever it is missed (`sigma` grows by `2.6 x` unpaired fraction).

Live census on 2026-11-18 (`python3 -m scheduler.backfill_status --json`, 41 offline tests, 0 NPU time):

| Operator | Unpaired / buckets | Modality of the pinned baseline | Sum headroom |
|---|---:|---|---:|
| SyncBatchNormGatherStats | **13 / 13** | `none` (`run-npu.sh` never invokes `perf.py`) | 42.49 pts |
| PReluGradUpdate | **12 / 14** | `profiler` (2 buckets) + `dev_us` | 34.07 pts |
| LayerNormV3 | 6 / 14 | `profiler` | 16.23 pts |
| ConfusionTransposeD | 4 / 17 | `profiler` | 10.29 pts |
| CTCLossV2 | 4 / 13 | `profiler` | 9.80 pts |
| StridedSliceGrad | 3 / 16 | `profiler` + `exec` | 7.57 pts |
| PadV3Grad | 3 / 17 | `exec` (0 `profiler`) | 6.27 pts |
| MultilabelMarginLoss | 2 / 18 | `profiler` @ `soc-b` | 5.48 pts |
| ConcatD · QuantUpdateScatter | **0 / 18 · 0 / 14** | complete | — |

**47 unpaired buckets across 8 of 10 operators → coalesced into 8 sessions → `eta_sec = 176s` of die time** (throttle-bound `eta_wall_sec = 6476s` if left strictly to AUTO pacing). The banner's “4” was a per-operator number; the real deficit is an order of magnitude larger and structural — it grows with the spectrum, not with operator effort.

> **Update (2026-11-19, §6.5.1).** Of those 47 ids, **36 are spec placeholders** that no session can ever pair (23 `*_proxy` rows + 13 `SyncBatchNormGatherStats` ids the harness never names). After the 2026-11-18 `MultilabelMarginLoss` pairing the live census is `unpaired_total = 9`, `spec_placeholder_total = 36`, `queued = 2`, `eta_sec = 154 s`. The table above is the *pre*-classification view, kept as the historical measurement.

### 6.2 Three lanes, one lexicographic admission order
The NPU die now has **three** lanes and the ordering is a strict total order, not a heuristic:

| Lane | Ticket `tier` | Owner | Quantum | Enters the die only when |
|---|---:|---|---|---|
| `Q0` `FAST-LANE` | **2** | `opsb test --fast` | `180s` | always (wins every tie) |
| `Q1` `BATCH-GATE` | **1** | `opsb test` | `1200s` | always |
| `B0` `BACKFILL` | **0** | this section | `session_timeout_sec = 180s` | no `tier >= 1` ticket exists **and** the die is idle |

`tools/npu-test.py::am_top_ticket()` admits a waiter iff its `(tier, prio, -created)` tuple is the maximum; `scheduler/backfill.py::ticket_rank()` re-implements the identical comparison (unit-tested for agreement).

**Lemma (operator non-starvation).** Let $O$ be any operator ticket (`tier_O ∈ {1,2}`) and $B$ any backfill ticket (`tier_B = 0`). Admission compares tuples lexicographically, so
$$\text{admit}(O) \iff (\,tier_O, prio_O, -created_O\,) > (\,tier_O', prio_{O'}, -created_{O'}\,)\ \text{for all other } O'$$
and since $tier_O > tier_B = 0$, we get `admit(B) ⇒ ¬∃O waiting`. Aging ($w_j/\tau_{age}$) and WSPT prio are only consulted **inside** a tier, so they can never lift `B0` above an operator: the bound is structural, not probabilistic. $\blacksquare$

**Two more independent barriers** make the lemma hold even if admission were bypassed:
1. The die is a kernel `flock(LOCK_EX)` on `bench-device0.lock`. `B0` performs only `LOCK_EX | LOCK_NB` attempts (never a blocking `flock -w`), so it can never enter the VFS waitqueue ahead of an operator's blocking locker — the “convoy starvation” pathology from §1.1.
2. `B0` holds the die for **perf only** (`python3 validation/perf.py`, no `cmake`/`compiler`, no `git checkout`, no `build.sh`, no `results/` write, no repo lock) with `try/finally` unlock + `perf_timeout_sec` watchdog, so the worst-case delay it can impose on a newly arriving operator is one bounded perf pass (`~20s`), never a build. The baseline commit is **materialized off to the side** (`git worktree add --detach /home/developer/work/.backfill/<Title>-<sha8> <sha>`, or a `git archive` export when worktree is unavailable), built there, and measured with the current `validation/` harness retargeted via `--root`; the operator's working tree is never checked out, rebuilt or written (§6.7).

The client-side gate is stricter than the server-side one: `admission()` refuses to *dispatch* at all when telemetry is offline (`npu_telemetry_offline`, fail-closed), a die lock is `HELD`, `npu_tickets[]` contains any `tier >= 1` entry, or any `PROFILER/PRECISION/FAST-LANE/STRESS/BUILD` task is active. `force=True` — the UI button — bypasses **only** the AUTO throttle (`min_interval_sec`, `max_sessions_per_hour`, `lane_disabled`, backoff); it can never bypass these four hardware-priority predicates.

### 6.3 State machine per dedup key
Dedup key $\kappa = (\text{operator},\ \text{bucket\_id},\ \text{baseline\_sha})$; session key $=\text{sha256}(\text{operator}\Vert \text{baseline\_sha} \Vert \text{sorted buckets})[:16]$ (content addressed, hence re-submitting the same plan is a no-op).

```
                    ┌──────────── (stale pid, crash) ────────────┐
                    ▼                                           │
 UNPAIRED ──plan──▶ QUEUED ──admit──▶ RUNNING ──ok─────────▶ PAIRED  (terminal)
    ▲               ▲    │              │
    │               │    │              ├─ no_timing_evidence / perf_rc_nonzero ─▶ COOLDOWN
    │               │    │              ├─ die_busy │ server_head_mismatch │ no_prebuilt_package
    │               │    │              │        └──▶ QUEUED (zero die time spent) ──┐
    └──(new sha)────┘    └──backoff─────┴─ max_attempts_per_key = 3 exhausted ──▶ GAVE_UP ┘
```

* **`PAIRED`** ⇒ the timing now lives in `<state>/backfill_timings.json` under `(title, sha)`, so `BenchmarkCalibrator._extract_bucket_timings()` merges it as an extra `profiler` source (holes only — a primary `validation.log` value always wins; a cross-`RUNTIME_SOC` pairing is rejected per §3.2), detection drops the bucket, and the ledger keeps an `ok` record for $\kappa$. Three independent layers therefore enforce *“已补过的永不重跑”*: paired ⇒ not unpaired ⇒ not queued; plus `successful_bucket_keys()` short-circuit; plus `attempts >= 3` ⇒ `GAVE_UP`.
* **`baseline_sha` in the key** is what makes this self-healing across board changes: a new active commit re-opens exactly its own keys and nothing else.
* **Resume after interruption**: a `started` record with a dead `pid` is `stale_inflight` (reported, never blocking) and the key becomes retryable immediately; a `started` record with a live `pid` is `running` and blocks a second acquisition of the same session (`backfill_inflight`) — single-flight, no double die grab, no reconciliation step needed because the plan is a pure function of `(specs, results, ledger, telemetry)`.

### 6.4 Append-only ledger (`<state>/backfill_ledger.jsonl`)
One `started` record and exactly one terminal record per session, `flock`-protected append, `sort_keys`, `fsync`; a torn tail line is tolerated by the reader (crash mid-write). Fields: `session_key`, `title`, `baseline_sha`, `buckets[]`, `bucket_keys[]`, `started_epoch/at`, `ended_epoch`, `duration_sec`, `result` (`ok | no_timing_evidence | perf_rc_nonzero | die_busy | server_head_mismatch | no_prebuilt_package | no_perf_script | ssh_timeout | runner_exception`), `measured{bucket: us}`, `measured_count`, `requested_count`, `runtime_soc`, `die`, `lock_wait_sec`, `log`, `pid`, `lane`, `tier`. `ledger_sha = sha256(file bytes)` is the client's cache key: unchanged sha ⇒ nothing happened. The remote side additionally appends die-time telemetry to `~/.opsb/backfill_sessions.jsonl` (kept separate from `die_ledger.jsonl` so the existing duty-cycle bars and macapp consumers are untouched).

### 6.5 Observability contract — `python -m scheduler.backfill_status --json`
Independent module entry (no new `opsb` subcommand, `cli/` untouched); pure `stdout` JSON:

```json
{"unpaired": [{"title","bucket","stratum","dtype","baseline_sha","modality","headroom_pts","key"}],
 "queued":   [{"job_id","title","baseline_sha","buckets","bucket_count","headroom_pts","est_sec","attempts","waiting_sec","lane":"B0","tier":0}],
 "running":  null,
 "last_run": null,
 "eta_sec":  176.0,
 "ledger_sha": "e3b0c442…b855"}
```
plus additive `eta_wall_sec`, `eta_lock_sec`, `eta_build_sec`, `unmeasurable[]`, `stale_inflight[]`, `spec_placeholder[]` / `spec_placeholder_total` / `spec_placeholder_by_operator{}` / `spec_placeholder_keys[]`, `admission` (the full proof: `allowed`, `reasons[]`, `details.die_locks/operator_tickets_waiting/operator_ticket_owners`), `config`, `paired_via_backfill`, `derived_tiers`.

`unmeasurable[]` is the honest counterpart of `queued[]`: a bucket whose id the operator's harness does not implement (`case_not_in_harness`, or a session that completed without emitting a row for it) can never be paired by waiting, so it is parked, never retried, and surfaced for a spec fix instead of burning rebuilds. `--dispatch` is the only mutating mode; `--dry-run` prints the session plan *and* the admission proof without touching the die; `--offline` is fail-closed (plan only). Runtime: **124 ms offline / 1.1 s live** for all 10 operators.

`cli_bridge status` gains one additive field `backfill = {lane, tier, auto_enabled, unpaired_by_operator{}, unpaired_total, spec_placeholder_by_operator{}, spec_placeholder_total, spec_placeholder_keys[], running, running_title, last_run, est_session_sec, ledger_sha}` computed from the already-loaded calibration docs (zero extra scans — the harness sources are read once and cached, `status` stays inside the app's `3.5 s` IPC budget), and a new `backfill` subcommand makes the existing `opsb bench backfill <Title>` button path (`cli/opsb_core/commands/bench_cmd.py → scheduler.cli_bridge backfill`) finally resolve to real work instead of a `127` fallback. The remote probe additionally publishes `npu_tickets[]` (read-only `/tmp/opsb-npu-tickets/*.json`) and a `BACKFILL` task tag for a B0 die holder.

### 6.5.1 Spec placeholders — the three rules for「永远不可测」

A bucket id is a **spec placeholder** when no run can ever pair it. `BackfillScheduler` classifies
every unpaired id with `spec_placeholder_reason()` *before* any admission decision, in this order:

| # | `placeholder_reason` | Test | Example | Fix belongs to |
|---|---|---|---|---|
| 1 | `proxy_suffix` | `id.endswith("_proxy")` | `t256_c1024_b16_proxy`, `bf16_128x256_proxy` | `bench-specs` |
| 2 | `parser_artifact` | the id contains `:` (a harness case id is a plain identifier) | `PReluGradUpdate:median`, `PReluGradUpdate:min` | `bench-specs` |
| 3 | `not_in_harness_case_list` | the operator's own `validation/*.py` never names the id (`harness_names_case()`) | `sbn_w4_c64_small` vs SBN's `f32_c*` case table | `bench-specs` **or** the harness |

Rule 3 is a *pre-flight* verdict only: `_census_rows()` passes `attempted=True` for any
`(operator, bucket)` pair already in the ledger, so a case the lane has actually run is never
reclassified behind its back — the **ledger** owns that status and it keeps landing in
`unmeasurable[reason = case_not_in_harness]`, never in `spec_placeholder[]`. `harness_names_case()`
answers `None` ("no opinion") when `validation/` is missing, so an operator repo that is not cloned
locally can never lose its whole census; a quoted literal *or* a bare token in the harness counts
as "named" (the token guard `(?<![\w.])…(?![\w])` stops `np.floor` from vouching for a case called
`floor`). The read is `validation/*.py` from the **working tree** (read-only, cached per operator,
`< 600 kB` for all ten), not the baseline commit: a harness that names the id today can still be
measured against the old build via the `--root` worktree of §6.7.

Consequences (all additive to the §6.5 contract):

* `unpaired[]`, `unpaired_total`, `unpaired_count` count **real cases only**;
* `spec_placeholder[]` (each with `placeholder_reason`), `spec_placeholder_total`,
  `spec_placeholder_by_operator{}` and `spec_placeholder_keys[]` report the rest;
* `plan()` never emits a placeholder into `queued[]`, so neither `eta_sec` / `eta_wall_sec` nor the
  append-only ledger ever pays die time for one; an operator whose **whole** deficit is placeholder
  (`SyncBatchNormGatherStats`, 13/13) is absent from `queued[]` and its banner number is `0`;
* `cli_bridge status`'s `backfill` badge runs the same classifier over the already-loaded
  calibration docs (`summary()`), so the client number can never hang on an unmeasurable id.

Effect on the §6.1 census (2026-11-19, `--offline`): `unpaired_total 45 → 9`
(`ConfusionTransposeD 1` + `PReluGradUpdate 6` + `StridedSliceGrad 2`),
`spec_placeholder_total = 36` (23 `proxy_suffix` + 13 `not_in_harness_case_list`), `queued 6 → 2`,
`eta_sec 462 s → 154 s`. The 36 were never work: no amount of die time can pair them, only editing
`bench-specs/<Title>/cases_perf.json` or the operator's case table can.

**Root cause of rule 2 (`estimator.parse_validation_perf`), fixed 2026-11-19.** The `PROF_SUMMARY`
branch used to publish `profiler_cases[f"{kernel}:median"]` / `f"{kernel}:min"`, and
`bench-specs/PReluGradUpdate/cases_perf.json` carries bucket ids spelled exactly
`PReluGradUpdate:min` / `PReluGradUpdate:median`. A parser key therefore *paired* a spec bucket:
PRelu showed `observed = true` on both, with `3.32 + 2.31 = 5.63 pts` of headroom, and the S2 gate
estimated a candidate as `57.34` (`"2 paired cases … raw_geo=0.990x"`) out of two kernel
aggregates. The aggregates now live in the additive `prof_kernel_stats` / `prof_summary` return
fields, and `profiler_cases` is case-names-only (`scheduler/tests/test_perf_log_parse.py` pins it,
including a 333-log corpus scan). Two invariants protect the fix:

* **`has_hw_evidence`.** A log that ran `profiler` but only returned kernel aggregates is *not* a
  log without hardware evidence. The `profiler > exec > dev_us` ladder in
  `BenchmarkCalibrator._extract_bucket_timings`, `ScoreEstimator.predict_candidate` and
  `backfill.dispatch` therefore keeps the `profiler` modality with an **empty** pool instead of
  silently substituting the host-enqueue (`exec=`) / `aclrtRecordEvent` (`dev_us`) floors — those
  are a different physical quantity. Before the fix PRelu's fake profiler rows shadowed real
  `dev_us` cases; after it, PRelu simply has *no attributable case-level profiler timing*.
* **Headroom is a partition, not a subtraction.** `headroom_pts` is `Σ(max_pts − current_pts)`
  and `Σmax` is fixed by the bucket weights, so un-observing a bucket does not remove its
  headroom from the total — the colon buckets went from measured `(3.32, 2.31)` to prior
  `(2.78, 2.78)`. What actually changes is the *evidence*: PRelu `observed 2 → 0`,
  `unpaired 12 → 14`, and a bucket that used to count as *measured* is now prior-only. The gate
  delta for the pending candidate moved `−0.47 → 0.00` (S2 HOLD either way, but now for the honest
  reason `"no perf telemetry found"` instead of a manufactured one).



### 6.6 AUTO scheduling without a daemon
No poller, no resident process (§ “no background loops”): the planner is pure, so scheduling is **event-driven** and the events already exist —
1. `cli_bridge schedule-test` → after an operator session releases the die, `backfill_auto()` runs once (bounded: `auto_max_wait_sec = 0` ⇒ a single non-blocking attempt; silent; never raises, so `opsb test` can never regress).
2. `opsb bench backfill <Title>` / `python -m scheduler.backfill_status --dispatch` — the same tick, driven by the app or a human.
3. Every entry point is idempotent: tick with an empty queue costs one local scan and **zero** SSH/NPU work.

Auto pacing (`BackfillConfig`, `<state>/backfill_config.json`): `min_interval_sec = 900`, `max_sessions_per_hour = 4`, `max_attempts_per_key = 3`, `backoff_sec = 3600`, `max_buckets_per_session = 32`, `est_session_sec = 22` (self-calibrating to the ledger median once `>= 2` successful sessions exist). At the measured `22s` per session, the whole 47-bucket deficit costs `176s` of die time — i.e. **≈ 1.1 % of one 4.4-hour operator day**, while removing the `2.6x` unpaired `sigma` penalty from every candidate estimate.

### 6.7 Materializing the baseline commit (the run that actually produces pairings)
A pinned baseline is almost never the server working tree HEAD (each repo sits on its newest
commit), so a session must **reconstruct the baseline off to the side** without touching the
operator's checkout:

1. **Materialize** — `git worktree add --detach /home/developer/work/.backfill/<Title>-<sha8> <sha>`
   (detached; the operator tree keeps its HEAD, its package and its dirty state). Fallback when
   worktree support/permissions are missing: `git archive <sha> | tar -x -C <dir>` (the project
   `build.sh` is git-independent) — recorded as `wt_method: git_archive` instead of `worktree`.
2. **Phase A build, `0.0s` die lock** — `source set_env.sh; sed s/COMPUTE_UNIT=…/ build.sh | bash -s -- -j4`
   *inside the worktree*, then `.opsb_backfill_build.json` (`sha, soc, target, rc, build_sec, at`).
   A warm marker (same sha, `rc == 0`, package present, age `<= build_cache_days = 7`) skips the
   build entirely; a failed build deletes the worktree so a half-built tree can never be cached.
3. **Phase B measure, die held** — the *current* `validation/` harness is retargeted at the
   baseline build (`kperf.py --root <worktree>`, or the current harness injected into the
   disposable worktree for repos without `--root`), because the baseline's own case table may
   predate the bucket shapes (`kperf k_8x4096_*` was added after the pinned `59f8065`). The
   Accel toolkit environment is imported once (`source set_env.sh` → `os.environ`) so `profiler`
   resolves. Timing rows are parsed by `parse_validation_perf` — the same parser as every
   `validation.log` — and stored under `(title, baseline_sha)`.
4. **Cleanup** — a tree with a green marker (`rc == 0`, matching sha, package present) is kept as
   the build cache **even when the measurement failed** (a `136 s` build must not be thrown away
   because the case table later turned out not to contain the id); a failed *build*, an expired
   entry (`> build_cache_days`) or a `--force` rebuild removes it (`worktree remove --force` +
   `worktree prune`). The ledger field `build_ok` — not `result == ok` — drives `est_build_sec`.
   The reaper runs at the head of every session, so a dead worktree can always be recovered.

Measured end-to-end (2026-11-18, `MultilabelMarginLoss` @ `59f8065`, 2 buckets, real die):

| quantity | measured |
|---|---:|
| Phase A build (cold, `worktree add` + `build.sh -j4`) | **20.0 s** (0.0 s die lock) |
| Phase B die hold (`kperf --cases k_8x4096_mean,k_8x4096_none`) | **18.35 s** |
| session wall (`duration_sec`) | **39.47 s** |
| paired timings | `k_8x4096_mean = 691.3 us`, `k_8x4096_none = 688.48 us` (profiler) |
| `unpaired` before → after | **47 → 45** (Multilabel `2 → 0`) |
| warm-cache re-run (same sha) | `build_cached = true`, build skipped, die hold **9.31 s**, `691.2 us` (0.01 % drift) |
| worktree footprint kept as cache | 11 MiB |

### 6.8 Budget, admission for “idle-run”, and the degradation ladder

**Budget** — `est_build_sec = 55 s` (cold-build prior; the measured cold build is `20 s` for a
small operator and `136 s` for `StridedSliceGrad`, so the prior is deliberately pessimistic) plus
`est_session_sec` (learned median of successful die holds, prior `22 s`, measured `18.35 s`).
`eta_sec = sum(budget_sec)` over `queued[]`, split into `eta_lock_sec` (die time) and
`eta_build_sec` (CPU-only, no lock). With all 8 sessions cold: **`eta_sec ≈ 539–616 s`
(`eta_lock_sec ≈ 154–176 s`) ≈ 10 min wall**, of which only ~3 min touches the die.

**Cold-queue reality check (2026-11-18)** — of the 47 unpaired buckets only **11** name a case the
operator's harness actually implements (Multilabel 2, `PReluGradUpdate` 6, `StridedSliceGrad` 2,
`ConfusionTransposeD` 1); the other 36 are spec-side proxy ids (`*_proxy`, plus two parser
artifacts `PReluGradUpdate:min/median`) that no harness can execute. Those are parked as
`unmeasurable[]` (`case_not_in_harness`) rather than retried, so the honest remaining queue is
**3 sessions / ≈ 114 s of die+build time** — exactly why the lane must distinguish “waiting” from
“impossible”.

**Admission for an auto run** (all four must hold; `force` bypasses only the throttle):
`telemetry online` · `no die lock HELD` · `no ticket with tier >= 1` · `no active
PROFILER/PRECISION/FAST-LANE/STRESS/BUILD task` — then the tier-0 ticket and `LOCK_EX|LOCK_NB`
make the operator priority structural (§6.2). A manual “立即补测” additionally skips
`min_interval_sec`/hourly cap/backoff but still sdkot take a die an operator is waiting for.

**Degradation ladder** (never a silent failure — every rung emits a named `reason` that lands in
`queued[].last_failure` and `status.last_run`):

| condition | mechanism | reported |
|---|---|---|
| `git worktree` unavailable (permissions/old git) | `git archive` export into the same directory | `wt_method: git_archive` |
| worktree dir dead / stale | `git worktree prune` + rebuild at the head of every session | `reaped: [...]` |
| disk `bavail < 5 GiB` | refuse before touching anything | `reason: disk_low` |
| baseline sha absent from the server clone | refuse (no fetch side effects) | `reason: baseline_sha_missing` |
| no prebuilt package / build fails | delete worktree, keep attempts for backoff | `reason: build_failed` |
| harness lacks the case ids | inject the current harness, else park the key | `harness_source`, `case_not_in_harness` |
| `profiler`/toolkit missing from `PATH` | import the sourced environment once | `toolkit_env` |
| build+measure exceed budgets | `build_timeout_sec`/`perf_timeout_sec` kill + release the die | `reason: build_failed / measurement_rc_nonzero` |

### 6.9 Verification
Per the hard constraint (“offline replay or `--dry-run`, never grab the die”), the lane ships with `scheduler/tests/test_backfill.py` + `scheduler/tests/test_gate_log_verdicts.py` (73 stdlib `unittest` cases, `<2s`, no SSH in tests): ticket-ordering proof, coalescing/dedup, `build_perf_plan` per repo flag dialect, ledger torn-tail/join semantics, all admission predicates (including *“a waiting Q1 ticket blocks even `force=True`”*), dry-run-never-executes, one-session-per-operator, never-rerun-after-success, backoff + `max_attempts`, dead-pid resume vs live-pid single-flight, `exec=` host-floor normalisation, the status contract, the calibration merge (holes filled, values never overridden, cross-SoC rejected), the §6.5.1 placeholder classifier on a fixture workspace (all three rules, an operator whose deficit is 100 % placeholder, an attempted case that must stay in `unmeasurable[case_not_in_harness]` instead of being reclassified, and the banner/summary split), and four read-only real-workspace invariants (census `==` `calibration.json::unpaired_bucket_count` for all 10 operators; `unpaired_total + spec_placeholder_total ==` the calibration census total; `status` additive + `< 5s`; dry-run writes nothing).


---

## 7. S1 `SAFETY` Log Verdicts: Structured, Not Substring (`gate.py::parse_log_verdicts`)

**The bug (live, tonight).** `results/CTCLossV2/2b9d49de…/validation.log` line 3 is a `git checkout`
echo:

```
Previous HEAD position was 39fea6c test(det_bad): print first 24 mismatched (t, s) coordinates on GATE_FAIL
```

The old S1 rule (`for bad_marker in (...): if bad_marker in log_text`) matched the token `GATE_FAIL`
inside that *quoted commit subject* and rejected the commit (`REJECTED_GATE`, `[S1:SAFETY]
validation.log contains failure token 'GATE_FAIL'`). The log's own verdict was `VALIDATION_PASSED`,
and the source went on to score **`52.85 → 56.21` (`+3.36`)** online — the night's only positive
delta, blocked by the gate.

**The rule.** `parse_log_verdicts()` classifies every non-empty line into exactly one of:

1. **reflog / fetch echo** (`Previous HEAD position was …`, `HEAD is now at …`, `From <url>`,
   `  a1b2c3d..d4e5f6a  main -> origin/main`, `123 files changed`) → *prose*; a marker there is
   counted in `echo_tokens_skipped` and never fatal. This is the class that caused the false positive.
2. **green tail** (`VALIDATION_PASSED`, `EXTENDED_RC=0`) → recorded as `green_tail: true`, purely
   diagnostic: `run-npu.sh` prints `VALIDATION_PASSED` unconditionally, so it can never override a
   real verdict (proof: all 14 `EXTENDED_RC=1` logs also print `VALIDATION_PASSED`).
3. **anchored failure** — the marker is the line's *first* token (an optional `[TAG]` runner prefix is
   allowed): `^EXTENDED_FAILED`, `^EXTENDED_CASES_FAILED`, `^VALIDATION_FAILED`, `^STRESS_CASE_FAIL`,
   `^NON_DETERMINISTIC`, `^GATE_FAIL`, `^Wrong Answer:`, and **new** `^EXTENDED_RC=(?!0)\d+`.
   Only these veto S1.
4. **everything else** (mid-line mention, markdown table cell, stack-trace text, path names) → prose.

**Two consequences measured over all 328 historical `validation.log` files:**

| | count | detail |
|---|---:|---|
| logs the old substring rule rejected | 78 | overwhelmingly genuine failures |
| **false positives removed** | **1** | `CTCLossV2 2b9d49d` (reflog echo, green tail) |
| **failures the old rule missed** | **14** | `LayerNormV3 …` `EXTENDED_RC=1` with `VALIDATION_PASSED` and no `EXTENDED_PASSED` (previously caught only indirectly by `MISSING_EXTENDED_PASSED`, i.e. with a misleading reason) |
| `S1:SAFETY` rejections in `results/submit-queue.jsonl` | 1 | only `CTCLossV2 2b9d49d` — now `SAFETY_OK` (re-evaluated: S1 passes, S2 correctly holds it as *already active at 56.21*) |

**Fixtures** (`scheduler/tests/fixtures/`, byte-derived from the real logs): the trimmed
`ctclossv2_2b9d49d_reflog_echo.log` must pass; `layer_norm_extended_rc1_with_green_tail.log` must
fail on `EXTENDED_RC`; `anchored_failure_with_prose.log` carries a genuine `EXTENDED_FAILED`
alongside table/stack-trace/traceback prose and must fail on line 10 only.
