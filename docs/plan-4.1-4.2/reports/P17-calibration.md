# P17 本地验证可信度、逐例校准表与噪声感知判定（调研 R1 R2 R3）

数据：`mat-chem-sim-pred` 截至 2026-11-26 09:30 UTC 的回执日志与 `results/<题>/<sha>/bench-runs/*/report.json`。回溯脚本只读，不写 `results/`，不发网络请求。真机部分是两次 `opsb test --level final`，按平台流程照常写 bench-runs。

## 1. 结论

- **计时由平台持有，缺口在模板文件。** 计时事件、同步、样本采集与输出比对都在平台运行时里完成，候选代码只提供内核。唯一的缺口是：运行器从被测提交里读取模板主程序 `main.asc` 以及 `sample.py`、`reference.py`，而判题端会换成自己的副本。新增 `scheduler/harness_check.py`：这三个文件与题库首次加入时的版本不一致时，运行照常执行以便诊断，但不再是正式证据（`formal=false`，摘要提示恢复文件）。14 个题库的全部分支上，这三个文件从未被修改过，因此这条规则不会误伤现有数据。
- **final 级别每次运行都派生未公开变体。** 种子、底例和形状都由运行 ID 确定性派生，经 `suite.extend`（与 `add_variants` 共用校验）进入当次运行的目录，标记 `origin="hidden-final"`，但不写入 `suite.json`，因此套件版本与 official 配对键都不变。真机上，已知通过的 SHA 跑了 8 个未公开变体，全部通过，没有误报。a2717b5 跑了 9 个未公开变体，也全部通过：这次新增变体没有额外暴露问题，它仍然只在 P02 的 tie 复现变体 `wa-e0f2d340-13` 上 WA。
- **物理合理性检查已接入门禁证据。** 读数低于带宽下限，或比此前同输入的最好成绩快 3 倍以上，或超过历史最大提速的 2 倍时，该读数被标为 `implausible`。在同一 SHA 的另一次运行复测、两次相差不超过 10%、且其中一次是冷启动会话之前，门禁不采用这个读数。存在待确认读数时，下一次正式运行自动以冷启动会话执行。
- **校准表的数据还很薄。** 14 题中只有 4 题有可配对的回执，共 10 条回执、49 个用例对。两题达到题级 `trusted`（NeighborListPbcFixedK、EdgeIndexCoalesce），各有 1 个 `divergent` 用例；其余用例因配对数不足都是 `noisy`。回退率：NL 0.06，其余有数据的题都是 0。

## 2. R1.1 计时归属核对

逐条核对了以下路径：

| 环节 | 位置 | 由谁持有 |
|---|---|---|
| 计时区间 | `runtime/npu_runtime/reference_cases.py` `generate()`：在模板主程序里定位内核调用与随后的 stream 同步，在两者外侧插入平台的事件计时，读数写入平台变量并经批处理通道回报 | 平台。新单测 `test_platform_events_bracket_the_kernel_launch` 核对插入顺序：开始事件、内核调用、结束事件、同步、取时长 |
| 执行与采样 | `device_broker.py`（常驻代理）、`case_batch.execute_batch`：热身样本与计时样本分开记录 | 平台 |
| 输入与期望输出 | `reference_cases.prepare_case` 在独立进程里调用冻结 oracle 生成；缓存条目只读硬链接恢复，内核写输入会失败 | 平台（oracle 文件来自被测提交，见下） |
| 比对 | `case_batch.verify` 在 CPU 上用期望输出逐次比对每个重复样本 | 平台 |
| 样本校验 | `case_bench._execute_run` 拒收非有限或非正的读数，只取热态样本 | 平台 |

**缺口**：`main.asc`、`sample.py`、`reference.py` 取自被测提交。它们决定了计时区间的位置和正确性的定义，而判题端会用自己的副本替换（`runtime/judge/project_files.py`）。worker 简报写明这些文件只读，但运行器此前没有校验。

**补上的检查**：`harness_check.protected_changes(repo, sha)` 把三个文件在被测提交中的 blob 与题库首次加入该文件的提交中的 blob 对比。只要有一个不同，`case_bench.run` 就把这次运行标为非正式（`formal=false`）并写入 `report.harness`，摘要提示恢复文件。单测：`test_changed_protected_file_is_never_formal_evidence`。

## 3. R1.2 final 级别的未公开变体

`scheduler/hidden_final.py`。每次正式 final 运行（显式用例列表与自定义实验除外）：

| 类 | 构造 | 失败是否判运行失败 |
|---|---|---|
| `seed` | 至多 4 个 official 用例换新种子（同一生成器的另一次取值） | 是：这是 oracle 自身的分布 |
| `scale` | 浮点输入乘以 3、0.01、−1。运行器先让 oracle 生成输入并做变换，再由 oracle 的 `run()` 计算期望输出（`transform` 字段） | 否：可能超出题面的输入域，只报告 |
| `shape` | 至多 2 个带显式 `shape` 的用例，把一个轴改成更小的奇数 | 否：同上 |

- 种子与所有选择都由运行 ID 确定性派生。显式值列表（`*values`）的用例不参与派生；按实测字节数（缺测时按参数乘积）跳过大用例，因为新输入每次都要在设备主机的 CPU 上生成。
- 变体不写入 `suite.json`，不改变套件版本，也不进入配对键（`pairing.official_key` 只看 official）。`catalog` 的 key 只在有 `transform` 时附加该字段，official 与历史 key 都不变。共享测量的准入键会去掉当次的未公开变体，因此相同请求仍可合并。
- oracle 自身无法构造的变体记为 `SKIP`，不算内核失败。
- 面向 worker 的摘要只写类别和底例，例如 `hidden-final:seed:07`，不出现种子、形状和数值。`report.json` 保留完整定义，便于平台复现。

## 4. R1.3 物理合理性检查

`scheduler/plausibility.py`，在每次运行结束时执行：

- 下限：`bytes / peak_bandwidth`。运行器现在把每例的输入与输出字节数写进 `report.cases[].bytes`。峰值带宽取 `config.json` `device.peak_bandwidth_gbps`，默认值为空，仓库里不写数值。本机目前没有配置这一项，所以只做倍数检查。
- 倍数：按输入 key 与此前所有可用读数比较，比最好成绩快 3 倍以上；或者该输入出现过提速，且这次的跳幅超过历史最大跳幅的 2 倍。
- 解除：同一 SHA 的另一次运行，同一输入的读数相差不超过 10%，且两次中有一次是冷启动会话（`reuse_session=false`）。
- 门禁证据：`case_map.local_minima`（逐例回执预测、用例映射）和估分器的 case-bench 最小值都会跳过未解除的读数。
- 复测：同一 SHA 存在未解除的读数时，下一次正式运行自动以冷启动会话执行。运维也可以用 `opsb test --cold` 手动触发。

## 5. R2 逐例校准表

`scheduler/case_calibration.py`，写入 `results/<题>/.calibration-pairs.jsonl`，按 `submission_id` 去重（同一 ID 以最后一行为准）。重放回执不会追加新行。用例映射的方法或配对键变化时，记录才会重算。每条记录的每个映射用例包含：

- `local`：本地预测增量，`ln(L_base/L_cand)`，扣除映射的启动开销；
- `band`：本地噪声带；
- `judge_delta`：评分端增量，`ln(T_base/T_cand)`；
- `baseline`：所用基线，即提交时的在榜 SHA，没有时取更早一个判过的 SHA。

标签规则：

- `divergent`：至少 2 次本地提升里一半以上在评分端变慢；或者至少 2 个双方都超出噪声的配对里，符号一致率低于 0.5；或者至少 4 对时秩相关为负。
- `trusted`：至少 3 个双方都超出噪声的配对，符号一致率不低于 0.75，回退率不超过 0.2。
- `noisy`：其余情况。

**门禁区间**（`scheduler/predict_interval.py`）：估分结果新增 `prediction_interval`，S2 的 metrics 里也有这一项。逐例预测时，每个判题用例按本地用例的校准偏差给出时间区间，再用判题公式对两端打分。`divergent` 用例的贡献按「未知」处理：区间从基线与预测中较慢者再慢 2 倍，到两者中较快者再快 2 倍，而不是记为 0；这些用例在 `divergent` 中列出。其他估分方法给出 ±1.28σ 区间，并按 divergent 用例的占比放宽。额度不进入区间计算。

**回溯结果**（只读计算，未写盘）：

| 题目 | 用例映射 | 带逐例时间的回执 | 配上对的回执 | 用例对 | 符号一致率 | 秩相关 | 本地提升数 | 回退率 | 题级标签 | trusted/noisy/divergent |
|---|---|---|---|---|---|---|---|---|---|---|
| BodySliceExtract | unmapped | 0 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| BucketPackMask | roster | 3 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| CGTensorProduct | roster | 6 | 1 | 5 | 1.00 | 0.40 | 2 | 0.00 | noisy | 0/5/0 |
| EdgeIndexCoalesce | roster | 8 | 6 | 20 | 0.80 | 0.12 | 5 | 0.00 | trusted | 0/9/1 (01) |
| FFT3DBatchComplex | roster | 2 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| MeshEdgeMessagePassing | roster | 4 | 1 | 4 | 1.00 | 0.80 | 1 | 0.00 | noisy | 0/4/0 |
| NeighborListPbcFixedK | roster | 5 | 2 | 20 | 0.90 | 0.79 | 18 | 0.06 | trusted | 0/14/1 (03) |
| PatchEmbedOverlap | roster | 3 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| QuantScaleCalib | unmapped | 1 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| RadialBasisBatch | roster | 4 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| RevINNorm | roster | 2 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| SgemmFp16Cube | unmapped | 2 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| SlidingWindowGather | unmapped | 0 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |
| SphericalHarmonicsYlm | roster | 4 | 0 | 0 | — | — | 0 | — | noisy | 0/0/0 |

- 没有配上对的原因只有两类：该回执之前没有判过的 SHA；或者两个 SHA 没有共同配对键下的正式、可计时运行（较早的回执大多早于平台的正式运行，或者只在旧套件下跑过）。
- **EdgeIndexCoalesce 01**（K=3 的极小用例）：本地增量在 ±2（对数）之间摆动，评分端几乎为 0，中位偏差 0.72。这是启动开销主导的计时噪声，按 `divergent` 处理是对的。
- **NeighborListPbcFixedK 03**：两次本地提升中，有一次（+0.08）在评分端变慢（−0.034）。
- 单个用例还没有达到 `trusted`，因为一个用例最多只有 2 个双方都超出噪声的配对。从本包起，门禁每次实时估分都会补写记录，表会随新回执变厚。

## 6. R3 三态判定与 noisy

`scheduler/verdicts.py`：

- 配对增量超出噪声带加余量（默认 1%）为 `improved`，反向为 `regressed`，其余为 `indistinguishable`。
- 噪声带默认取两侧热态样本的变异系数，按平方和合成。报告的 `timing_source` 不是 `event_warm` 且该例带 `interval_us` 时，改用这个区间（P10 的兼容接口）。
- 任一侧样本的变异系数超过 3% 时标 `noisy`：保留判定结果，但不计入提升或回退的声明。
- 已接入：测试摘要的 `baseline.verdicts` 与提示（例如 a2717b5 对 9af3c0d：「5 improved, 1 regressed beyond noise, 9 noisy」）、`opsb bench compare` 的逐例 `verdict` 与汇总、状态卡新增的 noisy 与校准标签一行。
- `verdicts.outcome()` 给账本 `experiment.outcome` 用，`verdicts.summarize()` 给 P12 的 `per_case` 视图用。P05 与 P12 尚未合入本分支，接口已备好，等它们合入后接线。

## 7. 真机验收

| 检查 | 命令 | 耗时 | 结果 |
|---|---|---|---|
| 已知通过的 SHA | `cli/opsb test QuantScaleCalib --sha 9af3c0dc37 --level final` | 37 s（准备 9.7 s，63 例均未命中缓存；持锁 5.9 s） | exit 0，verified；8 个未公开变体（seed×4，scale×3：×3 / ×0.01 / ×−1，shape×1：[16,3]）全部 PASS，无误报；`implausible=[]`；每例 `bytes` 已记录 |
| QuantScaleCalib a2717b5（线上 WA） | `cli/opsb test QuantScaleCalib --sha a2717b5499 --level final` | 37 s（准备 9.5 s，64 例） | exit 2：仅 P02 tie 复现变体 `wa-e0f2d340-13` WA（24/8320 个元素差 1，与 07:37 那次 final 相同）。9 个未公开变体（seed×4，scale×3，shape [61,16]、[1447,4]）全部 PASS，**没有额外暴露问题** |

两次运行都没有命中设备缓存。原因不在本包：缓存键里的 oracle 摘要覆盖了提交中所有非内核文件，不同 SHA 的文档文件不同，所以缓存不能跨 SHA 共享。

## 8. 与子计划或源码事实不一致之处

- 子计划写的是「与 P02 共用 `suite.add_variants` 机制」。但 `add_variants` 会写 `suite.json` 并升级套件版本，而种子由运行 ID 派生，每次都不同，照搬会让套件无限增长。因此把它拆成 `suite.extend`（只校验与合并，不写盘）和写盘两步，未公开变体只用前者。
- 子计划没有规定未公开变体失败后是否判运行失败。本包规定：`seed` 类判失败；`scale` 和 `shape` 类只报告，因为可能超出题面输入域，而 worker 又看不到输入，误报的代价很高。
- 校准记录使用两个 SHA 的全部正式运行，而不只用提交前的运行。代码在回执之后不会改变；如果只用提交前的运行，现有数据只能配上 2 条回执。记录里的 `late_runs` 给出候选 SHA 在回执之后的运行数。门禁按时点回放时，标签可能用到回放时点之后的本地运行，但预测分本身不受影响。

## 9. 风险与后续

- 本机没有 numpy，`scale` 变换只在设备主机上验证过（真机 3 次全部通过）。对应单测在本机跳过。
- `scale` 变换依赖 oracle 的 `run()` 通过 `build_case` 取输入。如果某题的 `run()` 另行构造输入，期望输出就会与变换后的输入不一致。这类失败只会作为非阻断项报告。
- worker 只要能直接读 `results/**/report.json`，就能看到未公开变体的完整定义；平台只在摘要和 MCP 输出中隐藏它们。
- `peak_bandwidth_gbps` 尚未配置，带宽下限暂不生效。
- 校准表目前很薄：区间对 `divergent` 的放宽方式（固定 2 倍）应在回执增多后再标定一次。
