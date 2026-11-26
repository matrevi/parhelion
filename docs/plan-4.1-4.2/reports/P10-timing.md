# P10 profiler 任务时长与 A/B 交替配对：真机测量

日期 2026-11-26，分支 `impl/P10-profiler-ab`。测量在 07:40–08:08 UTC 进行，设备主机 08:08 前后再次拒绝 SSH，§5 列出没做完的部分和补测命令。真实的芯片型号、工具链版本和路径只在 `~/.parhelion/config.json` 与主机上，不进仓库。

## 1. 实现要点

- **profiler 计时**（`runtime/npu_runtime/profiler_capture.py`，作为 broker 的 companion 模块随 broker 一起下发，见 `device_broker.ensure_broker(..., companions=...)`）
  - broker 在 profiler 下启动常驻 sample。真机上确认过：profiler 会把 stdin、回复描述符和环境变量原样传给应用，所以执行协议不用改。
  - 采集只开 task-time l0，不开 AI Core、AI CPU、ACL 和 runtime API。
  - 持锁期间只采集。关闭 sample 后，profiler 日志一出现导出标记（应用已退出）就释放设备锁；导出和解析在锁外的 Phase C 完成，结果以 `profile` 事件补发，`done` 等它们发完。
  - 每次执行都记录主机时钟窗口，kernel 任务按开始时间落进哪个窗口就归哪次执行。
- **报告 schema 2**：每个计时用例同时给出 `event_us` 和 `profiler_us`，`timing_source` 标明 `min_us`/`median_us` 取自哪一个。
  - 只有当 profiler 覆盖了该用例的每一次计时执行，而且每次的任务时长都不超过同次事件读数（5% + 1 µs 余量）时，才用 profiler；否则这个用例回落到 `event`。
  - 报告级的 `timing_source` 取 `profiler`、`event_warm` 或 `mixed`。
- **配对不跨来源**（`scheduler/timing.py`）：估分器的分别运行配对、逐例回执预测（`receipt_predict`）、`case_map.local_minima`、`case_bench` 的基线比较、历史最好值和 timer drift，每次比较两侧都读同一个计时器。profiler 只在覆盖全部共享用例时才被选用。
- **A/B 配对**：`opsb test --compare <base|board>`，或 `parhelion_test(compare=...)`。
  - 两个提交在同一个归档里下发（`cand/`、`base/`），oracle 必须一致，两份构建都在 CPU 侧完成，只排一次设备队。
  - 每轮按轮转顺序访问用例，A、B 背靠背执行；偶数轮先 A，奇数轮先 B，即每轮内 ABAB，下一轮 BABA（`case_pairs.schedule`）。
  - 报告里每个用例给出 `pair`：B/A 的几何均值比，以及对数差的 t 区间；另附 bootstrap 区间。
  - 配对运行记为实验（`formal: false`），放在候选 SHA 的 `bench-runs` 下，带 `compare.base_sha`。
- **门禁 S2 采用的证据次序**：分别运行的 profiler 最小值（覆盖全部共享用例时）> 配对运行 > 分别运行的事件最小值。理由见 §3。

## 2. #1 profiler 与事件计时：同一 SHA 连续 3 次 standard

每题跑 5 次 standard，profiler 开、关交替（开 3 次、关 2 次），都加 `--remeasure`。「关」是用 `OPSB_DEVICE_PROFILER_LEVELS=off` 临时覆盖的。

### NeighborListPbcFixedK 3f33362（看板 SHA）

| 指标 | profiler | 事件（同 3 次运行） | 事件（全部 5 次） |
|---|---|---|---|
| 逐例离散度：每次运行最小值的 CV，各例的中位数 | **0.9%** | 61% | 75% |
| 逐例 CV 的最大值 | 4.3% | 118% | 122% |
| 与判题 `time_by_case` 的偏差，取 5 次中的最好值（15 个映射用例，中位数） | −29.6%，全部低于判题 | 绝对值 19.2%，带符号 −5.9% | — |
| 偏差最大的用例 | 11：−54.6% | 03：+183%（事件读数进了慢档） | — |

- 事件计时在约 5 µs 和约 25 µs 两档之间来回跳，3 次运行里有 1 次落在慢档的用例很多。profiler 读数稳定，没有分档。
- profiler 比判题低，而且差额是加性的、各例不同：01 是 4.8 对 6.7 µs，05 是 3.9 对 8.5，11 是 4.1 对 8.9，大 kernel 07 是 128 对 134。判题时间更像「kernel 时长 + 每次启动约 1–5 µs」；事件计时在快档时最接近判题。

### EdgeIndexCoalesce 4f954c3（看板 SHA）

- profiler 覆盖了 15 个用例中的 14 个。用例 11 是空用例，不启动 kernel，因此按设计回落到事件计时，报告标为 `mixed`。
- 逐例 CV：profiler 中位 0.16%、最大 4.9%；事件中位 0.36%、最大 91%。本题大多是长 kernel，事件计时本身就稳。
- 与判题比较：只有 6 个映射用例没有标 `scale_mismatch`。其中好几例本地负载明显不同（例如 12 号本地 24.6 µs，判题 2.9 µs），中位偏差 profiler +143%、事件 +203%。这些数字反映的是用例映射里的负载差异，不是计时误差，不能作为计时精度证据。

### 持锁与端到端（#1 的开销验收）

| 题 | 持锁：profiler 开 | 持锁：关 | 端到端：开 | 端到端：关 |
|---|---|---|---|---|
| NeighborList | 4.03 / 4.04 / 5.03 s | 3.87 / 4.88 s | 28.2 / 28.2 / 29.3 s | 24.1 / 25.0 s |
| EdgeIndex | 10.04 / 10.04 / 10.04 s | 9.99 / 9.99 s | 55.0*（首轮重建用例缓存）/ 43.0 / 43.2 s | 39.3 / 39.0 s |

- 持锁时长基本不变（NeighborList 中位数 4.04 对 4.38 s，Edge +0.5%），**没有翻倍**。所以 profiler 按计划在 standard 和 final 级别开启（`device.profiler_levels` 默认 `standard,final`）。
- 端到端多约 4 s，就是锁外的导出与解析（`collect_sec` 约 4.0 s）。持锁内的「等 profiler 交还设备」是 2.4–3.4 s，它替代了原来关闭会话的时间。
- smoke 仍只用事件计时，把这 4 s 留给快速自检。如果要全局关闭：`device.profiler_levels: "off"`。

## 3. #2 A/B 交替配对

配对比是基线时间除以候选时间（B/A，大于 1 表示候选更快）。区间宽度定义为 hi/lo − 1，下表取各例的中位数。「分别运行」用的是每次运行的最小值，候选和基线各 3 次以上，按 Welch 区间计算。

| 题（候选 对 基线） | 配对：持锁 / 步数 / 端到端 | 配对区间宽度（事件） | 分别运行宽度（profiler） | 分别运行宽度（事件） | 与判题比值的偏差：中位 \|ln(本地/判题)\| |
|---|---|---|---|---|---|
| NeighborList d70506f 对 3f33362 | 8.3 s / 120 / 45.9 s | 152% | **5.6%** | 698% | profiler 0.134；配对 0.167；分别事件 0.697 |
| EdgeIndex 4f954c3 对 3f99cd8 | 18.6 s / 150 / 85.7 s | 3.1% | 无（见 §4） | 2.3% | 配对 0.019；分别事件 0.016（4 例） |

- **两个进程不能同时被 profiler 采集。** 第一次带 profiler 的配对运行里，只有先启动的那个 sample 拿到了任务数据。随后的直接实验（一个 profiler 会话下跑两个子进程）也是：第二个进程报 `Collect data failed`。也就是说，这台设备同一时间只为一个进程采集，而交替执行必须让两个会话同时活着。因此配对运行只用事件计时（`case_bench.run` 对 `compare` 关掉 profiler）。
- **交替执行会把事件计时推进慢档。** NeighborList 配对运行中，两臂的小 kernel 大多读在约 25 µs。所以在小 kernel 上，配对事件区间远宽于分别运行的 profiler 区间；它仍比分别运行的事件区间窄约 4.6 倍。长 kernel 上配对表现很好，例如 Edge 05 为 1.764 [1.738, 1.791]，14 为 1.013 [1.012, 1.014]，但分别运行的事件最小值也已经同样紧。
- 根据这两点，门禁 S2 的证据次序与子计划不同：profiler 完整时，分别运行的 profiler 最小值优先于配对运行；配对运行只取代分别运行的事件最小值（`estimator._case_bench_pair`，`receipt_predict`）。测试见 `scheduler/tests/test_paired_timing.py`。
- **profiler 比值不能直接等同于判题比值。** NeighborList d70506f 对 3f33362：profiler 在 04–13 号用例上一致显示快 12–22%，但同两个 SHA 的判题逐例时间几乎相等（比值 0.98–1.03，两发分数 24.74 对 24.86）。判题时间里那部分每次启动的固定开销不随 kernel 变化。回执预测（P01）对 T 小于 10 µs 的用例本来就用 `NOISE_US` 收缩，例如 3 µs 的用例权重只有 0.23。即便如此，profiler 比值仍会高估小 kernel 题的增益。建议在 P01 的回测框架里为 profiler 来源标定一个按题的判题侧加性开销，再让它参与小用例的估分。

## 4. 测量中发现并修复的问题

- **按顺序回填任务会错位。** 第一版在有执行窗口收不到任务时，会把任务数平均分给各次执行。EdgeIndex 3f99cd8 有一个不启动 kernel 的空用例（11），630 个任务恰好是 105 次执行的 6 倍，于是退回到顺序分配，后面每个用例都读到了别的用例的 kernel。这一版跑出的 3 次报告（`c922afa5…`、`fde9439e…`、`5d6de0f5…`）profiler 读数全部失真，事件读数仍然可用。修复如下：
  1. 只按时间窗口归属，删掉顺序回填；
  2. 设备侧检查：任务时长超过事件读数的用例不采用 profiler；
  3. 读取侧防护：`timing.profiler_trusted` 忽略 profiler 注记里带 `(order)` 的运行，这 3 份报告因此只按事件参与比较。
- 设备主机在 08:08 UTC 再次在密钥交换阶段重置 SSH 连接。修复后重测 3f99cd8 的 3 次运行立即失败，留下 3 份 `NOT_RUN` 报告（`3261c0bf…`、`3dfda07b…`、`9d04d36a…`，exit_code 1，不参与计时）。之后按 30 分钟一次的约束没有再连。

## 5. 待补测（主机恢复后执行）

1. EdgeIndex 3f99cd8，用修复后的归属方法跑 3 次 standard，用来补上 §3 表中 Edge 一行的「分别运行 profiler」宽度和判题偏差：
   ```bash
   cd <P10 工作树>
   for n in 1 2 3; do
     ~/.parhelion/venv/bin/python cli/opsb test EdgeIndexCoalesce --sha 3f99cd8ccf04c5abadcbbc8f6c8d10f7ea381e19 \
       --workspace /Users/winbeau/Competitions/mat-chem-sim-pred --level standard --remeasure
   done
   ```
   预期：报告的 profiler 注记形如 `N tasks, 104 of 105 executions attributed`，用例 11 为 `event`；各例 profiler 读数小于等于事件读数。
2. 可选：再跑一次 NeighborList 配对（`--sha d70506f… --compare 3f33362…`），确认交替执行下事件慢档是否稳定复现。
