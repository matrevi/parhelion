# P07 门禁与队列一致性、逃逸锁的 MCP 入口（4.1 #9 #10 #11 #12）

分支：`impl/P07-gate-queue`。在 P01、P02 合入后的 `main` 上开发。

## #9 队列视图直接使用门禁结论

现在 `services/queue_service.py` 的 `eval_gates` 仍按「增益 ≥ 底线」画四盏灯，而真实的 S2 已改为期望提升：E[max(0, 增益)] ≥ 底线，且 P(增益>0) ≥ 0.2（`scheduler/gate.py`）。

改法：灯与理由只读门禁的实际判定。门禁判定由 `SubmissionGate` 产出，经 `scheduler/cli_bridge.py` 合入队列卡片，见 `cli/opsb_core/ipc_bridge.py` 的 `merge_bridge_enrichment`。删掉队列侧的重复判定逻辑；没有门禁结论时显示「未评估」，不要自己猜。

额度不得出现在任何灯或理由里。

## #10 `opsb queue approve` 的输出自相矛盾

现在写入覆盖并入队后，仍打印 `enqueued: false / Already queued`。查 `commands/queue_cmd.py` 与 `cli_bridge` 的 approve 路径，让输出如实反映状态：

- 覆盖已写入；
- 本次是否新入队；
- 之前是否已经在队列中，在的话给出它的状态。

单测覆盖三种情形。不要改 approve 的语义：只绕过 S3，S1 逃逸锁仍需复核试验，以 force 入队。

## #11 合规拒绝卡片只显示判题消息

拒绝回执的 `detail` 里混有原始 Python 堆栈。在入库处（`services/submit_runner.py`、`services/sync_reconciler.py`）把判题返回的消息与本地异常分开存放：

- `judge_message`：给人看；
- `local_error`：完整堆栈写入日志文件，记录里只保留最后一行。

Swift 卡片 `Features/NpuMonitor/SubmissionQueueCardViews.swift` 只显示 `judge_message`，没有时显示 `local_error` 的末行。历史记录用读时归一兼容。

## #12 逃逸锁的 MCP 入口

本场主控为了改绑，手工重命名过绑定文件。新增 MCP 工具：

- `parhelion_escape_status(problem)`：列出该题所有逃逸锁的状态，含 P02 的待复现、已复现待修复、已修复三态，以及复核试验的绑定与是否已用，并给出下一步；
- `parhelion_escape_rebind(problem, receipt, sha)`：调用现有的「绑定的修复未提交、未排队时允许改绑」规则（`scheduler/case_feedback.py` 的 `authorize_review_trial`）。不满足条件时返回原因，不改文件；
- `parhelion_escape_reproducer(problem, receipt)`：返回 P02 为该回执生成的变体，以及跑复现的确切 `parhelion_test` 参数。

权限：worker 只能操作自己负责的题，主控可以操作全部。worker 身份由 `_caller_agent_id` 与总线记录判定。三个工具在 schema、handler 与角色三处同步登记。

队列卡片：ESCAPE LOCK 卡片的理由后附「下一步」。这是 bridge 新增的 `next_step` 字段，Swift 用 `decodeIfPresent`。

## 调研 R12：每题保留 top-K 候选

依据 `docs/v4.2/research/agent-optimization-practice.md` R12。每题保留逐例记录最好的 3 个候选 SHA（按门禁预测分排序，相互之间要有差异）。死路实验不能挤掉在位候选。

`parhelion_report(view=queue)` 与队列卡片显示这份候选档案。默认仍是每题一个 worker 串行改进。

## 验收

- 单测：
  - #9 队列灯与门禁判定逐项一致，参数化覆盖 S1–S4 各种结论；
  - #10 的三种输出；
  - #11 的消息分离与 Swift 回退；
  - #12 三个工具的成功与拒绝路径、角色限制，以及改绑前后的文件状态。
- `swift build` 通过。
- 用 `opsb queue list --json` 对真实比赛做只读检查：灯与 `gate` 判定一致，没有任何「额度」字样。结果写进报告。
