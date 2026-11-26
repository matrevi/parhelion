状态：已交付

# P14 同伴消息协议（4.2 F）

## 1. 分支与提交

分支 `impl/P14-peer-protocol`，从 `main` `152fd62` 切出。

```
1a50f4d Add the peer envelope, attachments and merged delivery
（最后一条）Deliver P14: envelope and merged peer delivery   ← 本报告随该提交进入分支
```

提交署名 `Generated-by: DSH deepseek-flash`。

## 2. 逐项完成情况

| 子计划条目 | 状态 | 实现与证据 |
|---|---|---|
| 1. `parhelion_squad` 改为结构化信封 `{to, refs, claim≤280, evidence, ask?}` | 已完成 | 新模块 `cli/opsb_core/agents/peer.py`：`envelope()` 校验 claim 长度、refs 形态（`L-0001` / `worker/L-0001`）、evidence 长度与有无正文；`mcp/schemas.py` 增加 `to/claim/refs/evidence/ask`；`mcp/handlers_agents.squad_body`（handler 与 daemon 路由共用）与 `daemon/agent_routes` 同步透传。`peer.py` 337 行（≤350）。 |
| 1b. 旧 `message` 参数保留一版 | 已完成 | `message` ≤280 字直接作 claim；更长时按正文处理（>1200 字转附件）。原来的 `SQUAD_MAX_CHARS=4000` 截断删除。测试 `test_legacy_message_short_is_the_claim_and_long_becomes_an_attachment`。 |
| 2. 正文 >1200 字自动转附件 | 已完成 | 落到 `<workspace state>/peer/<msg_id>.md`（含 from/to/refs/evidence/claim 头），消息只带 claim 与路径；claim 取前 280 字；发送方响应里带 `hint` 提醒下次写 claim；**不拒收**。附件全文与消息长度均在测试里断言。 |
| 3. 去重：重复引用段剥掉（逐行 shingle Jaccard ≥0.8） | 已完成 | 逐行 3-shingle 集合的 Jaccard；命中则清空 detail、`deduped=true`、只保留 claim+refs；已投递引文记录在 `<workspace state>/peer/quotes/<recipient>.jsonl`（上限 256KB/500 行，超出裁剪）。测试 `test_repeated_quote_is_stripped_while_refs_survive`。 |
| 4. 收件方只收到「来自谁、refs、claim、evidence 路径」 | 已完成 | 投递块为 `[peer <id>] from=… refs=… evidence=…` + `claim:`（+ `ask:`、`text:`、`full:`）；收件方账本写一条 `type=insight, source=peer, confidence=unverified`，`links=refs`，`why` 引用发送方 message id 与 sender。测试 `test_recipient_ledger_gets_a_peer_unverified_row`。 |
| 5. 回合边界合并投递；管理员指令与判题不合并 | 已完成 | `peer.queue()` 入队；runner 回合结束时 `on_turn_end` → `manager._flush_peer()` 把队列合成**一条** bus 行 + **一次**唤醒；空闲收件方由 0.4s 合并窗（`PEER_COALESCE_SEC`）覆盖。管理员指令（`directive`/`followup`）与判题反馈（`judge_result`）仍走原来的 `append_message`+`_enqueue_to_worker`，立即投递。测试 `test_peer_messages_merge_at_the_turn_boundary_into_one_hub_event`（3 条 → 1 行、1 个 hub inbox 事件、1 次唤醒）与 `test_admin_directive_and_judge_feedback_do_not_join_the_peer_batch`。 |
| 6. `result` 消息同样受 claim/附件约束，全文落盘，写账本 `decision`/`insight` | 已完成（见下方取舍） | runner 的回合回复与 `worker_report(kind="result")` 都走 `peer.result_*`：≤1200 字正文原样，>1200 字只留 claim+路径；全文一律写 `<state>/peer/<msg_id>.md`。`worker_report` 的结果另写一条账本 `decision`（`evidence=[附件路径]`，`sha` 为当前 SHA）。测试 `test_stage_result_is_persisted_with_a_claim_and_a_ledger_decision`、`test_a_long_turn_reply_is_attached_by_the_runner`。 |

### 6 的一处取舍

轮次自动回复（runner 的 `result` 行）落盘并走 claim/附件规则，但**不**另写账本 `decision`；只有 worker 显式
`parhelion_agent_report(kind=result)`（`worker_report`）的阶段结果写 `decision`。理由：轮次回复每回合一条（本场
370 条），逐条写 `decision` 会把账本变成回合日志，和"worker 的结论"语义冲突；显式阶段结果才是结论。子计划说
"`result` 消息（worker 阶段结果 `worker_report` 给总线的版本）"，我把落盘规则同时覆盖到 runner 的 result 行（那
是 723KB 的主要来源），账本只落在 `worker_report` 路径。若审查要求两条都写，改动 3 行。

## 3. 测试

两条全量命令的末行输出：

```
696 passed, 19 skipped, 2 subtests passed in 73.23s (0:01:13)
```
（`~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider`；基线 686 passed / 19 skipped，+10 为本包新增用例。）

```
452 passed in 59.41s (0:01:01)
```
（`PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests`；基线 442 passed。）

说明：`runtime/tests/test_device_broker.py` 的两条预算测试
（`test_job_budget_turns_remaining_cases_into_tle`、`test_case_budget_limits_each_execution_not_the_repeats`）在机器
高负载时偶发失败（已知问题，与本包无关）。本包验证中两处各失败过一次，单独重跑 `-k budget` 两条均通过；上面的
末行是两条全量命令各自单独运行（无并发负载）的结果。

新增/更新的用例：

- 新文件 `cli/tests/test_peer_protocol.py`，10 个用例：信封校验与 `to` 别名、旧 `message` 兼容、1200 字附件、
  去重剥离、收件方账本、回合边界合并（含"只产生一个 hub 事件"）、空闲收件方合并唤醒、管理员指令与判题不合并、
  `result` 落盘与账本 `decision`、runner 长回复附件、MCP handler 与 daemon 路由的信封透传。
- `cli/tests/test_orchestration_v4.py` 的 squad 用例按新协议更新：5000 字消息从"截断到 4000"改为"落盘附件 +
  claim"，并断言附件里有全文。
- 工具计数不变（未新增工具）：`cli/tests/test_agent_mesh_http.py` 与 `cli/tests/test_mcp_server.py` 的 26 个工具断言未改。

没有改 Swift，未运行 `swift build`（本包无 Swift 改动）。

## 4. 回放验证

数据源：本场工作区的 `state/agent_bus.db`，按 `cli/opsb_core/replay/sources.py` 的
`sqlite3.connect("file:<db>?mode=ro", uri=True)` 只读打开；只做聚合统计，未写入任何行，报告不含消息正文、
账号或用户 ID。

口径：逐条按新协议渲染（`peer.block` / `peer.batch_body` 的同一段代码），`result` 超过 1200 字用
`[result <id>] claim: … full: <path>`；peer 按收件方的下一个回合边界（该 agent 的下一条 `result` 行）分批。

当前快照（整库）与计划基线的差异：计划写 peer 198 条 371KB、result 317 条 723KB；本库现有 peer 219 条
421,774 B、result 370 条 875,174 B。前 198 条 peer 为 381,412 B、前 317 条 result 为 748,613 B，与计划口径
接近（差的 21 条 peer、53 条 result 是计划测量之后继续写入的）。

| 口径 | 现状 | 新协议 | 变化 |
|---|---:|---:|---:|
| peer 全量 219 条 | 421,774 B | 130,935 B（135 次投递） | −69% |
| peer 前 198 条（对齐计划基线） | 381,412 B | 117,518 B | −69% |
| result 全量 370 条 | 875,174 B | 164,812 B | −81% |
| result 前 317 条（对齐计划基线） | 748,613 B | 141,691 B | −81% |
| 单条最长 peer | 4,058 B | 1,533 B | |
| 单条最长 result | 7,046 B | 1,208 B | |

- **规模分布**：peer 219 条中 33 条 281–1200 字、186 条 >1200 字（没有 ≤280 字的短消息）；result 370 条中
  354 条 >1200 字、16 条以内联正文发送。收益主要来自"长正文转附件、只传 claim+路径"。
- **合并投递**：219 条 peer 在回合边界合并成 135 次投递（1/2/3/4/5/6/7 条的分组分别为 93/23/10/2/2/3/2），
  即少 84 次唤醒；但每批多一行约 80 B 的批头，字节上合并比逐条（127,953 B）多约 2.3KB。**合并的收益是唤醒
  次数与 `parhelion_wait` 的返回，不是字节。**
- **去重**：本场 281–1200 字的 33 条正文里没有 Jaccard ≥0.8 的重复（长正文都走附件，不参与正文去重），所以
  该规则在本场回放里省 0 字节；它的作用场景是"同一段结论被反复引用"，由单测覆盖。
- **附件体积**（不在总线、不进上下文）：按当前快照估算 peer 附件约 0.38MB、result 全文约 0.86MB，落在
  `<workspace state>/peer/` 下。

## 5. NOTE（非显然约束与源码事实）

1. **`confidence=unverified` 原本不合法。** 子计划要求收件方账本写 `source=peer, confidence=unverified`，
   但源码 `ledger/model.py` 的 `CONFIDENCE` 只有 `measured/inferred/speculative`，直接写会抛 `LedgerError`。
   本包在 `model.CONFIDENCE` 增加第四档 `unverified`（语义：同伴给的、本条 worker 未自测的 claim），并在
   docstring 说明；`parhelion_note` 的模型可写档位仍是 `inferred/speculative`，模型不能自称 unverified。
   主会话若要维护 aoci 条目，这一处需要写进 `ledger/model.py` 的语义。
2. **信封字段 `to` 与源码参数 `recipient`。** 源码（`manager.squad`、handler、GUI 路由、提示词）一直用
   `recipient`；为不破坏既有调用，保留 `recipient`，`to` 作为别名（`recipient` 优先），schema 里写明。
3. **`parhelion_squad` 的返回值变了。** 投递推迟到回合边界，发送时还没有总线行，所以不再返回 `seq`/`seqs`；
   现在返回 `queued`、`msg_id`、`delivery="next turn boundary"`、`duplicate`、`recipient`/`recipients`、
   `attachment`、`ledger_id`、`hint`。仓库内的提示词没有依赖 `seq`；仓库外若有驱动解析 `seq` 需同步。
4. **`request_id` 去重的位置变了。** 原来靠 bus 的 `event_key`（立即插入即去重），现在队列先落盘，去重索引
   在 `<workspace state>/peer/index.jsonl`（`peer:{sender}:{recipient}:{request_id}`），投递行本身用
   `peer-batch:<agent>:<首条 msg_id>` 作 `event_key`。重试同一 `request_id` 仍然只投递一次（有测试）。
5. **`mcp/tools.py` 无需改动。** 工具名、handler 分发、允许角色（worker）都没变，只是参数增加；参数经
   `squad_body` 逐字同名透传到 `manager.squad`，daemon 路由复用同一个 `squad_body`，因此三处（schema /
   handler / 角色）依旧一致。工具计数仍是 26。
6. **`manager.py` 只加接入点。** 新增的是：`squad` 委派、runner 的 `busy`/`on_turn_end`、`_flush_peer` /
   `_peer_poke` / `_peer_timeout`、`worker_report` 的 result 分支、`stop_all` 取消定时器（约 80 行）。
   信封、附件、去重、队列、账本、result 渲染全部在 `peer.py`（337 行）。
7. **合并投递"只产生一个 hub 事件"的机制**：`event_sources` 的 `inbox` 事件来自逐条扫描 bus 行，一次
   `append_message` = 一个事件；批次只写一行，所以只唤醒一次。判题反馈与管理员指令不走这个队列。
8. **压缩种子的原始尾部会变短。** `ledger/seed.recent_rounds` 读 bus 的 `result` 行，P14 之后读到的是
   claim+路径而不是全文；这正是 4.2 F 的目标（长 result 不再撑爆上下文），附件路径在消息里可回读。
9. **轮次自动回复不写账本**：见 §2 的取舍。

## 6. 已知风险与建议后续

1. **回合很长的收件方**：busy 时只在回合边界投递（按设计），一个很长的回合会让消息等很久；`parhelion_wait`
   仍能看到 `inbox` 的变化（批次行产生后才算），所以这不影响"变化驱动等待"。空闲收件方有 0.4s 合并窗，单条
   消息的投递最多推迟 0.4s。
2. **`peer/` 目录没有保留策略。** P13 的 spill 有 7 天/200MB 上限，peer 附件与 quotes/index 没有；本场估算
   peer+result 附件合计约 1.2MB，短期无碍。建议随 P13 的 `opsb doctor clean` 列表一起加清理（默认只列不删）。
3. **停止的 worker 收到的批次**：`_flush_peer` 仍写 bus 行（消息不丢、可回读），但 `_enqueue_to_worker` 不会
   唤醒已停止/出错的 worker；这条消息留在它的 inbox，不会转给别的 worker。
4. **`worker_report(kind=result)` 的附件取全文**（不再截断 4000 字），超大摘要会写大文件；建议和 §6.2 的
   上限一起处理。
5. **合并投递的批次身份**：`event_key=peer-batch:<agent>:<首条 msg_id>`。同一批次不会重复插入；但如果进程在
   `take_pending` 之后、`append_message` 之前崩溃，这批消息只留在队列外（`take_pending` 已截断文件）。极端
   情况会丢一条 peer 消息；账本行在入队时已写，结论不会丢。若要把这条也做到"绝不丢"，可在 append 成功后再
   截断队列（当前为了一次 flock 换简单性）。
6. **P08/P15 衔接**：`brief.py` 的 squad 一行已按新协议改写（claim/refs/evidence/附件/合并边界），
   `parhelion_squad` 的 schema 描述也写明了这些；P08 重做启动包时保留这段即可。

## 7. 复现

```bash
# 全量测试
~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests

# 回放估算（只读；脚本按报告 §4 的口径拼接 peer.block/peer.batch_body）
PARHELION_HOME=$HOME/.parhelion PYTHONPATH=cli:. ~/.parhelion/venv/bin/python <估算脚本>
```
