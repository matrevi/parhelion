# P13 统一的工具输出上限（4.2 E）

分支：`impl/P13-bound-output`。在 P12 合入后的 `main` 上开发。

## 设计

1. 新模块 `cli/opsb_core/mcp/bound.py`，提供 `bound_output()`，接在 `mcp/server.py` 的 `handle_jsonrpc_message` 的 `tools/call` 出口，是唯一出口：
   - 小结果直接返回。上限 `mcp.output_cap_bytes` 默认 5000，可配置。
   - 超过上限时落盘到 `<workspace state>/spill/<agent_id>/<ts>-<tool>.json`，返回 `{spilled:true, path, total_bytes, head, tail}`。head 与 tail 各约 1KB，且按 JSON 结构截断：先保留顶层标量与短列表。
   - 工具失败时保留尾部，因为错误信息通常在末尾。
   - 落盘文件按时间与总量回收，例如保留 7 天、总量 200MB。回收后的路径要从 doctor 清理中可见。
2. 分页：
   - 返回列表的工具（`agent_messages`、`kb_query`、`parhelion_ledger`、`parhelion_report` 的 history 与 queue 视图）接受 `offset`/`limit`，或者 `cursor`；
   - 超限时优先返回一页，并附 `next_cursor`，而不是落盘。
3. 回读：新工具 `parhelion_spill_read(path, offset?, limit?)`，worker 与主控都可用。只允许读 spill 目录内本 agent 的文件，主控可读全部。回读结果按窗口返回，本身不再二次落盘。
4. 简洁与详细两种格式：`parhelion_status`、`parhelion_kb_query`、测试摘要（`parhelion_test_wait` 与测试结果消息）接受 `format=brief|full`，默认 `brief`。brief 的内容由各 handler 定义，并在 schema 描述里写清；full 仍受 `bound_output` 约束。
5. 兼容：`status` 默认 2KB、`kb_query` 默认 3 条且每条 600 字符，这些是现有约束（`mcp/handlers_bench.py` 的 S），保留为 brief 的默认值。
6. 计量：P03 的 MCP 记录增加 `spilled` 与 `bound_bytes` 两个字段。

## 验收

- 单测：
  - 小结果原样返回；
  - 超限落盘，路径、头尾与 `total_bytes` 正确；
  - 失败保留尾部；
  - 分页的 cursor 连续，且没有重复或遗漏；
  - `spill_read` 的越权拒绝，回读不再二次落盘；
  - brief 与 full 两种格式；
  - 落盘回收。
- 回放口径：用 P03 的 `replay metrics` 估算，如果本场全部 MCP 返回都经过 `bound_output`，返回字节会减少多少。写进报告。
