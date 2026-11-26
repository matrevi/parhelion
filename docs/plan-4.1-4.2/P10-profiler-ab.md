# P10 profiler 任务时长与 A/B 交替配对（4.1 路线 #1 #2）

分支：`impl/P10-profiler-ab`。在 P01 合入后的 `main` 上开发：用例映射用来对比判题时间。整个过程需要真机，开工前向主会话确认设备空闲，没有其他子代理在跑测试。

## 背景

4.0 已知的限制：事件计时带有主机侧启动延迟。同一 SHA 的小内核，在不同运行之间、甚至同一次运行的不同用例之间，读数会在约 5µs 和约 25µs 之间跳档；10µs 以上的内核与官方相差约 2%。4.0 的应对是：
- 取该 SHA 的最好成绩；
- 扣除标定出的开销，约 2.4µs，见 `scheduler/case_bench.py` 的 `timing_offset`；
- 标记慢档运行。

真正的修复是 profiler 任务时长（`docs/v4/RELEASE-NOTES.md`「已知限制」与「4.1 路线」）。

相关代码：
- `runtime/npu_runtime/fast_profiler.py`：手动工具，程序名与追踪开关取自 `OPSB_DEVICE_PROFILER_BIN` / `OPSB_DEVICE_PROFILER_ACL_FLAG`；
- `runtime/npu-test.py`；
- `runtime/npu_runtime/case_batch.py`：`run_series` / `execute_batch`；
- `runtime/npu_runtime/device_broker.py`；
- `runtime/npu_runtime/arena_*`。

## #1 profiler 任务时长接入 reference 引擎

1. 在设备作业内，对计时执行启用 profiler 采集，只记录内核任务时长。持锁期间只采集，导出与解析推迟到释放锁之后（Phase C），沿用 `fast_profiler.py` 的低锁思路。
2. 报告里每个用例同时给出 `profiler_us` 与原有的 `event_us`，`timing_source` 标明以哪个为准。
   - profiler 可用且解析成功时，计时以 `profiler_us` 为准；
   - 否则回落到事件计时，并标记 `timing_source=event`。
3. 下游消费方继续读 `min_us` / `median_us`，由 `timing_source` 决定来源，不得把两种来源混在同一配对里：`estimator._case_bench_minima` 的配对要求两侧 `timing_source` 相同。`suite_version` 或报告 schema 视需要升版。
4. 开销验收：对比开启 profiler 前后的持锁时长与端到端时长，profiler 不能让持锁时长翻倍。超出时改成只在 standard/final 级别启用，smoke 级别保持事件计时。

## #2 A/B 交替配对与多轮轮转

1. 新增配对运行模式：同一设备作业内，候选与基线 SHA 交替执行（ABAB…），用例按轮转顺序穿插，让每个用例的样本分散到作业里的不同时刻。
   - 入口：`parhelion_test(level=..., compare=<base_sha>)`，或由平台在门禁需要配对证据时发起。worker 仍不能传 repeats、timing 等参数，轮数由平台按套件决定。
   - 两个 SHA 的产物都在 CPU 侧准备好，只排一次设备队。
2. 报告输出逐例配对比 `ratio` 及其置信区间（配对样本的 bootstrap 或对数差的 t 区间），这些数据进入 `scheduler/case_bench.py` 与估分器。存在配对运行时，门禁 S2 优先使用它，其次才是分别运行的最小值配对。

## 真机验收

在设备空闲时，对 2 道题（建议 NeighborListPbcFixedK 与 EdgeIndexCoalesce，它们都有带 `time_by_case` 的回执）各做以下测量：

1. 同一 SHA 连续 3 次 standard：比较 profiler 与事件计时的逐例离散度（CV）；
2. 借 P01 的用例映射，与同 SHA 判题 `time_by_case` 的偏差（中位与最大）；
3. 一次候选对基线的 A/B 配对运行：配对比的置信区间宽度，对比分别运行时的宽度；
4. 持锁与端到端耗时。

结果写入 `docs/plan-4.1-4.2/reports/P10-timing.md`。

## 验收

- 单测：
  - profiler 输出的解析，用 fixture；
  - 回落到事件计时；
  - `timing_source` 不混用；
  - ABAB 调度顺序的确定性；
  - 配对置信区间的计算；
  - 门禁优先采用配对证据。
- 真机报告如上。
