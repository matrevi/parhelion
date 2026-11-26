# P02 按 WA 回执生成多 seed 复现变体（4.1 #6）

分支：`impl/P02-wa-reproducers`。在 P01 合入后的 `main` 上开发，依赖 P01 的 `scheduler/receipts.py`（`case_results`）与 `scheduler/case_map.py`。

## 证据

- QuantScaleCalib 三次线上 WA（9ffca10、5041c86、a2717b5），本地每个用例都是零误差。WA 出现在 2048×2048、模式 0 这类对舍入平局敏感的形状，而本地每个用例只用一个 seed（`docs/v4/RUN-LOG-2026-11-26.md` 观察 11）。
- 逃逸锁与复核试验在 `scheduler/case_feedback.py`：
  - `authorize_review_trial` / `trial_spent`；
  - 复核放行的 SHA 再次线上失败后，下一个修复必须先在本地复现。
  
  `opsb bench verify-escape` 是现有的 CLI 入口。
- 套件变体机制：`scheduler/suite.py` 的 `normalize_variant`、`add_variant`、`manage`。用例输入由冻结的 oracle 按 params 生成，见 `runtime/npu_runtime/reference_cases.py`。oracle 冻结，不能改。

## 目标

WA/RE 回执一到，平台就自动为失败的判题用例生成一组多 seed、富含平局的本地变体，作为逃逸锁的复现入口。坏 SHA 在本地复现失败即「已复现」。修复 SHA 必须在这些变体上全部通过，才能解除该锁；现有的一次复核试验路径保留。

## 设计

1. 新模块 `scheduler/wa_repro.py`（≤350 行）：
   - `plan_variants(ws, title, receipt)`：
     - 从规范回执的 `case_results` 取出失败的判题用例；
     - 经 `case_map` 找到本地用例及其 params；未映射时取同题全部 official 用例中与失败用例时间量级最接近的 1–2 个，并标注 `mapped=false`；
     - 生成变体：
       - 同 params、多个 seed（默认 12 个，确定性派生自 `submission_id`）；
       - 以及 oracle 支持的「富含平局」输入模式。先读 oracle / 描述符支持哪些 `x_mode` 或分布参数（例如 uniform 小幅值、整数格点、重复值、恰在舍入中点的值）。只用 oracle 本身能表达的参数，表达不了就在报告里说明，不改 oracle。
   - 变体 id 形如 `wa-<sid8>-<n>`，带 `source="wa-repro"`、`receipt=<submission_id>`、`judge_testcase_id`。默认只进 `final` 级别，不改变 smoke/standard 的耗时。
   - `apply(ws, title, receipt)`：经 `suite.add_variant` 幂等写入；同一 `submission_id` 重放不重复生成。
2. 接入：回执入库的同一处（P01 的规范化入口之后），对 `Wrong Answer` / `Runtime Error` 调 `apply`。这一步只需 CPU，不排设备。
3. 逃逸锁：
   - 锁状态里列出对应变体 id，以及「下一步：在坏 SHA 上跑 final 以确认复现」；
   - 坏 SHA 的一次 final 运行在任一 `wa-repro` 变体上失败，即记录 `reproduced`，并附运行 ID；
   - 修复 SHA 在该回执的全部变体上通过（同套件版本的正式运行），即可解除该锁；
   - 复核试验路径与「复核放行后再 WA 必须本地复现」的规则不变。
   
   门禁 S1 的判断读这三种状态。卡片文案沿用 ESCAPE LOCK，理由里写清是「待复现」「已复现待修复」还是「已修复」。`ipc_bridge` 的 badge 字面量不变。
4. CLI：`opsb bench wa-repro <Problem> [--receipt SID] [--apply] [--json]`，经 `cli_bridge` 子命令，默认只列出计划。

## 验收

- 单测：
  - 变体生成的确定性与幂等；
  - 未映射时的回退；
  - 三种锁状态的迁移；
  - S1 对三种状态的判断；
  - 重放同一回执不重复写入。
- 真实数据（真机验收）：
  - 对 QuantScaleCalib 的三条 WA 回执执行 `wa-repro --apply`；
  - 用你分支的 `cli/opsb test` 在真机上对坏 SHA a2717b5 跑 final，看是否有变体失败。此时不得有其他子代理在用设备，主会话会安排。
  
  结果（复现与否、哪个变体、耗时）写入 `docs/plan-4.1-4.2/reports/P02-quant-repro.md`。复现失败也是有效结果，要写明试过的变体与下一步建议。
