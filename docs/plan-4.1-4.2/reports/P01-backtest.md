# P01 回测与判题对齐报告（4.1 #5 #7 #8）

数据：`mat-chem-sim-pred` 截至 2026-11-26 05:11 UTC 的回执日志（108 行，77 个判过的提交）、`results/<题>/<sha>/bench-runs/*/report.json`、`results/scoring-evidence/live-board-tbest.json`。回测脚本只读，不发网络请求。

## 1. 结论

- **按位置配对没有配错题号，配错的是工作量。** 判题 roster 与模板 `CASES` 的顺序一致：判题不计时的用例正好落在本地的空输入用例上，FFT 三个长度 135 的一维用例在两边的同一位置上耗时相同。0.58–110 倍的比值来自两类原因：小用例的启动开销和计时跳档（3 倍以内）；个别位置上模板参数与判题数据不是同一规模（FFT 05、04，Mesh 04，CG 14，SphHarm 13/15，Edge 03/14）。
- **计分公式已核实。** 用 live board 的逐例时间和 tbest，按 `100/(1+ln(t/tbest)/ln1.5)` 对全部用例取均值（判题不计时的用例记 0），14 题的在榜分全部精确复现。`QuantScaleCalib/per_case_ledger.md` 写的线性公式不成立（按它算 Quant 是 13.40，在榜分是 15.75）。
- **新预测更准。** 留一回测（n=15，4 题）：MAE 1.32，偏差 −0.22。旧投影（n=9）：MAE 2.75，偏差 +2.51。两者都能预测的 8 条回执上，旧投影 MAE 2.55、偏差 +2.27，新方法 MAE 1.46、偏差 −0.24。
- **局限在数据上。** 4.1 之前，榜上 SHA 的完整逐例时间没有入库，因此严格按时间回放时，门禁实际可用的基线 SHA 只有 2 条回执用得上。从本包起，每条回执都会写入 `case_results`，这一限制会随新回执消失。

## 2. 两边用例的来源（#5.1）

| | 本地 official | 判题 |
|---|---|---|
| 定义 | 题目模板 `reference.py` 的 `CASES`（`case_bench.catalog` 用 AST 解析；`suite.json` 只在其上叠加 `local/*` 变体）。`reference_cases.py` 按 `params` 与 `seed` 生成输入。 | problem detail 的 `rule.testcases`：只有 roster 顺序和 ID。ranking 的 `testcases`：`_id`、序号 `ID`、`tbest`、`type`、`hidden`。没有任何形状信息。 |
| 逐例结果 | `report.json` 的 `cases[].min_us`（热态最小值） | submission detail 的 `result[]`，按 roster 顺序排列：`testcase_id`、`testcase_status`、`time`、`best_time` |
| 磁盘上能拿到的 | 全部 | `timing_score.tbest_by_case`（字典顺序即 ranking 顺序）；live board 的 `tbest` 与 `our_public_times_us`（roster 顺序，但去掉了 tbest 为空的用例）；`public_times_us`（前 5 个**有计时的**用例） |

佐证位置一致的证据：

- Edge 本地 11（K=0，空表）对应判题第 11 位，该位 tbest 和 time 都是空值。CG 本地 04（1.6µs，空输入）对应判题第 4 位，同样为空。
- FFT 本地 03/14/15（Nx=135 一维）对应判题 03/14/15，都约 167µs，本地三者都约 155µs。
- NL 本地 15 跑 92.6ms，判题 98.5ms（0.94 倍）。Mesh 08、15 分别为 0.99 倍、0.96 倍。同一种设备上，同一工作量的读数相差不大。

还发现了位置问题：`public_times_us` 跳过不计时的用例。CG 第 4 位不计时，所以它的 5 个公开时间实际是第 1、2、3、5、6 位。旧的 `zip(sorted(official), public_times_us)` 从第 4 例起就错开了一位。

## 3. `device_us` 的计时范围与 FFT 05（#5.2）

`reference_cases.py` 改写模板 `main.asc` 时，只在 `run_kernel(...)` 前后各插一个 `aclrtRecordEvent`，结束事件后才同步 stream。输入的 H2D 在此之前完成（`Group` 构造时读文件并拷贝），输出的 D2H 和写文件在此之后（`group.save()`）。所以 `device_us` 包含 `run_kernel` 入队的全部设备工作（可能有多次 launch 和候选代码自己发起的设备端拷贝或清零），加上启动开销。**不含**文件读写，也不含输入输出的 H2D/D2H。

FFT 05 在本地是 8×256³ 的 complex64，输入输出各 1 GiB。19e0086 用 profiler 测得 7.688s（任务时长，见 `SCORE_HISTORY.md`），与事件计时的 7.4s 一致，说明这是真实的核计算，不是 I/O。同一个 SHA a1b3d5d，本地 2.78s，判题 8.46ms，差 329 倍。判题这一位的 tbest 是 24µs，即使只把 2 GiB 读写一遍，也要约 90 TB/s 的带宽，物理上不可能。结论：**判题第 5 例是一个小得多的输入**，模板参数并不是判题数据。FFT 04 同理，本地 96×135×243 跑 55ms，判题 4.1ms，差 17 倍。

## 4. 各题映射（`opsb bench case-map <题> --rebuild` 的结果）

| 题 | 方法 | 置信 | 判题/本地 | 可计时配对 | 说明 |
|---|---|---|---:|---:|---|
| NeighborListPbcFixedK | roster | high | 15/15 | 15 | 4 个 SHA 佐证 |
| MeshEdgeMessagePassing | roster | high | 15/15 | 14 | 04：本地是判题的 102 倍（6.2ms），视为不同工作量 |
| SphericalHarmonicsYlm | roster | medium | 15/15 | 13 | 13（14 倍，540µs）、15（14 倍，180µs）为不同工作量 |
| RadialBasisBatch | roster | structural | 15/15 | 15 | 有回执但没有正式运行，尚无计时佐证 |
| CGTensorProduct | roster | high | 15/15 | 13 | 04 判题不计时；14 是 9 倍（63ms），只有 2 个 SHA，未达到同步变化的门槛 |
| RevINNorm、BucketPackMask | roster | medium | 15/15 | 15 | |
| PatchEmbedOverlap | roster | structural | 15/15 | 15 | |
| FFT3DBatchComplex | roster | high | 15/15 | 13 | **05（329 倍）**、04（17 倍）为不同工作量 |
| EdgeIndexCoalesce | roster | high | 15/15 | 12 | 05、06 为不同工作量；11 判题不计时。02/03/04/13/14/15 本地慢 4–111 倍，但在 3–7 个 SHA 上与判题同步变化（log 比值的标准差 ≤0.082），予以保留 |
| SlidingWindowGather、BodySliceExtract | unmapped | — | 15/15 | 0 | live board 少 1–2 个不计时用例，又没有回执给出 ID，无法确定它们在 roster 中的位置；第一条规范回执到达后会自动成为 roster |
| SgemmFp16Cube、QuantScaleCalib | unmapped | — | 19/15 | 0 | 判题比本地多 4 例；assignment 方法至少要 2 个有完整时间的 SHA，目前每题只有榜上 1 个 |

判定规则：判题耗时 ≥50µs 的用例上，启动开销可以忽略，此时本地与判题的比值超出 [1/4, 4] 即为 `scale_mismatch`。只有在 ≥3 个 SHA 上 log 比值的标准差 ≤0.1 时，才保留为 `judge_mapped`。标记为 `judge_mapped=false` 的本地用例不参与计时偏移和分数预测，正确性检查照常进行。

**设备时间建议（未改套件）**：不同工作量的用例照样占设备。FFT 05 占 standard 持锁时间的 56%（中位 34.7s，3 次运行），CG 14 占 50%（7.8s，41 次），Mesh 04 占 38%。建议把它们移出 standard 的计时，只在 final 里跑一次做正确性检查（不计时）。FFT、CG 每次 standard 大约能省一半设备时间。是否修改套件由主会话决定。

## 5. 回执归一与回填（#8）

`opsb bench receipts backfill` 只读磁盘：

- 77 个判过的提交（108 行）：19 行可以补全，追加为 `source="receipt-backfill"` 行。其中 7 行补的是 `timing_score` 里已有的分数和逐例时间，12 行补的是 live board 上当前在榜提交的逐例时间与 tbest。
- 补不全的照实列出：58 个缺逐例时间；20 个 WA/RE/TLE 全部不知道失败在哪一例；2 行缺 SHA，回执文件里也没有；1 个 Pass 没有分数。
- `--apply` 后日志从 108 行变为 127 行，前 108 行逐字节不变。再执行一次追加 0 行。
- 用当前运行的守护进程代码（主检出）重放：补全前后策略事件集合相同（19 个），`receipt:` 消息键没有新增，因此不会重复投递，也不会触发交接。

## 6. 回测（#7）

### 方法

- **新方法** `receipt_per_case`：`t_c[j] = T_b[j] / r[j]`，`r = (L_b−off)/(L_c−off)`。`off` 取 <50µs 用例上（本地−判题）的下四分位数。b 的单次慢档读数（本地/判题比值超过其典型值 3 倍）按判题时间还原。`r` 先截断到 [0.1, 10]，再按 `T_b/(T_b+10µs)` 的权重在对数尺度上收缩。没有映射的判题用例沿用 `T_b`。
- **留一（LOO）**：去掉候选 SHA 自己的全部回执行，重建映射、选基线和 tbest。基线依次取当时的榜上 SHA，再取时间最近的、有完整逐例时间的 SHA。tbest 用候选自己回执观测到的值（同代），没有时用时间最近的观测。
- **因果**：一切数据截至提交时刻，基线可以是任意已判 SHA。
- **旧投影**：平台在提交时刻自己缓存下来的 `.case-bench-residuals.json`，未经校准。

### 结果

| 方法 | n | 覆盖题 | MAE | 偏差 | RMSE |
|---|---:|---|---:|---:|---:|
| 旧投影（`case_bench_paired`，未校准） | 9 | CG、Edge、Mesh、NL | 2.75 | +2.51 | 3.51 |
| 新方法 LOO | 15 | CG、Edge、Mesh、NL | **1.32** | −0.22 | 1.64 |
| 同 8 条回执：旧投影 | 8 | | 2.55 | +2.27 | 3.38 |
| 同 8 条回执：新方法 LOO | 8 | | **1.46** | −0.24 | 1.86 |
| 新方法，因果，任意基线 | 6 | CG、Edge | 2.58 | −2.58 | 2.93 |

旧投影两次高估 NeighborList（28.35 对 24.86，29.80 对 24.74），新方法 LOO 分别给出 22.85 与 25.92。因果回放偏低，因为当时只有一个弱基线（Edge 16f86a6，8.40）有完整时间：从弱基线外推，没有映射的用例被留在了慢的时间上。门禁只接受榜上 SHA 或历史最佳作为基线（`allow_other=False`）；按这个规则因果回放，只有 2 条回执可用，残差为 CG 8565cf2 +2.10、Edge 749ee25 +0.78。

### 灵敏度（LOO，n=15）

| 计时噪声 `NOISE_US` | 0 | 3 | **10** | 30 |
|---|---:|---:|---:|---:|
| MAE / 偏差 | 1.91 / −1.48 | 1.48 / −0.80 | 1.32 / −0.22 | 1.32 / +0.22 |

| 比值截断 | [0.25, 4] | **[0.1, 10]** | [0.05, 20] |
|---|---:|---:|---:|
| MAE | 1.35 | 1.32 | 1.23 |

不收缩时偏差为 −1.5：微秒级用例上，候选的本地慢档读数被当成了退化。样本只有 4 题 15 条，参数取了中间值，不宜再细调。真正的修复是 profiler 任务时长（P10）。

### 接入门禁后的现状（在比赛数据的副本上运行，未写真实目录）

当前未判的候选会走新方法，基线复现在榜分时分毫不差。Edge dc9254e：14.98（在榜 15.01）。CG a614e64：23.51（23.98）。NL 644aaf9：20.83（24.59；旧门禁给出 −12.86，同样暂缓）。FFT 0ab7bd8：14.00（13.95）。Quant、Sgemm 未映射，仍走旧投影。在副本上运行 `status`：0.70s 增加到 0.99s。

## 7. 真实数据命令

均从工作树的 `cli/opsb` 运行，`--workspace` 指向比赛目录：

- `opsb bench case-map <题> --rebuild` × 14 题：共 5s，写出 14 个 `results/<题>/.case-map.json`。回填后再运行一次，`evidence.source` 从 live-board 变为 receipt，配对不变。
- `opsb bench receipts backfill`：0.4s，列出 19 行。
- `opsb bench receipts backfill --apply`：0.4s，追加 19 行；再执行一次追加 0 行。

**过失说明**：06:56 UTC，一个分析脚本通过 `predict()`，按当时的默认行为调用了 `case_map.current()`，在比赛目录写出了 12 个 `.case-map.json`。这是批准命令之外的写入。这些文件在 07:03–07:04 被批准的 `--rebuild` 全部重写。回执日志、残差缓存和 `.timing-offset.json` 没有被这次写入改动。`predict()` 已改为默认不写，只有门禁的实时评估会刷新映射。

## 8. 与子计划不一致的地方

- 子计划假设"映射错位"。实际是位置正确、工作量不同，所以映射记录的是 `scale_mismatch` 与 `judge_mapped`，不重排位置。
- 子计划里 r 的公式是"扣除已标定偏移后求比"。这里另外做了两件事：只还原 b 的慢档读数，并对微秒级用例收缩比值。偏移改用下四分位数：Edge 本地普遍慢 3–4 倍，用中位数会得到 76µs 的"偏移"。
- tbest 取最近一次观测，并在回执里记录 `tbest_observed_at`。
- 回填之后，回放按文件顺序取"最后一行"当榜上 SHA，会出错，所以去重时让回填行占据原行的位置。估分器在 `as_of` 回放时，`active_board_sha` 原本取的是最后一条已判回执，现在改用刷新过榜的回执。
- 熔断器原来把"Pass 但未刷新榜"计为归零失败，现在只计失败的判定（`status != Pass`）。

## 9. 风险

- 合入后，`deliver_judgements` 会把"未刷新榜的 Pass"计入每 6 发的窗口：这是 AGENTS.md §6 的口径，6 发指实际消耗额度的判题，而这类 Pass 同样耗额。重放模拟显示，这会新产生 **1 个** EdgeIndexCoalesce 的 stagnation 事件（第 749ee25 那一发），这是对历史窗口的追溯。合入前需要主会话决定：接受这个事件，还是先写入对应的 event_key。
- 守护进程在重启前仍运行旧代码。旧代码在 `as_of` 回放时会把回填行当作最新的回执，只影响历史卡片上的估分显示。旧代码的状态卡"last receipts"会显示重复的行。worker 目前都已停止。
- Quant、Sgemm（19 对 15）以及 SWG、Body 暂时没有映射，仍用旧投影。
