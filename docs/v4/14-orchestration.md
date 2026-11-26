# 14 · 编排与 token 经济（工作包 C）

> 索引：[10-DETAILED-INDEX.md](10-DETAILED-INDEX.md) · 分支：`v4/c-orchestration`

## 1. 要解决的问题

- `start_sprint` 每题固定开 3 个席位（algorithm、dataflow、calibration）。14 道题就是 42 个常驻 worker。其中校准席位消耗了约三分之一 token，产出的实验大多卡在排队里。
- 压缩条件是 `context_percent ≥ 60` 并且 worker 已经报告过阶段结果。DSH 报告的窗口是 100 万 token，所以 60% 是 60 万 token，实际永远不会触发。worker 上下文平均峰值约 22 万 token。
- 供应商余额耗尽后，每个 worker 各自报错，平台继续向它们派发 followup，产生了 265 次失败和 149 条无效 followup。
- `parhelion_squad` 只接受 agent_id，而提示词写的是"发给 optimizer"，出现 122 次报错。
- brief 最多 2950 字节，且没有一张由平台维护的、简短的题目状态卡；worker 只能自己反复读文件拼出现状，工具输出里 43% 是读文件。

## 2. 改动范围

修改：`cli/opsb_core/agents/manager.py`、`agents/sprint.py`、`agents/adapters.py`、`agents/bus_store.py`（如果需要）、`mcp/handlers_agents.py`、`drivers/brief.py`、`services/templates_md.py`、`daemon/server.py`（只增加一次 `deliver_test_results` 调用）。
新建：`services/state_card.py`、`cli/tests/test_orchestration_v4.py`。
补充测试：`cli/tests/test_agent_mesh_mcp.py`。

## 3. 冲刺模式（`sprint.py`）

- `start_sprint(..., mode="solo")`，`mode` 的取值：
  - `solo`：每题 1 个席位，`axis="solo"`，`track="sprint-solo"`，`role_label="worker"`；
  - `dual`：`algorithm` 和 `dataflow` 两个席位（现有的任务描述）；
  - `full`：再加 `calibration`（`role_label="bench-author"`，现有行为）。
- `solo` 的任务描述（英文，≤400 字符）：`"Own this problem end to end. Pick one hypothesis from the state card, change one variable, run parhelion_test level=standard, compare against the baseline in the digest, and hand a candidate to parhelion_submit_gate only when the digest shows a measured gain. Record negative results with parhelion_agent_report."`
- `DIRECTIONS` 增加 `solo`。`sprint_seats()` 识别 `sprint-solo` 席位。复用已有席位的逻辑不变。
- `handlers_agents.tool_parhelion_sprint_start` 读取 `args.get("mode", "solo")` 并校验，然后传给 `start_sprint`。

## 4. 供应商故障熔断（`manager.py`）

1. 常量 `PROVIDER_FAULT = re.compile(r"insufficient balance|insufficient_quota|quota exceeded|billing|payment required|\b402\b|\b401\b|unauthori[sz]ed|invalid api key|rate limit|\b429\b", re.I)`。
2. `_WorkerRunner` 捕获异常时，如果错误文本匹配，就调用 `manager.suspend_driver(ws, driver, reason)`；worker 本身仍记为 `error`，错误文本保持不变。runner 需要能访问 manager 的回调：构造时传入 `on_fault: Callable[[str, str], None]`，参数是 driver 和错误文本。
3. `suspend_driver`：
   - 把 `{driver: {"since": iso, "reason": ≤300 字符}}` 写入 `resolve_workspace_state_dir(ws)/driver_suspensions.json`（原子写）；
   - 只追加一条 strategic 消息：`kind="error"`、`direction="worker_to_orch"`、`priority="strategic"`、`event_key=f"suspend:{driver}:{since[:13]}"`，正文写"驱动 {driver} 因供应商故障暂停：{reason}。恢复后用 parhelion_sprint_start 或 parhelion_agent_spawn 重新派发。"；
   - 对该驱动所有处于 `idle` / `working` 状态的 runner 调用 `stop()`，并把它们的状态记为 `error`，错误文本为 `"suspended: provider fault"`。
4. `driver_suspended(ws, driver) -> bool`。`spawn_agent` 和 `start_sprint` 在派发前检查：
   - 管理员显式调用（`parhelion_sprint_start` / `parhelion_agent_spawn` 不带 `_sprint` 内部标记）时，先清除暂停记录，再派发，视为管理员确认已经恢复；
   - 内部自动派发遇到暂停中的驱动时，直接跳过，结果记为 `blocked`，原因 `driver suspended`。
5. `_enqueue_to_worker`：目标 worker 的状态是 `error` 或 `stopped`，或者它的驱动处于暂停状态时，不入队，返回 `{"ok": False, "reason": "..."}`。调用方（`send_message`、`squad`、`deliver_judgements`）照常把消息写入总线，但不唤醒 worker。

## 5. 按 token 数压缩（`adapters.py` + `manager.py`）

1. `emit_context(used, size, callback)` 在回调 `("context", percent)` 之后，再回调一次 `("context_tokens", str(int(used)))`。
2. `_WorkerRunner._on_tool_event` 处理 `context_tokens`：记录 `self._context_tokens`。不需要写入数据库，`context_percent` 的写法不变。
3. 阈值按模型窗口自适应：
   - 默认 `clamp(0.3 × 窗口, 120000, 400000)`。窗口取 adapter 最近一次上报的 `size`（ACP 的 `usage_update.size`，Codex 的 `modelContextWindow`）；没有上报时按 200000 计算。
   - 结果：100 万窗口的模型为 30 万 token；20 万窗口的模型为 12 万 token（相当于窗口的 60%）。
   - 配置 `agents.compact_tokens` 可以覆盖默认值，限制在 [80000, 600000]。
   - 理由：算子优化要同时装下 2000 行级的内核源码、测试摘要和若干轮实验结论，12 万 token 对 100 万窗口的模型来说太早。按日志，worker 的上下文峰值约 22 万 token，所以 30 万的阈值主要是防止无限增长，正常迭代不会频繁触发。token 浪费的主因是并发过多、重试和轮询，已经由 §3、§4 和工作包 A 解决。
   - 为此，`emit_context` 还要回调一次 `("context_window", str(int(size)))`，runner 记录到 `self._context_window`。
4. 每个 turn 结束、并且已经写入 result 消息后，满足以下任一条件就压缩：
   - `self._context_tokens ≥ 阈值`（硬上限）；
   - `context_percent ≥ 70`。

   4.2 C #7（P06）起，低于硬上限时还要有边界事件，并且达到阈值的 40% 下限才压缩；细节见 §12。
   不再要求必须先有 `_stage_result`。
5. 压缩的检查点文本（4.2 起，`ledger/checkpoint.py`）按顺序拼接：`state_card.build(ws, problem, agent_id)`，`_stage_result` 的**结尾**（没有就取 reply 的最后 3000 字符；超出预算时丢开头，标 `…`），加上一句 `"Continue from the state card. Do not re-read files you already summarized."`。总长度不超过 `agents.seed_budget_chars`（默认 8000，限制在 [3000, 20000]）。`worker_report` 登记的阶段结果超过 4000 字符时同样保留结尾。ACP 压缩还会让模型按固定五个标题写不超过 1500 字的交接叙述，再由 `ledger/handoff.py` 对照账本校验后拼进新会话的种子；Codex 在原生压缩后的下一回合前置同一份账本摘要。校验与回退规则见 `docs/plan-4.1-4.2/P05-ledger-core.md`。
6. 压缩后把 `_context_tokens` 清零，状态写入方式不变（`compacting` → `idle`）。

## 6. 状态卡（`services/state_card.py`）

`build(ws, title, agent_id=None) -> str`，UTF-8 不超过 2000 字节。英文纯文本，固定以下几段，没有数据的段写 `n/a`：

```
STATE {title} @ {utc HH:MM}
best: {score} {sha7} ({submission_id[:8]}) | head: {sha7} {subject≤60}
last receipts: {status score sha7}; … (最近 3 条)
public gap (ours/tbest µs): 01 9.4/8.8 · 02 … (最近一条带 public_times_us 的回执，最多 5 例)
last tests: {compact_line}; …   (test_runs 中该题最近 3 条；有 agent_id 时只取该 worker 的)
suite: v{suite_version} · timed {n} · levels smoke/standard/final
rules: device work only via parhelion_test; submit via parhelion_submit_gate; no ssh/flock.
```

- 数据来源：
  - `results/submit-queue-outcome.jsonl`：best 取该题得分最高的 Pass 记录；last receipts 取该题最近 3 条记录；
  - 题目仓库的 HEAD：`services/git_fast.read_head_fast`；
  - `services.test_runs`：读取 test_runs 目录下的记录（用 A 暴露的 `compact_line`）；A 尚未合入时 try/except 跳过这一段；
  - `scheduler.suite.load_suite`：B1 尚未合入时 try/except 跳过，这一段写 `n/a`。
- 任何异常都不得向上抛出。卡片生成失败时返回一行 `STATE {title}: unavailable`。

## 7. brief 与指南（`brief.py`、`templates_md.py`）

- `MAX_BRIEF_BYTES` 改为 1500。`compose_session_brief` 只保留：
  - 角色和目标一句话；
  - 工作树与分支；
  - 工具使用顺序：`parhelion_test` → `parhelion_test_wait` → 比较 digest → `parhelion_submit_gate`；
  - squad 寻址规则一句话；
  - 最后附上状态卡（状态卡不计入 1500 字节）。
- KB 摘要和快速反馈的内容从 brief 里拿掉，改为一句"需要时调用 parhelion_kb_query（默认返回 3 条摘要）"。
- `compose_bench_author_brief` 同样精简，并说明校准用 `level=final`，套件修改通过 orchestrator 执行 `opsb bench suite add-variant`。
- `templates_md.py` 里的英文指南（`WORKSPACE_AGENTS_MD`、`PROBLEM_AGENTS_MD`）：
  - 把 `parhelion_test_plan` 换成 `parhelion_test` / `parhelion_test_wait`；
  - 说明三个级别；
  - 删除"自定义 profile"相关的说法；
  - 明确禁止 `ssh` / `flock` / 直接访问设备。
  - 保留现有的比赛策略、冻结 oracle、实测历史等段落（AGENTS.md 第 8 条的要求）。

## 8. squad 寻址（`manager.squad`）

1. `recipient` 的解析顺序：
   1. 精确等于某个队友的 agent_id；
   2. `all`：投递给全部活跃队友；
   3. 不区分大小写地等于队友的 `axis`、`role_label`，或 `"sprint-" + axis`。

   恰好匹配一个就投递。匹配多个，或者一个都没匹配上，抛出 `ValueError`，消息里附上队友列表（`agent_id:axis:role_label`）。
2. 消息超过 4000 字符时不再拒绝，而是截断到 4000 字符，并在末尾加 `…[truncated]`。
3. `all` 返回 `{"ok": True, "recipients": [...], "seqs": [...]}`。单个收件人时返回形状不变。

## 9. 测试结果投递（`manager.deliver_test_results` + `daemon/server.py`）

- `deliver_test_results(ws)`：对 `test_runs.list_undelivered(ws)` 返回的每一条记录：
  - 追加一条总线消息：`agent_id=record.agent_id`、`direction="orch_to_worker"`、`kind="test_result"`、正文为 `compact_line(record)`、`event_key=f"test:{request_id}"`、`problem=record.problem`；
  - 如果 `record.observed` 为 false，并且该 worker 当前是 `idle`，调用 `_enqueue_to_worker` 唤醒它，正文同上，再加一句 `"Full digest: parhelion_test_wait request_id=…"`；
  - 调用 `mark_delivered`。
- `daemon/server.py` 里调用 `deliver_judgements` 的地方，紧接着调用 `deliver_test_results`，异常吞掉并记日志，频率与 `deliver_judgements` 相同。
- A 尚未合入时，`import` 失败就直接返回。

## 10. 其他

- `deliver_judgements` 给 worker 投递时遵守 §4.5 的规则（不唤醒 error / stopped 状态的 worker）。
- 删除对 `bench-author` 的硬编码依赖：没有 bench-author 时，校准回执只投给提交该 SHA 的 worker（现有逻辑已经这样做，确认没有回归即可）。

## 11. 验收测试（`cli/tests/test_orchestration_v4.py`）

用现有测试里的假 adapter 工厂和临时工作区。

1. `start_sprint(mode="solo")` 为 14 道题各开 1 个席位；`dual` 开 2 个；`full` 开 3 个。
2. 一个 worker 抛出 `"Insufficient Balance"` 后：同驱动的其他 worker 全部停止；只产生 1 条 strategic 消息；之后的 `send_message` 不会唤醒这些 worker。
3. 管理员显式 `spawn_agent` 会清除暂停记录。
4. 窗口 100 万时，`context_tokens` 到 29 万不压缩、到 30 万压缩；窗口 20 万时，到 12 万压缩；配置覆盖值生效且被限制在 [80000, 600000]。压缩时检查点里包含 `STATE` 行。
5. squad 按 `dataflow` 投递成功；按 `all` 投递给两个队友；按不存在的名字投递时，报错里包含队友列表。
6. 状态卡不超过 2000 字节，账本为空时也能生成。
7. `deliver_test_results`：observed 为 false 且 worker 空闲时唤醒；observed 为 true 时只写消息；同一个 request_id 不重复投递。
8. `compose_session_brief` 不含状态卡的部分不超过 1500 字节，且包含 `parhelion_test_wait`。

## 12. 4.2 C #5 #7 与 4.1 #13 #14（P06）

### 12.1 压缩时机（`agents/compact_window.py` + `manager.py`）

`should_compact(tokens, window, boundary, cfg, tool_in_flight, percent, threshold) -> (bool, reason)`：

1. 有工具调用在跑（`tool_in_flight`）→ 不压缩。
2. `tokens ≥ compact_threshold` 或 `percent ≥ 70` → 压缩（硬上限，理由 `hard_cap`）。
3. `tokens < 阈值 × agents.compact_floor_percent%`（默认 40，限制 [10, 100]）→ 不压缩（`below_floor`）。
4. 否则只在一个边界上压缩：`test_terminal`（本 worker 的测试运行到终态，`deliver_test_results` 记）、
   `judged`（判题回执到达，`deliver_judgements` 记）、`stage_boundary`（`parhelion_agent_report` 的
   result/breakthrough/stagnation/escape_zero）。边界只消费一次。

`_WorkerRunner.note_boundary(kind)` 记边界，`_should_compact()` 决定并留下 `compact_reason`。

### 12.2 压缩种子（`ledger/seed.py`，开关 `agents.seed_scheme`）

`legacy`（4.2 之前：叙述 + 检查点）、`ledger_core`（P05：叙述 + 账本摘要 + 检查点，默认）、
`ledger_full`（本包：由代码按顺序拼装，预算 `agents.seed_budget_chars`，目标 6–10K 字符）：

1. `RULES`：比赛 `AGENTS.md` 的规则段、管理员 directive、平台 `RULES_LINE`，原文照录；同时以
   `constraint` 条目按文本哈希幂等钉住（`ledger.seed.pin`）。
2. `STATE`：扩展状态卡（§12.3）。
3. `LEDGER`：`ledger/digest.py` 的封顶摘要——在研假设按优先级、未利用洞见、最近 15–20 条失败方向；
   16–20 条以后只留 id（R4），失败列表先被截。open 假设与未利用洞见的 id 永不丢。
4. `HANDOFF`：模型叙述，≤1500 字，五个固定标题校验（P05）。
5. `RAW TAIL`：最近几轮回复原文（`agents.seed_tail_turns`，默认 2 轮、1600 字符）+ `git status`、
   `git diff --stat`、在改文件路径。
6. 行动指令（替换 `CHECKPOINT_TAIL`）：取最高优先级的假设或洞见并说出 id，重推之前先 `parhelion_recall`。

预算不足时的顺序：先丢原始尾部，再退到只有 id 的摘要，再缩叙述；固定规则与 open id 不截。
默认值按 `docs/plan-4.1-4.2/reports/P06-replay.md`：`ledger_full` 与 `current` 打平，默认保持 `ledger_core`。

### 12.3 状态卡扩展版（`services/state_card_ext.py`）

`state_card.build(ws, title, agent_id, extended=True)` → `EXT {title} @ HH:MM | worktree <绝对路径>`：
逐例相对同代基线的加速比（`estimator._case_bench_pair` 的 `cases`）与置信度、逐例 `(ours−tbest)/tbest`
与 tbest 观测时间（`scheduler.receipt_predict`，没有映射时取最新回执）、最近 3 条回执（SHA/状态/官方分/
是否刷新最高分）、以及 R8 的「权重 × 差距」前 3 个用例与数值目标。上限 8000 字节，规则行仍在最后。
`parhelion_status(detail=true)` 对主控返回同一份数据的紧凑表（`case_detail`，最多 3 道题）。

### 12.4 账本工具（`mcp/handlers_ledger.py`）

- `parhelion_note(type, claim, why?, scope?, sha?, evidence?, confidence?, revisit_if?, supersedes?, ...)`：
  写入一条模型条目并返回 id；平台字段（`outcome`、`per_case_us`、`receipt_id` 等）被拒绝而不是忽略；
  `confidence=measured` 降为 `inferred`；状态更新是带 `supersedes` 的新行（`id` 也读作更新目标）。
- `parhelion_ledger(view=open|unexploited|failures|case:<id>|since:<id>, problem?, worker?, limit?)`：
  每条一行（id、类型、claim、关键数字）。worker 只能读自己题目的账本。
- `parhelion_recall(query, limit?)`：账本、`state/handoffs/*.jsonl` 与该 worker 的会话流水的倒排索引，
  结果带 `文件#行`；索引缓存在 `<workspace state>/recall/`，日志目录只读。
- `parhelion_submit_gate` 新增 `hypothesis_id`、`predicted_effect`；缺省时沿用平台自动生成的假设并在
  `ledger.note` 里说明生成了哪个 id；id 无法解析也不拒绝提交。
- `parhelion_test` 新增 `force_rerun`、`reason`：同 SHA、同级别、同套件版本已有正式运行时直接返回该结果
  与账本 id（不排设备）；候选 diff 与最近实验近似（哈希相同或规范化 diff 重叠 ≥ 0.90）时返回最相近实验
  与 id，要求先说明差异。`remeasure=true` 视同 `force_rerun`（旧参数）。
- R5/R8：`ledger/budget.py` 的失败指纹（用例集 × 瓶颈类别 × diff 类别 × 结果）、3 次尝试预算与连续 5 次
  无提升的平台期，作为 `warnings` 附在测试与门禁返回里，只警告不阻断。
