# P24 逃逸锁出口与 S1 体检

分支：`impl/P24-escape-health`。在 P21 合入后的 `main` 上开发。与 P23 都改 `scheduler/gate.py`：P24 改 S1，P23 改 S2 与意图。后合入的一方先 `git rebase main`。

## 为什么

**判题判决不是源码的属性。** 上一场有 4 个 SHA 在源码一字未改的情况下，被判出两种判决类别：
- 2 例 Pass→TLE；
- 1 例 TLE→Pass；
- 1 例 Pass→WA→Pass。

当时的原话是 `restore after a single WA on the same source (platform flake?)`。

4.2.1 已经让 TLE 不再锁题。剩下两个缺口：

1. **WA/RE 的锁没有出口。** 复现变体在坏 SHA 上全部通过，也就是没能复现时，`scheduler/wa_repro.status` 停在 `pending`，锁永远不解。
2. **S1 不做机械体检。** 7 大线上事故里，有 3 条可以从机械指标提前看出来（R3 §2.3）：
   - 模板实例化太多，编译加评测撞穿判题时限，导致 TLE；
   - 内核体积暴涨，冷启动吃亏，线上反跌；
   - 冷启动双峰被本地 warmup 掩盖。

   当前 S1 不读这些数字。`runtime/npu-test.py` 已经记录了 `phase_a_cpu_sec` 与 `template_instantiations`，`cli/opsb_core/fast_feedback/` 也有 `elf_guard` 与 `cold3_probe`，但 `scheduler/` 一次都没引用它们。

这一条的稳健内核是**防 TLE**，不是「体积小了就涨分」。同一题把实例化从 81 剪到 33 之后，线上分反而低了 2.28（00-SUMMARY §4.5）。所以除了判题时限风险，其余指标只提示，不暂缓。

## 范围

1. **未复现的出口**
   - `wa_repro.status` 增加 `unreproduced` 状态：坏 SHA 在 level final 的正式运行中，全部复现变体都通过，且这样的运行至少有 `unrepro_runs` 次（默认 2）。如果运行记录能区分会话复用，其中至少一次要关闭会话复用。
   - `next_step` 写明：「N 次未复现，按判题侧噪声处理，锁降级为提示」。
2. **门禁的处理**
   - 对**其他 SHA**：`unreproduced` 时 S1 通过，阶段码 `ESCAPE_UNREPRODUCED`，status_tag 为 EXPLORE，优先级减半，原因里写明坏 SHA 与回执。
   - 对**坏 SHA 本身**：与 TLE 规则一致，在它线上 Pass 之前不能原样重投。
   - 开关 `escape_unreproduced_releases`，默认开。
3. **判题噪声事件**
   - 未复现与同源判决翻转都写一条平台账本行，类型为 measurement，带 `judge_noise` 标记，按 `submission_id` 幂等。
   - 如果 P22 已合入，rules 视图里显示本题的翻转次数。
4. **S1 体检**
   - 新模块 `scheduler/health.py`，提供 `check(workspace, title, sha, board_sha)`。
   - 指标取自该 SHA 最近一次正式运行的结果：
     - 编译秒数；
     - 模板实例化数；
     - 内核目标文件字节数；
     - 冷启动比值（有 cold3 结果时）。
   - 有 `elf_guard` 告警时附上，并与在榜 SHA 的同名指标比较，给出增长百分比。
   - **先核实 case-bench（参考模板路径）的报告是否记录了这些字段。** 没有记录就在测试时补记：远端脚本只用标准库，见 `runtime/npu-test.py` 头部说明。数据缺失时跳过，不暂缓。
5. **体检进入门禁**
   - 体检结果写进 S1 的 metrics 与 `parhelion_submit_gate` 的回复。
   - 有警告时，优先级乘以 `health_penalty`（默认 0.8）。
   - **判题时限风险：**
     - 判据：编译秒数 × `judge_compile_factor` + 预估判题运行秒数 ≥ `health_time_margin`（默认 0.8）× 判题时限。
     - 判题时限优先取 P22 规则快照里的时限，没有就取 `config.json` 的 `judge.time_limit_sec`（默认空，即跳过）。
     - 预估判题运行秒数：用 `scheduler/receipt_predict` 的逐例预测时间乘以判题迭代次数。做不出来就只用编译项。公式写进报告。
     - exploit 时暂缓，阶段码 `JUDGE_TIME_RISK`，下一步是「减少实例化或编译量，或用 intent=probe 发」。如果 P23 还没合入，只写「或请求主控决定」。
     - 开关 `health_time_risk_holds`（默认开，见 00-PLAN D3），关掉后只提示。
   - 总开关 `health_checks`。
   - 阈值不写真实设备值。仓库里只出现中性默认值，设备相关的值放在 `config.json` 的 `device` 段或 `judge` 段。

## 复用

- `scheduler/wa_repro.py`：`status`、`next_step`、`lock_view`；
- `cli/opsb_core/mcp/handlers_escape.py`：逃逸三件套，显示新状态；
- `scheduler/calibration.py`：`inspect_escape_state`；
- `cli/opsb_core/fast_feedback/elf_guard.py`、`cold3_probe.py`；
- `cli/opsb_core/ledger/auto.py`：幂等写入。

## 测试

- `unreproduced` 的判定：次数不足、部分失败、全部通过。
- S1 对其他 SHA 放行、对坏 SHA 拒绝原样重投。
- 体检在数据缺失时跳过。
- 时限风险的暂缓与开关。
- 账本行幂等。

## 验收

- **P21 回放**：两场在改前改后的 `frozen_shots`（G3），以及阶段码分布。
- **TLE 回执逐条核对**：用 ops-bench 上 7 条 TLE 回执对应的本地结果（`results/<题>/<sha>/result.json` 里有字段的那部分），列出体检会不会标出时限风险。R3 说只有 203 次运行记录了 `template_instantiations`，缺的就写缺。

## 真机

在当前工作区的一道题上跑一次 `opsb test`（standard，只测不提交），确认新字段写进报告。设备不通时标为待补。

## 报告（`docs/v4.3/reports/P24-escape-health.md`）

- `unreproduced` 的判定口径；
- 体检字段在各报告格式里的来源；
- 时限风险公式；
- 回放与 TLE 逐条核对表；
- 真机结果。
