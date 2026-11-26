状态：已交付

# P08 worker 启动包与 DSH 插件按需挂载（4.2 G；4.1 #15）

## 1. 分支与提交

分支 `impl/P08-startup-pack`，起点 `main` = `a48b3c9`（含 P12/P14/P18 的合入）。

```
227a8a0 Add the code-built worker start-up pack
d19e18f Add parhelion_agent_peek and scope DSH Parhelion mounts by Agent type
（最后一条）Deliver P08: start-up pack, agent peek and DSH mount scoping   ← 本报告随该提交进入分支
```

提交署名 `Generated-by: DSH deepseek-flash`。没有合入 `main`、没有推送。`aoci*.txt`、`.aoci/`、`~/.dsh`
均未改动（`git status` 与提交列表可核对）。

改动文件：

| 文件 | 角色 |
|---|---|
| `cli/opsb_core/drivers/startup_pack.py`（298 行） | 启动包：契约/路径/账本三段、字节预算与 5→3→4 截断、`record_size` |
| `cli/opsb_core/drivers/repo_map.py`（129 行） | 仓库地图两种来源（aoci F 字段 / 代码扫描） |
| `cli/opsb_core/drivers/change_targets.py`（111 行） | 要改的文件与行号范围（`git diff` 推导） |
| `cli/opsb_core/drivers/brief.py` | 薄接入点：`include_startup_pack` 参数、门禁措辞、P15 插槽注释 |
| `cli/opsb_core/drivers/registry.py`、`agents/manager.py` | 两个生产调用点启用启动包 |
| `cli/opsb_core/config.py` | `startup_pack.tokens` / `startup_pack.repo_map` 默认值 |
| `cli/opsb_core/mcp/peek.py`（295 行） | `parhelion_agent_peek` 实现 |
| `cli/opsb_core/mcp/{schemas,tools,handlers_agents}.py` | 工具三处登记（schema、分发、角色） |
| `cli/opsb_core/drivers/dsh_parhelion_mcp.mjs`、`dsh_driver.py` | 按 Agent 类型给角色；块机制不变 |
| `cli/tests/test_startup_pack_p08.py`（11 条）、`test_agent_peek_p08.py`（12 条）、`cli/tests/node/parhelion_plugin_check.mjs` | 单测与 node 最小调用 |
| `cli/tests/test_mcp_server.py`、`test_agent_mesh_http.py` | 工具计数 27→28 |

## 2. 逐项完成情况

| 子计划条目 | 状态 | 实现与证据 |
|---|---|---|
| G1 任务契约（现有 brief），**1.5KB 只约束契约段** | 已完成 | `startup_pack.section_contract` 用 `_truncate_utf8(contract, 1500)`；契约段上限常量 `CONTRACT_MAX=1500` 与 `MAX_BRIEF_BYTES` 一致。单测 `test_sections_carry_contract_path_workdir_and_budget` 断言该段 ≤1500 字节，而整包预算独立（默认 6000 token = 21600 字节）。 |
| G2 工作树绝对路径 + `workdir` 指引 | 已完成 | `section_worktree` 输出 `Path(worktree).resolve()` 与逐驱动规则；`WORKDIR_TOOLS` 表按 driver kind 选措辞（DSH/Codex 给 `workdir=…`，Claude/ACP/Pi 给绝对路径）。单测 `test_workdir_guidance_is_per_driver` 三段各断言。 |
| G3 仓库地图（两种来源，1–2K token） | 已完成，**默认关闭** | `repo_map.py`：有 `aoci.txt` 时取 F 字段（并读它 manifest 里的 volume 文件，兼容 parhelion 自己的 volumes 格式），没有时按受管源文件一行一个（路径、行数、主要符号）。R11 结论见 §4：回放显示收益不抵成本，`startup_pack.repo_map` 默认 `false`，可用配置或 `PARHELION_STARTUP_REPO_MAP=1` 打开。单测覆盖两种来源（`test_repo_map_uses_aoci_f_fields_when_present`、`test_repo_map_falls_back_to_code_scan`）。 |
| G4 要改的 3–5 个文件及行号范围（+内核入口） | 已完成 | `change_targets.py`：最佳 SHA（`board_sha`→`HEAD`）对题目模板基线（最早的 root commit）做 `git diff --numstat` 与 `-U0` hunk 解析，代码文件优先、每文件最多 3 段，再补 `kernel.asc`/`main.asc` 入口文件。fixture git 仓库单测 `test_change_targets_derives_files_and_hunk_ranges` 断言 `kernel.asc` 的 `[[5,6]]` 与 `main.asc` 的 entry 标记。 |
| G5 本题账本摘要（`ledger/digest.render` + `state_card_ext`） | 已完成 | `section_ledger`：`digest.render(rows, own=agent_id)` + `state_card_ext` 扩展卡 + 平台状态卡（`state_card.build`，保留 `STATE ` 标记，编排测试依赖它）。新 worker 无账本行且无回执时自动省略扩展卡（全是 n/a），只留 digest 一行与状态卡。单测 `test_ledger_section_uses_digest_and_state_card`。 |
| 截断顺序 5、3、4，按 UTF-8 字节 | 已完成 | `fit()` 按 `TRUNCATE_ORDER=(5,3,4)`、每段 0.6/0.35/0.15/0 四档收缩，段 1、2 从不裁剪；`_truncate_utf8` 按字节切并在多字节边界丢弃残片。单测 `test_fit_truncates_in_order_5_3_4_and_never_the_contract` 与 `test_truncation_counts_utf8_bytes_not_characters`（3000 个汉字 ≈ 9000 字节，必须被裁）。 |
| 启动包大小进 P03 测量 | 已完成 | `startup_pack.record_size` 往 `metrics/mcp_meter` 写一行 `tool=startup_pack`（bytes/tokens/分段/地图来源），`opsb replay metrics` 的 `mcp_calls` 直接统计它。`compose_session_brief` 启用启动包时调用；单测 `test_pack_size_enters_the_p03_meter`。 |
| 修正 `brief.py` 门禁措辞（第 188 行附近） | 已完成 | 改为 “Quota is not a gate: S4 only decides dispatch timing … From attempt four, apply the safety, gain and circuit gates; quota never blocks them.” 单测 `test_orchestrator_brief_does_not_treat_quota_as_a_gate`。P14 的 `parhelion_squad` 信封那行原样保留（claim ≤280 / refs / evidence / 1200 字转路径 / 回合边界合并），同测试断言仍在。 |
| `parhelion_agent_peek` | 已完成 | 见 §3。 |
| DSH 插件：按 Agent 类型给角色 | 已完成 | 见 §5。 |
| DSH 插件：按需挂载 | 部分完成（调查结论：不可行，未改） | 见 §5.3：客户端没有懒启动模式；把挂载推迟到首个 step 会让该 step 的工具表缺 `parhelion_*`；服务端空闲退出会被客户端的自动重连立刻拉起。三条都有代码依据，未强行实现。 |

### 2.1 `parhelion_agent_peek`

`parhelion_agent_peek(agent_id, what=files|file|log_tail|diff_stat, path?, lines?)`，仅 orchestrator：

- `files`：worker 工作树两层清单（上限 120 条）；
- `file`：**工作树内相对路径**，`lines` 支持 `40-120` 或起始行；绝对路径、`..` 越界、symlink 逃逸（`resolve()` 后比对）全部拒绝；
- `log_tail`：`<logs>/sessions/mesh-<agent_id>.jsonl` 尾部（默认 40 行、上限 200，单行 400 字符），只读，且 `cookie/authorization/token/api_key/password/session_id` 等键值被替换为 `<redacted>`；
- `diff_stat`：`git diff --stat` + `--shortstat`；
- 输出上限 8000 字节（`_fit` 从最长文本字段回缩并置 `truncated`），单文件 256KB、二进制拒绝。

单测（12 条）：路径越界 3 种参数化 + symlink 逃逸、行范围、清单、diff、日志只读与脱敏、未知 agent/错误 view、角色限制（worker 走 JSON-RPC 得到 `-32601`，orchestrator 正常返回）、8KB 上限、node 插件调用。

## 3. 逐驱动 `workdir` 结论与沙箱能力（要求逐条列出）

| 驱动 | shell/终端工具 | 支持 `workdir` | 依据（代码或文档） |
|---|---|---|---|
| DSH (`deepseek`) | `bash` | **是** | `dsh-tool-bash/lib/index.js`：inputSchema 的 `workdir` 字段（L506）、`...args.workdir !== void 0 ? { cwd: args.workdir } : {}`（L261）、`resolveWorkdir(args.workdir, exec, standingPolicy?.workspaceRoot)`（L650/L654）；同包 `README.md` L56/L103：“pass `workdir` instead of `cd`”，相对 workdir 按会话 cwd 解析、沙箱 workspace root 优先。本会话注入的 bash 工具描述也是同一段文字。 |
| Codex | `exec_command`（旧版 `shell`） | **是** | openai/codex `codex-rs/core/src/tools/handlers/shell_spec.rs`（main）：`workdir` 属性，描述为 “Working directory for the command. Defaults to the turn cwd.”；app-server 每次会话透传 `approvalPolicy`/`sandbox`（`drivers/codex_appserver.py` L239–265）。 |
| Claude Code | `Bash` | **否** | Bash 工具参数只有 `command`/`description`/`timeout`/`run_in_background`（本机 `claude` 二进制中 `run_in_background` 出现 85 次；`workdir` 只出现在它自己的 EnvironmentWorker API 与 `--workdir` CLI 开关，不在 Bash 工具 schema 里）。命令在同一 shell 内 `cd` 会保留，所以指引为“用绝对路径”。 |
| Antigravity (ACP) | agent 自有 terminal 工具 | 平台不可控 | `drivers/antigravity_acp.py` 只是 ACP 客户端，工具面由 agent 提供；指引退化为绝对路径。 |
| Pi | agent 自带 bash | 未透传 | `drivers/pi_driver.py` 只负责进程启动，平台不注入工具参数；指引为绝对路径。 |

| 驱动 | 沙箱 / 审批能力 | 依据 |
|---|---|---|
| DSH | 文件策略 `read-only` / `workspace-write` / `danger-full-access`（本会话为 `danger-full-access`），审批 `ask`/`never`；bash 另有 sandbox policy 与逐次升级审批 | `~/.dsh/profiles/desktop/cordis.patch.yml` 的 `permission` presets；`dsh-fs-sandbox`/`dsh-bash-sandbox`/`dsh-sandbox-policy` 包；本会话运行时上下文 |
| Codex | `read-only`/`workspace-write`/`danger-full-access` 三档，`[sandbox_workspace_write] network_access = true`（本机 `~/.codex/config.toml`），逐次 `sandbox_permissions=require_escalated` 需审批 | `~/.codex/config.toml`；`shell_spec.rs` 的 `sandbox_permissions`/`justification`/`prefix_rule`；`codex_appserver.py` |
| Claude Code | 无 OS 沙箱：本机 `~/.claude/settings.json` 为 `permissions.defaultMode=bypassPermissions`、`sandbox.enabled=false`，只有绝对路径与提示词约束 | `~/.claude/settings.json` |
| Antigravity (ACP) / Pi | 由 agent 自己决定，平台只转发 | `drivers/antigravity_acp.py`、`drivers/pi_driver.py` |

结论：启动包里 DSH/Codex 直接写 `workdir=<绝对路径>`，Claude/ACP/Pi 写“绝对路径 + 不要在命令开头 `cd`”。这三个驱动之下，平台对越界写没有强制手段，只能靠工作树路径与角色约束。

## 4. R11：启动包逐段回放对比（有/无该段）

口径：`opsb replay metrics`（窗口 2026-11-25T14:30Z–2026-11-26T05:41Z，15.17 h，135 个 worker）给出
`context.startup_median=17224`、`startup_max=126745`（只有 61/135 在 15K 以内）、`cd_shell.count=6998`。
逐段用同一批真实 worker 的“第一次平台调用之前的调用与结果字节”做对照。

| 段 | 3 个真实 worker 的字节 | 估算 token | 去掉该段的代价（回放） | 结论 |
|---|---|---|---|---|
| 1 任务契约 | 1498–1500 | 416–417 | 没有目标与循环说明，首轮无法行动 | **必留**（1.5KB 上限只约束这一段） |
| 2 工作树/workdir | 469–485 | 130–135 | 16/16 个 worker 的第一条调用都是 `pwd && git log && ls` 形态（结果 557B–3104B），本段直接给出绝对路径与“别用 cd” | **留**（成本 130 token，替代首个 survey 的一部分） |
| 3 仓库地图 | 1189–1556 | 330–432 | 发现阶段里“清单型”调用（只有 `pwd/ls/find/wc/git log|status`）合计 557–4432B，中位约 1870B≈520 token；大块是 `kernel.asc` 54KB、`DESIGN.md` 15KB、`reference.py` 17KB 这类必须读的正文，地图替代不了 | **默认关**（收益上限≈成本，且 R11 的对照研究是负收益）；`startup_pack.repo_map=true` 可开 |
| 4 要改的文件 | 468–484 | 130–134 | 替代首条 survey 里的 `git log/status/diff --stat`（观测 557–3114B ≈ 155–865 token），并直接给出 hunk 行号 | **留**（净正） |
| 5 账本摘要 | 1898–2289 | 527–636 | 旧状态卡 774–1050B（215–292 token）。新段多出 digest+扩展卡；无账本行且无回执的新 worker 会自动省略扩展卡 | **留**（计划要求；有历史时才付费） |

整包（3 个真实 worker，预算 6000 token/21600 字节，**未触发截断**）：

| worker | 题目 | 旧 brief+状态卡 | 启动包（地图关） | 启动包（地图开） |
|---|---|---|---|---|
| `dee-cf7eae` | SgemmFp16Cube | 2758 B | **4482 B / 1245 token** | 5688 B / 1580 token |
| `dee-7be6aa` | EdgeIndexCoalesce | 3030 B | **4881 B / 1356 token** | 6415 B / 1782 token |
| `dee-663447` | NeighborListPbcFixedK | 2916 B | **4552 B / 1264 token** | 6125 B / 1701 token |

分段（地图关，字节）：契约 1498–1500 · 工作树 469–485 · 要改的文件 468–484 · 账本 1898–2289。
生成耗时 0.14 s/次（含 git、账本与状态卡读取）。

**R11 结论**：仓库地图默认关闭（配置可开），启动包里保留的是不可发现内容——工作树绝对路径与
`workdir` 规则、要改文件的行号、账本里的失败方向与目标用例。这与子计划 §“仓库地图只有在回放证明有
收益时才默认开启”一致。

## 5. 回放验证

### 5.1 三个真实 worker 的起步成本与启动包

`startup` 取 `opsb replay metrics` 的 `context.per_agent[*].startup`（第一次平台调用前的最后上下文），
“发现”= 第一次平台调用之前的工具结果字节。

| worker | 题目 | 轮次 | 第一次平台调用前的上下文（token） | 发现调用 | 发现结果字节 | 新包替换旧 brief 的增量 |
|---|---|---|---|---|---|---|
| `dee-cf7eae` | SgemmFp16Cube | 34 | 10140 | 1 | 557 | +1724 B（≈+479 token） |
| `dee-7be6aa` | EdgeIndexCoalesce | 53 | 35877 | 12 | 80611 | +1851 B（≈+514 token） |
| `dee-663447` | NeighborListPbcFixedK | 13 | 52565 | 7 | 98070 | +1636 B（≈+454 token） |

16 个本场 worker 的总体分布：首请求上下文 9969–10141 token（中位 10057）；第一次平台调用前的发现
1–15 次调用、557B–122585B（中位 52571B，11/16 超过 5KB）；16/16 的第一条工具调用都是 shell survey。
因此“起步→首次平台调用”的主导项是发现阶段的正文读取，不是 brief 大小；启动包把这部分变成“先读这几
行、先问平台”，增量约 +450 token。目标 ≤15K 要等下一场用同一口径（`opsb replay metrics`）实测；
本包只给出上述估算。

### 5.2 `cd` 口径

- 本场 16 个 worker 的 2888 次 bash 调用中 **1984 次以 `cd` 开头（68.7%）**，其中 723 次是 `cd <自己的
  工作树>`（占 cd 的 36.4%，可被 `workdir` 一一替代），1261 次是 `cd` 到比赛根/结果目录（属调查类，由
  P12/P15 的数据通道覆盖），仅 60 次显式传了 `workdir`。
- P03 口径（窗口内全体 135 个 worker）：`cd_shell.count=6998`，15.49MB 结果字节，461 次/小时。
- 对“以 cd 开头的调用降 70%”：单靠 `workdir` 指引能覆盖 36.4%（自己的 worktree）；剩下 63.6% 需要
  P15 的数据通道把“读比赛根/结果目录”换成 `parhelion_report`，本包无法单独达成，下一场同口径复核。

### 5.3 DSH 插件：懒启动为什么没做

| 问题 | 结论 | 依据 |
|---|---|---|
| mcp-client 是否支持懒启动（首次调用才 spawn） | **不支持** | `@deepseek-ai/dsh-mcp-client` 的 `Config` 只有 `transport/serverName/command/args/env/cwd/toolCallTimeoutMs/failOnStartupError/maxInstructionBytes/reconnect`；`apply()` 直接 `const connection = startConnection(...)` 后 `await connection.ready`（连接 + 首次工具同步完成后插件才激活）。README：“The server's tools appear before the harness starts its first turn”“stdio negotiation starts a temporary probe process before the serving process”。 |
| 能否把 `ctx.plugin(...)` 推迟到首个 step | **不安全** | `dsh-agent-loop/lib/index.js` 的 `preStep()` 先 `await this.loopCtx.systemPrompt.assemble(...)` 得到 `assembly.tools`，再跑 `agent/pre-step` 瀑布；在 pre-step 里挂载只会让**下一步**才看到工具，而新 worker 的第一步正是它第一次调用平台的 step。 |
| 退而求其次：服务端空闲退出 + 插件下次重启 | **对该客户端无效** | 连接监管器在子进程退出时 `scheduleReconnect()`（`connection lost; reconnecting`），`initialDelayMs=500`，每次 `connectGeneration()` 都新建 `StdioClientTransport`（重新 spawn）。所以空闲退出会被 500ms 后自动拉起，变成“退出—拉起”循环，稳态进程数不变；把 `reconnect.enabled` 关掉则工具仍在列表里但调用一直失败，直到重载插件。插件自身可以 `fiber.dispose()` 后重新 `agent.ctx.plugin(...)`，但重挂只能发生在 pre-step 或之后，即整整晚一步。 |
| 不做懒启动时，本包交付了什么 | 角色正确 + 每 Agent 一个 mount；组内请求从 orchestrator 变 worker | 见 §5.4 |

**成本实测**：每个 `opsb mcp serve` 空闲 RSS 约 11.6 MB，`initialize`+`tools/list` 端到端 0.12 s（含进程启动），
本场 7 个对话 ≈ 81 MB、启动期约 0.85 s——资源不是主要问题，**角色越权才是**。真正要去掉“每对话一个进程”，
应改为共享 `opsb daemon` 的 `POST /v1/mcp`（streamable-http）：`daemon/server.py` L479–489（角色头在 L486–487） 已经按
`X-Parhelion-Role` / `X-Parhelion-Agent-Id` 头分角色，`do_POST` 走 token 校验。这需要 daemon 侧的令牌分发与
一次真机验证，超出本包范围，列为后续。

### 5.4 角色判定

`agentRole(cwd, header, root)`：`header.origin === 'subagent'` 或存在 `header.parentSession` → worker
（这两个字段由 `dsh-subagent` 的 `childSessionMeta` 写入，见其 `lib/index.js` L471–480）；cwd 等于比赛根 →
orchestrator；cwd 路径段含 `.worktrees` → worker；其余（含无法识别）→ **worker**。

node 最小调用（`cli/tests/node/parhelion_plugin_check.mjs`，伪造 `agents/loader/effect/on/logger` 与磁盘上的
比赛目录，真跑 `apply()`）结果：6 个伪造 Agent 中 `main`=orchestrator、worktree/原生子代理/子代理在 worktree/
未分类目录=worker、比赛外目录不挂载；每个 mount 的 args 为
`[<opsb>, 'mcp', 'serve', '--workspace', <root>, '--role', <role>]`，`serverName=parhelion`，
`toolCallTimeoutMs=120000`。另有真实 stdio 抽查（工作树代码 + `--role`）：

```
worker tools: 23        （无 parhelion_agent_peek / parhelion_agent_spawn / parhelion_sprint_start）
orchestrator tools: 28  （含 parhelion_agent_peek）
```

## 6. 测试

两条全量命令（工作树内）的末行输出：

```
739 passed, 19 skipped, 2 subtests passed in 75.09s (0:01:15)
```

```
492 passed in 67.90s (0:01:07)
```

基线是 716 passed / 19 skipped 与 469 passed；新增 23 条（启动包 11 + peek/插件 12），无既有用例被删除。
本包未改 Swift，未跑 `swift build`。`runtime/tests/test_device_broker.py` 的两条预算测试本轮未出现高负载偶发失败。

## 7. NOTE（非显然事实，供主会话写 aoci 条目）

1. **启动包默认不启用**：`compose_session_brief(..., include_startup_pack=False)` 保持旧行为，因为既有用例断言
   brief+状态卡 <3KB 且含 `STATE `；两个生产调用点（`registry.launch_problem_session`、`agents/manager` 的 worker
   启动）显式传 `include_startup_pack=True`。若希望所有 driver 默认开启，改两处默认值即可。
2. **状态卡仍在启动包里**：`section_ledger` 末尾保留 `state_card.build`（`STATE ` 行），编排测试
   `test_sprint_modes_open_one_two_or_three_seats_per_problem` 依赖它；扩展卡在没有账本行、没有回执时省略。
3. **比赛题仓库没有 `aoci.txt`**（本机 18 个题目目录都不存在），所以实际生效的是代码扫描来源；
   parhelion 仓库自身是 volumes 格式（`aoci.txt` 只是 manifest），`repo_map` 会继续读它声明的 volume 文件。
4. **模板基线 = 最早 root commit**。若某题仓库是从导入树开始的历史（没有单独的模板提交），差异会包含全部文件，
   此时 `change_targets` 仍按代码优先排序，不会写错但会列满 5 个文件。
5. **token 估算固定用 3.6 字节/token**（`startup_pack.BYTES_PER_TOKEN`），预算与报告数字同源；它只是估算，
   不是分词器。
6. **`state_card_ext` 的读路径**会 import `scheduler.*`（`case_map`/`receipt_predict`），与门禁同源但只读；
   启动包异常全部吞掉，最坏情况退回 digest 一行与状态卡。
7. **`replay metrics` 的 `startup` 是“第一次平台调用前的最后上下文”**，与子计划里“44K”的口径一致；本机当前
   窗格读到的是中位 17.2K、最高 126.7K，说明 44K 是更早一次读数的抽样。
8. **P12 的 `queue` 视图会触发 `cli_bridge status` 写盘**，本包未调用任何 report 视图，回放只读 mesh 日志与
   总线（`mode=ro`），未对真实比赛目录跑任何写路径。

## 8. 风险与后续

1. **`agent_peek` 的输出路径在 P13 合入后要切换**：P13 的 `bound_output` 接在
   `cli/opsb_core/mcp/server.py` 的 `handle_jsonrpc_message()` `tools/call` 分支（`payload = call_tool(...)` 之后、
   `mcp_meter.record_call(...)` 之前——那是全部工具的唯一出口）。rebase 到 P13 后的 main 上需要做两件事：
   - 把 `parhelion_agent_peek` 加进 `cli/opsb_core/mcp/bound.py` 的受限工具清单（P13 会以
     `BOUNDED_TOOLS`/分页函数命名，具体名字以 P13 代码为准；子计划已点名 `agent_messages`、`kb_query`、
     `ledger`、`report history/queue`，`agent_peek` 与之同类）；
   - `peek.py` 里的 8000 字节 `_fit` 退化为本地上限（保留无害），输出真正的截断/落盘由 `bound_output` 统一做。
2. **工具计数 28 → 29**：本分支是 27→28（新增 `parhelion_agent_peek`）。P13 合入后 main 为 28，本分支 rebase
   之后要把三处断言改成 **29**：`cli/tests/test_mcp_server.py` L246、L466 与
   `cli/tests/test_agent_mesh_http.py` L173（当前都已写成 28）。漏改会在主会话合入后的全量测试里炸。
3. **`brief.py` 与 P15 的冲突**：P15 会在 `compose_session_brief` 的 `lines` 列表末尾加一行数据通道路由。我在
   该列表末尾留了注释插槽，P15 只需要在原处插行；冲突时取“P15 的行 + 本分支的 `include_startup_pack`
   参数与 startup_pack 调用”，不要回退门禁措辞与 P14 的 `parhelion_squad` 行。P15 的行会自然落到启动包的
   第 1 段（契约），因此它必须短（契约段仍受 1.5KB 上限约束）。
4. **仓库地图若要在下一场默认打开**：先跑 `opsb replay metrics` 看 `startup_*` 是否因“少读目录”下降；本包给出
   的判据是清单型调用（557–4432B）必须显著高于地图成本（330–432 token）。不达标就保持默认关闭。
5. **共享 daemon 端点**（去掉每对话一个 python 进程）需要 daemon 侧令牌与真机验证；本包只做了只读核实，
   没有改 daemon，也没有重启它。
6. **`change_targets` 的 git 调用全部 5s 超时并吞异常**；仓库很大或磁盘慢时退化为“入口文件清单”，不会阻塞
   启动，但会让第 4 段失去行号。
