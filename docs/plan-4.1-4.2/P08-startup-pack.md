# P08 worker 启动包与 DSH 插件按需挂载（4.2 G；4.1 #15）

分支：`impl/P08-startup-pack`。在 P06 合入后的 `main` 上开发。目标指标：worker 从起步到第一次平台调用的上下文 ≤15K（现状最高 44K）；以 `cd` 开头的 shell 调用降 70%。用 P03 的 `opsb replay metrics` 口径衡量。

## G. worker 启动包

`drivers/brief.py` 现在拼的是 worker brief（≤1.5KB）加状态卡。改为「brief + 启动包」，启动包由代码生成，预算约 6K token，可配置：

1. 任务契约：现有 brief 的内容。
2. 工作树的绝对路径。明确写出：所有 shell 工具调用使用 `workdir` 参数或绝对路径，不要 `cd && ...`。DSH 与 Codex 的 bash 工具都支持 `workdir`，你需要核实各驱动。
3. 仓库地图，1–2K token：
   - 工作树里有 `aoci.txt` 时，取它的 F 字段压缩版；
   - 没有时，由代码列出受管源文件，每个文件一行，含路径、行数与主要符号（类似 Aider 的 repo map）。
4. 要改的 3–5 个文件及行号范围：由当前最佳 SHA 相对题目模板基线的 `git diff --stat` 与 hunk 范围得出，再加上内核入口文件。
5. 本题账本摘要，用 P06 的 `ledger/seed.py` 中的摘要段。

总量上限按字节截断（UTF-8），超出时按 5、3、4 的顺序截短。`compose_session_brief` 的 1.5KB 上限只约束契约部分。新增启动包的大小进入 P03 的测量。

## G. 主控查看 worker 的工作

MCP 新工具 `parhelion_agent_peek(agent_id, what=files|file|log_tail|diff_stat, path?, lines?)`，仅 orchestrator 可用：

- 读取该 worker 工作树中的文件（限工作树内的相对路径）；
- 读取会话流水尾部（只读）；
- 读取 `git diff --stat`。

输出遵守现有上限；P13 合入后改用 `bound_output`。

## #15 DSH 插件的副作用

`drivers/dsh_parhelion_mcp.mjs` 与 `drivers/dsh_driver.py` 的 `ensure_parhelion_mcp_plugin` 现在的行为：比赛目录下每个打开的对话都会起一个 `opsb mcp serve`（本场 7 个），主控的原生子代理也拿到 orchestrator 角色。

1. 按需挂载：
   - 查清 DSH 插件 API 是否支持懒启动，即首次调用工具时才 spawn。依据插件现有代码与 DSH 文档，或 `~/.dsh` 下的插件示例，只读；
   - 支持就改为首次调用时启动；
   - 不支持时，退而求其次：让 `opsb mcp serve` 空闲 N 分钟后自动退出，并在下次请求时由插件重启。如果插件无法重启，在报告里说明。
2. 按 Agent 类型区分角色：
   - 比赛根目录的主对话为 orchestrator；
   - 位于 `.worktrees/*` 的对话为 worker；
   - 原生子代理为 worker。如何识别原生子代理，需要你从插件上下文里找可用字段，找不到就默认 worker，宁可少给权限。
3. 插件块由起止标记界定，启动时整段改写。保持这个机制，DSH 会热加载，不要求重启。

## 验收

- 单测：
  - 启动包的各段、预算截断顺序、绝对路径与 `workdir` 指引；
  - 仓库地图两种来源；
  - 要改文件的推导，用 fixture git 仓库；
  - `agent_peek` 的路径越界拒绝与角色限制；
  - 插件渲染出的角色判定与懒启动配置。
- 用 node 对 `.mjs` 做一次最小调用：伪造插件上下文，检查返回的 server 配置。
- 回放口径：对 3 个真实 worker 生成启动包，记录字节数与估算 token，写进报告。
