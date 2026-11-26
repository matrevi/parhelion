# P03 测量：缓存命中、工具字节与回放基准（4.2 A）

分支：`impl/P03-metering-replay`。4.2 其余各项都用本包的数字验收，所以口径必须与 `docs/v4.2/00-PLAN.md` §1 一致。

## 数据来源（主会话已核实）

- **ACP 会话流水**：`~/.parhelion/logs/sessions/mesh-dee-<id>.jsonl`，每行 `{ts, dir: in|out, msg: <JSON-RPC>}`。
  - `session/update` 的 `sessionUpdate` 有五种：`tool_call`、`tool_call_update`、`usage_update`（只有 `used`/`size`，没有缓存字段）、`agent_thought_chunk`、`agent_message_chunk`。
  - `session/new` 的结果带 `sessionId`。
  - 压缩时平台发出的 prompt 以 "Write a short handoff" 开头（`cli/opsb_core/agents/adapters.py:236`），随后新开会话。
- **DSH 原生会话**：`~/.dsh/sessions/<project_key>/<sessionId>/session.v4.jsonl.zstd`，zstd 压缩的 JSONL。
  - 路径与解码见 `drivers/dsh_driver.py`：`_dsh_project_key`、`_zstd_read`，路径函数在 :119。
  - `assistant/message` 事件的 `data.usage = {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, totalTokens}`。实例：3947 + 422 + 7936 = 12305，可见 `inputTokens` 是未命中的输入。
  - 另有 `request/header`、`request/context`、`system/message`、`tool/call`、`tool/result`、`step/*`、`turn/*`、`agent/inbox/spliced` 等事件；`data.message.source.model` 是模型名。
- **Codex app-server** 与 **Claude stream-json** 的 token 用量字段，由你从 `agents/adapters.py`、`drivers/codex_appserver.py`、`drivers/claude_driver.py` 与实际流水中确认（Codex 有 cached input tokens，Claude 有 `cache_read_input_tokens`）。
- **MCP 唯一出口**：`cli/opsb_core/mcp/server.py` `handle_jsonrpc_message` 的 `tools/call` 分支。stdio 与 HTTP 都经过这里。

## 1. 每次模型调用的缓存命中（A1、A2）

1. 新包 `cli/opsb_core/metrics/`（每个模块 ≤350 行）：
   - `usage.py`：按驱动解析出规范记录 `{agent_id, driver, model, ts, input_miss, cache_read, cache_write, output, call_seq}`。
     - DSH：由 mesh 流水里的 `sessionId` 定位原生会话；
     - Codex / Claude：从各自事件取；
     - 取不到的驱动标 `unavailable`，不要猜。
   - `shape.py`：参照 Reasonix 的 `CaptureShape`/`CompareShape`（见 `docs/v4.2/research/token-economy.md`）。为每次调用记录系统提示、工具表、消息前缀的指纹；对每次未命中标出原因：`cold_start`、`system_changed`、`tools_changed`、`compaction_or_truncation`（前缀不再延续）、`ttl_gap`（距上次调用超过可配置阈值）、`unknown`。只用日志里实际存在的内容；DSH 的 `request/*`、`system/message` 能给出多少，就做到多少，并在报告里说明各驱动能判定到哪一级。
   - `store.py`：增量解析，缓存游标放在 `<workspace state>/metrics/`。只读源日志，绝不改写。
2. 汇总：按 agent（主控与 worker）给出命中率 `cache_read / (cache_read + input_miss)`、各类 token 总量与最近一次未命中原因。
3. 展示：
   - `AgentMeshManager.build_hot_mesh` 为每个 worker 与主控增加可选字段 `cache_hit_pct`、`input_miss_tokens`、`cache_read_tokens`、`last_miss_reason`。
   - Swift：`Domain/AgentMeshModels.swift` 用 `decodeIfPresent` 解码；`Features/AgentMesh/AgentMeshNodeCards.swift` 的外部 worker 卡与检查器显示命中率。热路径预算是 1Hz，解析放后台线程并缓存，不要在构建 hot mesh 时解压日志。

## 2. MCP 工具字节与频率（A3）

- 在 `handle_jsonrpc_message` 的 `tools/call` 处记录 `{ts, agent_id, role, tool, args_bytes, result_bytes, ms, is_error}`，追加到 `<workspace state>/metrics/mcp-calls.jsonl`。
  - 用 `config.append_jsonl_locked`；超过 8MB 轮转。
  - 记录失败绝不影响工具调用本身。
- 汇总每个工具每小时的调用数、平均与 p95 返回字节。

## 3. 冲刺指标口径（§1 与 §3 的表）

`cli/opsb_core/replay/sprint_metrics.py`：从会话流水重算下列指标，给出现状基线：
- worker 上下文峰值（中位、最高）与压缩次数；
- 起步到第一次平台调用的上下文；
- 以 `cd` 开头的 shell 调用次数与返回字节，按 `sed`/`git`/`python3`/`grep`/`ls`/`cat` 分列；
- 临时 Python 脚本数，其中读 JSON、解析 `report.json` 的各多少；
- 绕过 MCP 的 CLI 调用（`opsb queue list`、`opsb history`、`opsb bench verify-escape`）；
- 主控的 `agent_wait`、`test_wait`、`agent_send`、`status` 调用数；
- 总线消息按类型的条数与字节；
- 压缩后重读压缩前文件的比例；
- 压缩后重测同 SHA 与级别的次数。

数字应能大致复现 `00-PLAN.md` §1。对不上时，以你的口径为准，写明差异与原因。

## 4. 压缩回放基准（A4，§5.1）

新包 `cli/opsb_core/replay/`：
1. `loader.py`：读 mesh 流水（必要时结合 DSH 原生会话），重建「轮次 → 工具调用 → 结果」，在每个压缩点切开。
2. `probes.py`：从切点之后的会话确定性地生成探针（不调用 LLM）：
   - 切点后提出的想法，切点前是否已经提过；
   - 切点前已证伪（测试变慢、WA、无增益）的方向，切点后是否又被重试；
   - 切点后读的文件，切点前是否读过；
   - 切点后测试的 SHA 与级别，切点前是否测过。
   
   每个探针带要点（SHA 前缀、用例 id、方向关键词）和来源位置。
3. `seeds.py`：种子构建器注册表，至少包含：
   - `current`：复现 `manager._checkpoint` 加 handoff 的拼法，handoff 原文取自日志；
   - `current_plus_ledger`：用切点前的平台事件（测试结果、回执、提交说明）重建的简易账本摘要；
   - `full_history`：切点前的完整记录，作为上限。
   
   给 P06 留注册接口：`register("full_c", builder)`。
4. `score.py`：一个探针的要点出现在种子里即算覆盖。按 worker 汇总覆盖率，并对每个 worker 做 bootstrap 置信区间（重采样探针）。可选的 LLM 评审模式默认关闭，不调用任何 API。
5. CLI：
   - `opsb replay bench [--sessions GLOB] [--schemes a,b] [--json]`；
   - `opsb replay metrics [--sessions GLOB] [--json]`。
   
   两者都只读日志。

## 5. 成本效率指标（调研 R9）

依据 `docs/v4.2/research/agent-optimization-practice.md` R9。按 worker 与题目汇总三项：
- 每个被保留改进（本地门禁判为 improved 的实验）消耗的 token 与设备秒；
- 每分官方增益消耗的 token 与设备秒；
- 每次判题的增益。

数据来自 §1 的用量、§2 的工具记录、测试登记表与规范回执（`scheduler/receipts.py`）。§3 的冲刺口径报告同时给出这三项的现状。

## 验收

- 单测：
  - 用裁剪后的 mesh 与 DSH 流水 fixture（各几十行，去敏，zstd fixture 可在测试里现场压缩）覆盖三种驱动的解析与 `unavailable`、六种未命中原因、MCP 记录与轮转；
  - hot mesh 字段可缺省；
  - 回放的切点识别、四类探针、覆盖评分与 bootstrap 结果确定（固定 seed）。
- `swift build` 通过。
- 真实数据：对本场冲刺流水（`mesh-dee-*.jsonl`，只读）运行 `replay metrics` 与 `replay bench --schemes current,current_plus_ledger,full_history`，结果写入 `docs/plan-4.1-4.2/reports/P03-baseline.md`。这份报告是 4.2 全部目标的基线。
