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

## 2. NPU Task Sequence Scheduler (`scheduler/npu_scheduler.py`)

### 2.1 Split-Phase CPU/NPU Pipeline (Admission Pre-filtering)
Following disaggregated execution principles, `schedule-test` splits every validation into two decoupled resource domains:
1. **Phase A — `A_CPU_PREFILTER` (0s NPU Lock)**: Acquires only a repo-scoped mutex `/tmp/opsb-repo-<Title>.lock`, checks out the exact detached 40-char `<SHA>`, runs pure-host tile state-machine verification (`validation/walk_model.py` when present), and executes 32-core CMake + Compiler KernelC compilation (`build.sh`). Compile or state-walk failures abort in $\sim 15\text{--}35\text{s}$ with **zero NPU die contention**.
2. **Phase B — `B_NPU_DIE_EXEC` (Cooperative Ticket Lock)**: Only after `.run` artifacts are built does the job enter `/tmp/opsb-npu-tickets/` to acquire `/srv/work-device{0,1}.lock` in priority order.

Active processes are classified into six semantic tags via read-only `/proc/locks` + `/proc/<pid>` + local `ps` correlation (never touching `.lock` files):
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

### 3.1 Expected Score Estimator
1. **Paired `profiler` Spectrum A/B**: Extracts device `median_us` across all repo telemetry formats (`MS_PROF`, `KPERF_JSON`, `KPERF`, `KERNEL_JSON`, `KERNEL_PERF`, `PERF`, `PROF_SUMMARY`, `dev_us`, plus compute-bound `exec >= 150 us`, rejecting `<150 us` Python host-enqueue noise). Over common cases $\mathcal{C}$, individual speedups $k_c = t_{\text{base},c}/t_{\text{cand},c}$ are clamped to $[0.25, 3.5]$ to prevent single-shape outliers (`Multilabel 197e082` `4.3x` on 2 tail bands with 11 anchors at `1.0x`) from inflating whole-spectrum expectations:
   $$k_{\text{geo}} = \exp\!\left(\frac{1}{|\mathcal{C}|}\sum_{c \in \mathcal{C}} \ln \text{clamp}(k_c, 0.25, 3.5)\right), \quad k_{\text{eff}} = 1 + \min\!\left(1, \frac{|\mathcal{C}|}{8}\right)(k_{\text{geo}} - 1) - 0.03\,N_{\text{reg}}$$
   Projected via smooth per-case saturation $r'(k) = \min\!\left(0.98, \frac{r \cdot k}{1 + 0.35\,r\max(0, k-1)}\right)$ separately on $S_{p,\text{vis}}$ ($0.4\times$ sensitivity due to dispatch floor) and $S_{p,\text{hid}}$.
2. **Same-SHA Restore (Empirical Bayes)**: Given $m$ prior `Pass` observations $\{s_1, \dots, s_m\}$ of `<SHA>`, estimates $\hat{S} = \bar{s} + \max(-1.20, -0.08 \cdot \min(\Delta t_{\text{hrs}}, 12))$ with $\sigma_{\text{conf}} = 2.70\sqrt{1 + 1/m}$.

### 3.2 The 4-Stage Submission Gate & Token-Bucket Reserve

| Stage | Name | Pass Predicate | Failure Tag / Action |
|---|---|---|---|
| **S1** | `SAFETY` | `result.json` `exit_code == 0`, 40-char SHA git tree verified, `FAST_LANE != 1`, `EXTENDED_PASSED` present, zero `EXTENDED_FAILED`/`VALIDATION_FAILED`/`STRESS_CASE_FAIL`/`Wrong Answer:`, all `mismatched=0/N`, `GATE_DET identical=True`. Kernel commits touching `SetAtomicAdd`/`SyncAll` with no prior online `Pass` **must** carry `STRESS_DONE failures=0` or `DETERMINISTIC`. | `REJECTED_GATE` (never enqueued; even `override approve` sdkot bypass S1) |
| **S2** | `NOISE_FLOOR` | $\Delta_{\text{expected}} \ge +2.50\text{ pts}$ ($\approx 0.93\,\sigma_{\text{jitter}}$), **OR** $S_{p,\text{curr}} = 0$ / `status != "Pass"` (`CRITICAL_RESTORE`), **OR** explicit `force=True` with $\Delta_{\text{expected}} \ge 0$. | `HOLD_LOW_GAIN` (parked in `held_items` to preserve daily quota) |
| **S3** | `CIRCUIT_BREAKER` | Rolling $12\text{h}$ window has $< 6$ consecutive non-improving shots since peak (resets on any new high, e.g., `ConcatD 899a2dc` `52.87 > 52.36`) and $< 2$ consecutive zeroing errors (`WA`/`TLE`/`RE`). `CRITICAL_RESTORE` passes via `HALF_OPEN`. | `CIRCUIT_OPEN` (`Switch Baseline` required) |
| **S4** | `QUOTA_RESERVE` | Dual Token Bucket: ① Rate bucket ($1\text{ token} / 190\text{s}$ $\to$ `RATE_WAIT`); ② Beijing-day (`UTC+8` `00:00` reset) 50-shot budget reserves $N_{\text{reserve}} = \max(5, |\{p : \text{gap}_p \ge 2.5 \lor S_p = 0\}|)$ slots for closing restores and zero-recoveries. Approved queue drains by `(CRITICAL_RESTORE desc, d_p * delta_expected desc)`. | `HOLD_LOW_GAIN` (if inside closing reserve) or `RATE_WAIT` |

---

## 4. Causal Retrospective Validation on Historical Submissions (`retrospect`)

Replaying all **54 chronological records** (`43` live platform submissions, `7` skipped, `4` rejected) in `results/submit-queue-outcome.jsonl` strictly causally (zero lookahead — at index $i$, only records $1 \dots i-1$ are visible):

| Metric | Historical Un-Gated | With 4-Stage Gate | Improvement |
|---|---:|---:|---|
| Live submissions consumed | 43 | **22** | **21 slots saved (`48.8%`)** |
| Catastrophic zeroings (`WA`/`TLE`/`RE` $\to 0$) | 5 | **1*** | **4 / 5 zeroings prevented (`80%`)** |
| Board score regressions (`Pass` $< S_{\text{prev}}$) | 11 | **2** | **9 / 11 regressions prevented (`81.8%`)** |
| Genuine breakthroughs held ($\Delta_{\text{actual}} \ge +2.5$ pts) | — | **0** | **100% recall on real gains (`19` approved)** |
| Hit 50/day `daily_limit` blocking `CTCLossV2 02c2048` (`+5.76` pts)? | **Yes** (rejected ×2, delayed 15h) | **No** (`~29/50` used) | **`+11.52` weighted pts landed same day** |
| Score Estimator accuracy across 26 `Pass` transitions | — | **MAE `2.96` / RMSE `3.65` pts** | Near physical $\sigma_{\text{jitter}} = 2.70\text{ pts}$ floor |

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

# Split-phase NPU validation entry point for `opsb test` (Q0 --fast <=180s vs Q1 full)
python3 -m scheduler.cli_bridge schedule-test --title PadV3Grad [--fast] [--dry-run]

# Causal historical replay & accuracy verification
python3 -m scheduler.cli_bridge retrospect --workspace /Users/winbeau/Competitions/ops-bench
```
