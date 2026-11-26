# P14 同伴消息协议（4.2 F）

分支：`impl/P14-peer-protocol`。在 P06 合入后的 `main` 上开发。

针对：本场 `peer` 消息 198 条、共 371KB，`result` 消息 317 条、共 723KB，单条最长 7KB；以及 Codex 压缩后只剩同伴长消息的问题。现有入口：`agents/manager.py` 的 `squad`、`_squad_targets`，以及 `mcp/handlers_agents.py` 的 `tool_parhelion_squad`。

## 设计

1. `parhelion_squad` 改为结构化信封：`{to, refs:[账本 id], claim ≤280 字, evidence: "path#anchor" | 运行 ID | 回执 ID, ask?}`。
   - 旧的 `message` 参数保留一个版本，作为兼容。
   - 旧参数的处理：正文 ≤280 字时直接当作 claim；更长时按第 2 条处理。
2. 正文超过 1200 字时，自动转成附件：落盘到 `<workspace state>/peer/<msg_id>.md`，消息里只带路径与 claim。claim 取前 280 字，并提示发送方下次写 claim。不直接拒收，避免丢信息。
3. 去重：引用段与收件方近期已收到的消息高度重复时剥掉，例如逐行 shingle 的 Jaccard ≥0.8；只保留 refs。
4. 收件方：
   - 只收到「来自谁、refs、claim、evidence 路径」；
   - 同时在收件方的账本里记一条 `source=peer, confidence=unverified`，引用发送方的 id。用 P05 的 `ledger/store.py`。
5. 批量投递：同一收件方的多条同伴消息，在它的下一个回合边界合并成一条投递，不再逐条唤醒 agent。管理员指令和判题反馈不合并，仍立即投递。
6. `result` 消息：worker 的阶段结果（`worker_report`）给总线的版本，同样受 claim/附件规则约束；全文落盘，并写一条账本 `decision` 或 `insight`。

## 验收

- 单测：
  - 信封校验；
  - 兼容旧参数；
  - 1200 字转附件；
  - 去重剥离；
  - 收件方的账本条目；
  - 回合边界合并投递，管理员指令与判题不合并；
  - `result` 消息的落盘。
- 回放口径：用本场总线消息（`agent_bus.db`，只读打开）估算新协议下的 peer 与 result 字节，写进报告。
