# P04 变化驱动的等待，取代轮询（4.2 B）

分支：`impl/P04-change-driven-wait`。针对：主控每小时约 560 次轮询（`agent_wait` 约 250、`test_wait` 约 200、`agent_send` 约 200、`status` 约 110）。

## 现状

- 等待相关代码：
  - `agents/manager.py` 的 `wait_events`（约 :926）、`worker_inbox`、`read_messages`；
  - `mcp/handlers_agents.py` 的 `tool_parhelion_agent_wait`；
  - `mcp/handlers_bench.py` 的 `test_wait`、`status`；
  - `services/test_runs.py` 的 `wait`、`refresh`、`list_undelivered`；
  - `daemon/agent_routes.py` 与 `daemon/server.py`。
- daemon 在运行时，MCP 调用经 `_proxy_daemon` 转发；没有 daemon 时才本地直连。两条路径都要保留。
- DSH 插件给 MCP 工具设了 120s 超时（`drivers/dsh_parhelion_mcp.mjs`），所以单次阻塞必须明显短于 120s，默认上限 100s。

## 设计

1. 新模块 `cli/opsb_core/agents/event_hub.py`（≤350 行），挂在 daemon 内：
   - 版本号 `rev = "<boot_id>:<n>"`，n 单调递增。`since` 的 boot_id 与当前不一致时，返回 `resync=true` 与当前快照摘要，不报错。
   - 事件源有四个：
     - `inbox`：`AgentBusStore.append_message`，按收件人；
     - `test`：test_runs 登记表的状态变化；
     - `agent`：`update_agent` 的状态变化；
     - `judge`：`results/submit-queue-outcome.jsonl` 新增的回执。提交在子进程里完成，所以 daemon 内以约 1s 间隔检查文件大小与 mtime，按 `submission_id` 去重。
   - 突发合并：250ms 合并窗口，每秒最多投递 32 条，参考 Reasonix。
   - 按调用者过滤：worker 只看到自己的收件箱、自己的测试、本题的回执与自己的状态；主控看到全部。
2. MCP 新工具 `parhelion_wait(since?, until=[inbox|test|agent|judge], timeout?)`，worker 与 orchestrator 都可用：
   - 服务端阻塞；
   - 无变化时只返回 `{changed:false, rev, waiting_for, idle_streak, next_timeout}`；
   - 有变化时只返回 `rev` 之后的增量，每条一行紧凑摘要。全文用已有的 `agent_messages` / `test_wait` 取，或按 id 取。
3. 连续无变化：`idle_streak` 递增，`next_timeout` 自动加长，但不超过 100s。返回里用一句话告诉模型它在等什么，例如「等待 FFT 的 standard 运行 r-xxxx，进度 9/15」，避免空转。
4. `parhelion_status(if_rev=...)`：自上次以来没有与 status 相关的变化时，返回 `{not_modified:true, rev}`。status 的 rev 由 hub rev 与 daemon 冷快照 etag 组合。
5. `agent_wait`、`test_wait` 保留原参数与返回形状，内部改走 hub。没有 daemon 的本地模式保留现有轮询实现作为回退。
6. 主控提示里的等待用法，在 `mcp/schemas.py` 的工具描述中写清：优先 `parhelion_wait`，不要循环调用 status。比赛提示词模板在 `services/templates_md.py`，如有轮询指引一并修改。

## 验收

- 单测：
  - hub 的 rev 单调、boot_id 变化时 resync、250ms 合并、32 条/秒限流、按调用者过滤、四个事件源；
  - 无变化与有变化两种返回；
  - `idle_streak` 与超时上限；
  - `status(if_rev)` 的 `not_modified`；
  - `agent_wait` / `test_wait` 返回形状不变；
  - 本地无 daemon 回退。
  
  daemon HTTP 层的测试仿照 `cli/tests/test_agent_mesh_http.py`。
- 新增测试用假 adapter，不调用真实模型。
- 报告给出设计的延迟数据：从事件发生到 wait 返回的 p50 与 p95，用测试或本地基准测得。
