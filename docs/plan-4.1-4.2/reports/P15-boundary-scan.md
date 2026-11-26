状态：已交付

# P15 worker 边界（4.2 K）

日期 2026-11-26，分支 `impl/P15-worker-boundary`，基于 `main` `a48b3c9`。四项都已完成，无未做项。没有改 `aoci*.txt` 或 `.aoci/`，没有重启或停止守护进程，没有运行 `opsb daemon reload`，没有派发或停止 worker，没有清理总线，没有调用 `queue approve`/`force`，没有向判题站提交，没有改自动提交开关，没有安装插件或应用，没有移动或删除真实比赛数据。

起点的事件（`docs/v4.2/00-PLAN.md` §1）：有 worker 在自己的工作树里导入 `opsb_core`，用 `board_refresh_service._all_team_sessions()` 读出全队判题账号的会话去查排行榜。本报告 §3 的回放扫描在本场 13 个 worker 流水里检出该形态 7 次（`dee-a07dea`，2026-11-26T04:28:25Z 起）。

## 1. 分支与提交

```text
$ git log --oneline main..HEAD
c883ad7 Route worker platform data through parhelion_report
376349c Sandbox worker processes and detect platform-boundary access
```

本报告随最后一个 `Deliver P15:` 提交进入分支，因此上面是本报告提交前的列表。

## 2. 逐项完成情况

### 2.1 进程环境（已完成）

新模块 `cli/opsb_core/worker_sandbox.py`（253 行）：`platform_source_dirs()`、`sanitize_pythonpath()`、`worker_env()`。

- **平台源码目录**：仓库根、`<root>/cli`、`<root>/scheduler`、`<root>/runtime`；当根是 `.app` 的 `Contents/Resources` 时再补上 `Resources/{cli,scheduler,runtime}`（`config.CLI_DIR` 推导，不写死路径）。
- **`worker_env(base, overrides)`**：保留操作系统环境，去掉 `PARHELION_*`/`OPSB_*`（`PARHELION_HOME` 除外）、`JUDGE_SESSION_FILE`、`PYTHONHOME`、`PYTHONSTARTUP`；`PYTHONPATH` 里落在平台源码目录内的条目逐条删除，删空则整个键不出现。驱动自己的变量（`PARHELION_MCP_ROLE`、`PARHELION_MCP_CONFIG`、`CODEX_HOME`、`DSH_PERMISSION_MODE` 等）在过滤之后由 `overrides` 加上，所以 adapter 仍能只交出角色与身份。
- **接线点**（`{**os.environ, **env}` 全部换掉）：

| 路径 | 位置 | 说明 |
|---|---|---|
| `agents/adapters.py` | `BaseWorkerAdapter._launch` | 所有托管 adapter（deepseek/codex/claude/antigravity/gemini/pi）都从这里起进程 |
| `drivers/base.py` | `BaseDriver._execute_headless_turn` | 各 driver 默认的每回合执行 |
| `drivers/base.py` | `BaseDriver.launch_session`（分离式） | 作业进程需要 `PYTHONPATH=cli` 才能 `-m opsb_core.drivers.registry`，所以改为把作业进程整体套上凭据读禁，驱动子进程再由 `_execute_headless_turn` 过滤一次 |
| `drivers/antigravity_acp.py` | `run_acp_headless_turn` | antigravity 与 deepseek 共用 |
| `drivers/claude_driver.py` | `_execute_headless_turn` | |
| `drivers/pi_driver.py` | `_execute_headless_turn` | |
| `drivers/codex_appserver.py` | `run_codex_headless_turn`、`preseed_codex_thread` | |
| `drivers/dsh_driver.py` | `preseed_dsh_conversation` | 预置会话也换掉 |

- **工作目录**：所有启动点本来就是 `cwd=worktree`，未改，单测断言 `cwd` 是工作树。
- **`worker_access.py` 核对结果**：worker 的 `relay.json`（0600）里只有 `HMAC(daemon_token, [workspace, agent_id])` 派生的 scoped token，它只在 `/v1/mcp/worker/<id>` 上通过 `hmac.compare_digest`；守护进程完整 token 不写进 relay，SSH_AUTH_SOCK 被移除，网络与进程执行另有 deny。因此一个 worker 拿不到别人的路由，也拿不到主控路由。这条已加进单测（不同 seat 的 token 不同、都不等于 daemon token）。唯一新增的沙箱内容是凭据读禁（§2.2），没有放宽任何既有 deny。

### 2.2 可读路径（按驱动说明，能做到哪一步）

平台层对所有托管 worker 与分离式启动统一生效：`worker_access.prepare_worker_command` 现在用 `worker_sandbox.seatbelt_profile()` 组装 profile，原来的 `~/.ssh`、`daemon.json`、SSH 网络与 `ssh/scp/sftp` 禁项全部保留，并新增：

| 排除项 | 形式 |
|---|---|
| `~/.parhelion/cj_accounts.json` | `(deny file-read* file-write* (literal ...))` |
| `~/.parhelion/sessions/` | `(deny ... (subpath ...))` |
| `~/.judge/`（含 `session.json` 与 `sessions/`） | `(subpath ...)` |
| `~/.parhelion/daemon.json` | `(literal ...)`（原有） |
| `cache/.cache_remote_judge_*` | `(deny file-read* (regex ...))` |
| `~/.parhelion/config.json` | **仅当**该文件含凭据形状的键（`cookie/token/secret/password/api_key/credential/session_file/auth`）时整文件 deny |

`config.json` 的条件 deny 是源码事实决定的：`sandbox(7)` 没有 JSON 段粒度，而 `config.json` 的默认 schema 里并没有凭据段（只有语言、工作区、`scheduler` 等），凭据只在 `cj_accounts.json`、`sessions/`。无凭据时不 deny，worker 侧 MCP server 照常读到真实配置；一旦有人把凭据写进 `config.json`，整文件 deny，`config.read_json` 对读失败返回默认值，工具退化而不是泄漏。这比子计划「排除 config.json 的凭据段」更准确，也避免了无条件 deny 让 `parhelion_status`/`parhelion_report(queue)` 读到默认阈值。

**逐驱动能做到哪一步：**

| 驱动 | 驱动自身的沙箱配置面 | 能否排除读路径 | 本包做法 |
|---|---|---|---|
| codex | `config.toml` 的 `default_permissions` + `[permissions.<name>.filesystem]`（`read`/`write`/`deny`），另有每回合 `sandboxPolicy` | 新版可以；旧版忽略，且 profile 与 `sandbox_mode` 互斥 | `render_codex_config_toml` 渲染 `parhelion_worker` profile（`extends=":workspace"`、四个凭据路径 `deny`、工作树 `write`），作为纵深；真正生效的是平台 sandbox |
| deepseek (DSH) | `@deepseek-ai/dsh-permission-presets`：`sandbox` 只取 `read-only`/`workspace-write`/`danger-full-access` 三个模式加 approval；`sandbox/mode` 事件记录实际值 | **不能**。`dsh-bash-sandbox` 与 `dsh-fs-sandbox` 的文档写明模式只管写（"reads stay unconfined"），没有路径 deny 配置 | 模式保持原样（改成 `workspace-write` 会禁掉 `git commit` 写共享 `.git`），读排除由平台 sandbox 做 |
| claude | 只有 `--permission-mode` | 不能 | 平台 sandbox |
| antigravity | ACP `session/new` 无读策略 | 不能 | 平台 sandbox |
| gemini / pi | 无 | 不能 | 平台 sandbox |

做不到的那部分由 §2.3 的检测兜底。另有一条同层限制要说明：平台源码目录对 worker **仍可读**，因为 worker 进程树里的 `opsb mcp serve` 自己要读 `cli/opsb` 与 `opsb_core`，同一个 sandbox 无法只禁父进程；因此「不能 `import opsb_core`」靠环境清洗（没有 `PYTHONPATH` 时普通 python 找不到它）加检测（显式 `sys.path.insert` 到平台目录会被报），而不是靠禁读。

**真机实测**（不是只渲染文本）：临时目录里造四个凭据文件，用渲染出的 profile 跑 `/usr/bin/sandbox-exec -p <profile> /bin/cat <file>`：四个文件全部 `rc=1` 且无输出；同一 profile 下 `<PARHELION_HOME>/workspaces/<ws>/state/test_runs/*.json` 可读、普通命令正常。这条实测发现了关键缺陷：`sandbox(7)` 匹配**真实路径**，macOS 的 `/var`、`/tmp` 是符号链接，用未 canonical 的路径写 filter 会静默失效——现在 `read_deny_paths()` 与 `extra` 都先 `resolve()`，并有一条跳过非 macOS 的真实执行单测。

### 2.3 检测与告警（已完成）

- **检测器**：`cli/opsb_core/worker_boundary.py`（282 行，分类 + 告警）与 `cli/opsb_core/boundary_scan.py`（118 行，回放扫描）。两种事件形态都吃：ACP `session/update` 的 `tool_call`/`tool_call_update`（`rawInput`）与 DSH 原生会话 `tool/call`（`data.arguments`，实际是 JSON **字符串**，检测器会先反序列化）。
- **只看能碰文件系统的工具**：shell 类（`bash`/`shell`/`exec`/Codex 的 `commandExecution` 等）取 `command`，文件类（`read`/`write`/`edit`/`grep`/`glob` 等）取路径类参数；`parhelion_*` 工具直接跳过，所以一条提到路径的 `parhelion_squad` 消息或 `parhelion_agent_report` 摘要不会被误报。
- **必须覆盖的形态与规则**（类别按严重度排序，一条命令同时命中时归最严重的一类）：

| 类别 | 命中形态 |
|---|---|
| `team_sessions` | 出现 `_all_team_sessions` |
| `judge_credentials` | `cj_accounts.json`、`.cache_remote_judge_*`、`~/.judge`、`$HOME/.judge`、`<PARHELION_HOME>/sessions/`，或 `JUDGE_SESSION_FILE` |
| `platform_state` | `<PARHELION_HOME>/workspaces/<id>/state/**`（含 `state/test_runs/*.json`）、`agent_bus.db`、`.parhelion/hardware`、`.parhelion/cache` |
| `platform_import` | `import opsb_core` / `from opsb_core …` / `from scheduler …`，`sys.path` 里塞平台根，或读平台源码目录下的文件 |

- **不误报**：路径 token 解析后落在该 worker 自己的**工作树内**就先剔除（连裸词规则也一起剔除），所以 `state/test_runs/*.json`、`agent_bus.db` 在工作树里同名时不算命中；相对路径不解析（worker 的 cwd 就是工作树，无法区分，宁可不报）；`~/.local/bin/opsb` 启动器不算平台源码（brief 里就写着 CLI 兜底）。
- **运行时接线**：ACP 每个 update 先过检测再走原事件转发（`AcpWorkerAdapter._on_update`），Codex 的 `item/*` 同理（`_on_message`）；`_WorkerRunner` 把 `on_boundary` 接到 `worker_boundary.report()`。回放/审计用 `boundary_scan.scan()`。
- **命中动作**（`worker_boundary.report`，幂等）：
  - 总线两条 `platform` 消息，`priority=strategic`：一条 `worker_to_orch`（主控），一条 `agent_id="admin"`/`direction="system"`（管理员）；`event_key=boundary:<agent>:<category>` 与 `boundary:admin:…` 走既有的唯一索引，所以同一 worker 同一类只发一次（跨重启也成立）；
  - 账本一条 `constraint`（`source=platform`、`ekey=boundary:<category>`，重复命中不写第二行）；
  - 热 mesh 的 worker 行新增 `boundary_flags`（落 `agents.boundary_flags`，老库自动 `ALTER TABLE`）；
  - Swift 三处：`AgentWorkerHot.boundaryFlags` 用 `decodeIfPresent([String])` 解码，节点卡片显示红色 `K` 徽标、检查器显示 `BOUNDARY n` 徽标，hover 列出类别；
  - **不自动停止 worker**：告警体写明「是否停止由管理员决定」，单测断言命中后 `status` 不是 `stopped`、adapter 未关闭。
- **隐私**：告警体只有 worker、类别、时间；不含命令、路径内容或读到的任何值。回放扫描只返回 `(worker, 时间, 类别, 工具名, 来源)`。

### 2.4 数据通道（已完成）

- `mcp/schemas.py` 的 `parhelion_report` 描述加了一段：board/tbest/回执/环境事实只经该工具的 `board`/`history`/`per_case`/`env` 视图，不读 `~/.parhelion` 文件、不 import `opsb_core`、不调 `board_refresh_service._all_team_sessions` 这类内部函数，违反会在协作页标记并告警主控与管理员。
- `drivers/brief.py` 的 `compose_session_brief` 列表**末尾加了一行**（198 字节，仍在 1500 字节预算内，`test_orchestration_v4` 的预算断言通过）。
- **⚠ 与 P08 的冲突**：P08 正在另一个工作树里重写 `drivers/brief.py`（brief + 启动包）。我按协调要求只加这一行、没动结构。冲突解法：P08 合并时删掉我的那一行，把同样的意思放进 P08 的契约段（它的 brief 有一节讲平台契约）；语义不变，只是位置从列表末尾移到契约段。文字可直接复用：`Platform data (board, tbest, receipts, env) comes from parhelion_report views board/history/per_case/env, not from ~/.parhelion files, opsb_core imports or platform internals.`

## 3. 回放扫描（本场流水，只读）

**口径。** 只读打开 `~/.parhelion/logs/sessions/mesh-dee-*.jsonl` 的 13 个 worker 流水（与 P12 聚类报告 §1 同一组）与 `~/.dsh/sessions/**` 的原生会话（cwd 在 `.worktrees/` 下的才算 worker），窗口 2026-11-26T04:15–05:42Z。同一 worker 同一秒同一类合并成一行；两种来源都命中时行内不重复计数（本场 130 行全部为 ACP 与 DSH 双来源命中）。

```python
import sys; sys.path.insert(0, 'cli')
from opsb_core.replay.loader import expand_sessions
from opsb_core import boundary_scan
keys = "07013f 0f97d5 17d6d3 2180de 345d84 43695a 754bf6 7be6aa a07dea abcefb b0c078 ba3f0e cf7eae".split()
paths = expand_sessions(",".join(f"mesh-dee-{k}.jsonl" for k in keys))
hits = boundary_scan.scan(mesh_paths=paths, since="2026-11-26T04:15", until="2026-11-26T05:42")
```

**结果**：13 个 worker 里 11 个命中，共 **130** 条；分类合计 `platform_import` 53、`platform_state` 49、`judge_credentials` 21、`team_sessions` 7。

**必须检出的已知事件：`_all_team_sessions` 被检出 7 次，全部来自 `dee-a07dea`，首次 2026-11-26T04:28:25Z**（见表中 `team_sessions` 行）。

### 3.1 按 worker 与类别

| worker | 类别 | 命中数 |
|---|---:|---:|
| `dee-07013f` | platform_import | 4 |
| `dee-07013f` | platform_state | 9 |
| `dee-0f97d5` | judge_credentials | 1 |
| `dee-0f97d5` | platform_import | 3 |
| `dee-0f97d5` | platform_state | 5 |
| `dee-17d6d3` | judge_credentials | 8 |
| `dee-17d6d3` | platform_import | 19 |
| `dee-17d6d3` | platform_state | 7 |
| `dee-2180de` | judge_credentials | 7 |
| `dee-2180de` | platform_import | 7 |
| `dee-2180de` | platform_state | 8 |
| `dee-345d84` | judge_credentials | 4 |
| `dee-345d84` | platform_import | 4 |
| `dee-345d84` | platform_state | 14 |
| `dee-43695a` | platform_import | 1 |
| `dee-754bf6` | platform_import | 2 |
| `dee-754bf6` | platform_state | 1 |
| `dee-7be6aa` | judge_credentials | 1 |
| `dee-7be6aa` | platform_import | 1 |
| `dee-7be6aa` | platform_state | 2 |
| `dee-a07dea` | platform_import | 9 |
| `dee-a07dea` | team_sessions | 7 |
| `dee-b0c078` | platform_import | 1 |
| `dee-b0c078` | platform_state | 3 |
| `dee-ba3f0e` | platform_import | 2 |

### 3.2 全部命中（worker、时间、类别）

| 时间（UTC） | worker | 类别 | 工具 |
|---|---|---|---|
| 2026-11-26T04:16:03Z | `dee-345d84` | platform_import | bash |
| 2026-11-26T04:16:05Z | `dee-7be6aa` | platform_import | bash |
| 2026-11-26T04:18:26Z | `dee-0f97d5` | platform_import | bash |
| 2026-11-26T04:18:28Z | `dee-0f97d5` | platform_import | bash |
| 2026-11-26T04:18:30Z | `dee-0f97d5` | platform_import | bash |
| 2026-11-26T04:18:56Z | `dee-2180de` | platform_import | bash |
| 2026-11-26T04:18:58Z | `dee-2180de` | platform_import | bash |
| 2026-11-26T04:20:48Z | `dee-0f97d5` | platform_state | bash |
| 2026-11-26T04:22:16Z | `dee-0f97d5` | judge_credentials | bash |
| 2026-11-26T04:22:16Z | `dee-0f97d5` | platform_state | bash |
| 2026-11-26T04:22:35Z | `dee-754bf6` | platform_state | bash |
| 2026-11-26T04:22:47Z | `dee-754bf6` | platform_import | bash |
| 2026-11-26T04:23:24Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:23:27Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:24:24Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:24:30Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:25:32Z | `dee-345d84` | platform_import | bash |
| 2026-11-26T04:25:35Z | `dee-345d84` | platform_import | bash |
| 2026-11-26T04:25:37Z | `dee-345d84` | platform_import | bash |
| 2026-11-26T04:26:20Z | `dee-345d84` | judge_credentials | bash |
| 2026-11-26T04:26:20Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:27:48Z | `dee-0f97d5` | platform_state | bash |
| 2026-11-26T04:27:50Z | `dee-0f97d5` | platform_state | bash |
| 2026-11-26T04:27:51Z | `dee-43695a` | platform_import | bash |
| 2026-11-26T04:28:12Z | `dee-754bf6` | platform_import | bash |
| 2026-11-26T04:28:21Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:28:22Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:28:25Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:28:25Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:28:25Z | `dee-a07dea` | team_sessions | bash |
| 2026-11-26T04:28:28Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:28:28Z | `dee-a07dea` | team_sessions | bash |
| 2026-11-26T04:28:29Z | `dee-17d6d3` | judge_credentials | bash |
| 2026-11-26T04:28:29Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:28:31Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:28:31Z | `dee-a07dea` | team_sessions | bash |
| 2026-11-26T04:28:31Z | `dee-ba3f0e` | platform_import | bash |
| 2026-11-26T04:28:46Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:28:46Z | `dee-a07dea` | team_sessions | bash |
| 2026-11-26T04:28:49Z | `dee-17d6d3` | judge_credentials | bash |
| 2026-11-26T04:28:49Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:28:51Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:28:52Z | `dee-0f97d5` | platform_state | bash |
| 2026-11-26T04:28:54Z | `dee-17d6d3` | judge_credentials | bash |
| 2026-11-26T04:28:54Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:28:56Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:28:56Z | `dee-a07dea` | team_sessions | bash |
| 2026-11-26T04:28:57Z | `dee-17d6d3` | judge_credentials | bash |
| 2026-11-26T04:28:57Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:29:00Z | `dee-17d6d3` | judge_credentials | bash |
| 2026-11-26T04:29:00Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:31:23Z | `dee-17d6d3` | judge_credentials | bash |
| 2026-11-26T04:31:23Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:31:26Z | `dee-17d6d3` | judge_credentials | bash |
| 2026-11-26T04:31:26Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:33:26Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:33:32Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:33:36Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:33:53Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:33:53Z | `dee-a07dea` | team_sessions | bash |
| 2026-11-26T04:39:19Z | `dee-17d6d3` | judge_credentials | bash |
| 2026-11-26T04:39:19Z | `dee-17d6d3` | platform_state | bash |
| 2026-11-26T04:39:23Z | `dee-17d6d3` | platform_state | bash |
| 2026-11-26T04:39:25Z | `dee-17d6d3` | platform_state | bash |
| 2026-11-26T04:39:27Z | `dee-17d6d3` | platform_state | bash |
| 2026-11-26T04:39:31Z | `dee-17d6d3` | platform_state | bash |
| 2026-11-26T04:39:52Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:39:54Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:43:00Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:43:02Z | `dee-345d84` | judge_credentials | bash |
| 2026-11-26T04:43:02Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:43:38Z | `dee-b0c078` | platform_state | bash |
| 2026-11-26T04:43:42Z | `dee-b0c078` | platform_state | bash |
| 2026-11-26T04:43:44Z | `dee-b0c078` | platform_state | bash |
| 2026-11-26T04:43:49Z | `dee-07013f` | platform_import | bash |
| 2026-11-26T04:43:53Z | `dee-07013f` | platform_import | bash |
| 2026-11-26T04:44:04Z | `dee-b0c078` | platform_import | bash |
| 2026-11-26T04:46:42Z | `dee-17d6d3` | platform_state | bash |
| 2026-11-26T04:46:45Z | `dee-2180de` | platform_import | bash |
| 2026-11-26T04:46:56Z | `dee-17d6d3` | platform_state | bash |
| 2026-11-26T04:47:35Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:47:35Z | `dee-a07dea` | team_sessions | bash |
| 2026-11-26T04:48:03Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:48:15Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:48:26Z | `dee-07013f` | platform_import | bash |
| 2026-11-26T04:48:28Z | `dee-07013f` | platform_import | bash |
| 2026-11-26T04:49:00Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:49:02Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:49:07Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:49:20Z | `dee-a07dea` | platform_import | bash |
| 2026-11-26T04:50:39Z | `dee-17d6d3` | platform_import | bash |
| 2026-11-26T04:52:24Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:52:28Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T04:56:17Z | `dee-7be6aa` | platform_state | bash |
| 2026-11-26T04:57:33Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T04:57:37Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T04:57:46Z | `dee-7be6aa` | judge_credentials | bash |
| 2026-11-26T04:57:46Z | `dee-7be6aa` | platform_state | bash |
| 2026-11-26T04:57:49Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T05:00:27Z | `dee-2180de` | platform_state | bash |
| 2026-11-26T05:00:32Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T05:00:45Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T05:00:55Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T05:04:23Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T05:05:06Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T05:05:08Z | `dee-07013f` | platform_state | bash |
| 2026-11-26T05:07:56Z | `dee-ba3f0e` | platform_import | bash |
| 2026-11-26T05:08:10Z | `dee-2180de` | judge_credentials | bash |
| 2026-11-26T05:08:10Z | `dee-2180de` | platform_state | bash |
| 2026-11-26T05:14:05Z | `dee-2180de` | platform_state | bash |
| 2026-11-26T05:14:09Z | `dee-2180de` | judge_credentials | bash |
| 2026-11-26T05:14:09Z | `dee-2180de` | platform_state | bash |
| 2026-11-26T05:14:11Z | `dee-2180de` | platform_state | bash |
| 2026-11-26T05:14:14Z | `dee-2180de` | platform_state | bash |
| 2026-11-26T05:14:16Z | `dee-2180de` | platform_state | bash |
| 2026-11-26T05:14:18Z | `dee-2180de` | platform_state | bash |
| 2026-11-26T05:14:30Z | `dee-2180de` | platform_import | bash |
| 2026-11-26T05:14:38Z | `dee-2180de` | judge_credentials | bash |
| 2026-11-26T05:14:41Z | `dee-2180de` | judge_credentials | bash |
| 2026-11-26T05:14:43Z | `dee-2180de` | platform_import | bash |
| 2026-11-26T05:14:46Z | `dee-2180de` | judge_credentials | bash |
| 2026-11-26T05:14:48Z | `dee-2180de` | judge_credentials | bash |
| 2026-11-26T05:14:48Z | `dee-2180de` | platform_import | bash |
| 2026-11-26T05:15:22Z | `dee-2180de` | judge_credentials | bash |
| 2026-11-26T05:15:22Z | `dee-2180de` | platform_import | bash |
| 2026-11-26T05:29:12Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T05:29:14Z | `dee-345d84` | judge_credentials | bash |
| 2026-11-26T05:29:14Z | `dee-345d84` | platform_state | bash |
| 2026-11-26T05:29:16Z | `dee-345d84` | judge_credentials | bash |
| 2026-11-26T05:29:16Z | `dee-345d84` | platform_state | bash |

`dee-a07dea` 的 7 条 `team_sessions` 逐条时间（UTC）：04:28:25、04:28:28、04:28:31、04:28:46、04:28:56、04:33:53、04:47:35。两种来源（ACP mesh 日志与 DSH 原生会话）都在同一秒命中同一类，合并成 7 行。

### 3.3 说明

- 命中只到 worker、时间、类别；命令文本、路径内容与读到的值都不进本报告，也没有进过分析过程（脚本只做聚合，不打印原文）。
- 未命中的 2 个 worker（`dee-abcefb`、`dee-cf7eae`）在本窗口内没有这些形态。
- 检测器对工作树内的同名文件不报：本场所有 worker 工作树都在比赛目录下，扫描以每个 ACP 会话的 cwd 作为工作树传入，工作树内路径先剔除。
- 与聚类报告 §5.1 的 91 次不是同一个口径：那 91 是「内联 Python 脚本」的聚类计数，这里是「工具调用 × 类别」的去重计数，其中包含非内联脚本形态（如直接 `python3 -c`、`sqlite3`、`cat`）。

## 4. 两条全量测试与 swift build

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
741 passed, 19 skipped, 2 subtests passed in 73.99s (0:01:13)
```

```text
$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
494 passed in 62.51s
```

基线是 716 passed / 19 skipped 与 469 passed；新增的 25 条单测分别在 `cli/tests/test_worker_sandbox.py`（9 条）与 `cli/tests/test_worker_boundary.py`（16 条）。首次全量跑时 `runtime/tests/test_device_broker.py::test_case_timeout_releases_lock_and_requeues_the_rest` 失败一次（主计划 §2.3 记录的高负载偶发项，同一族的预算测试），单独重跑 19 passed，随后两次全量都全绿。

```text
$ swift build
Build complete! (1.52 sec)      # 含 Swift 改动的构建
Build complete! (0.26 sec)      # 提交前再跑一次的增量空构建
```

`Sources/Parhelion/Domain/AgentMeshModels.swift`、`AgentMeshNodeCards.swift`、`AgentMeshInspectorView.swift` 改了（新字段 + 两处徽标），两次构建都通过。

## 5. 单测覆盖（对照验收项）

| 验收项 | 测试 |
|---|---|
| 各 adapter 的启动环境里没有平台源码路径 | `test_worker_env_drops_platform_source_paths_and_private_variables`、`test_worker_env_drops_pythonpath_when_only_platform_entries_remain`、`test_adapter_launch_environment_has_no_platform_source_paths`、`test_every_headless_runner_filters_its_child_environment`（5 个 driver 全部验证走到 `worker_env`） |
| 沙箱配置渲染包含排除项（按驱动） | `test_seatbelt_profile_excludes_credential_stores`、`test_config_json_is_denied_only_when_it_carries_credentials`、`test_worker_codex_config_renders_permission_excludes`、`test_seatbelt_profile_really_denies_the_read_on_this_machine`（真执行） |
| worker 只能拿到自己的回环路由 | `test_worker_access_binds_one_scoped_route_and_sandboxes_the_process` |
| 检测器对样例事件的命中 | `test_detector_hits_each_documented_shape`（8 个形态）、`test_detector_reads_the_dsh_native_event_shape` |
| 不误报工作树内同名文件 | `test_detector_does_not_flag_a_worktree_local_same_name_file`、`test_detector_ignores_platform_tool_arguments` |
| 两种事件形态与去重 | `test_scan_reads_a_mesh_log_and_a_dsh_session`、`test_scan_dsh_attributes_a_session_to_its_worker` |
| 同类告警只发一次 | `test_same_worker_same_class_alerts_only_once`（两主控 + 两管理员消息、两条账本行，重复命中零新增） |
| 协作页标记、不自动停止 | `test_report_marks_the_collaboration_page_row`、`test_live_hit_never_stops_the_worker` |

## 6. NOTE（非显然的源码事实，供 aoci 条目）

1. **`sandbox(7)` 匹配真实路径**：macOS 上 `/var`、`/tmp` 是符号链接，profile 里的 filter 必须写 canonical 路径，否则静默不生效（`(allow default)` 下看起来"渲染正常"）。`worker_access` 既有的 `~/.ssh`、`daemon.json` 两条靠 `Path.home()` 恰好是真实路径才生效。
2. **DSH 沙箱只管写**：`dsh-bash-sandbox`/`dsh-fs-sandbox` 文档写明 `read-only`/`workspace-write` 只约束写，读不受限；DSH 权限面的三档是 preset（sandbox + approval），`sandbox/mode` 事件记录实际档位。所以 DSH 读排除没有配置面。
3. **DSH 原生会话里 `tool/call` 的 `data.arguments` 是 JSON 字符串**，不是对象；ACP 的 `rawInput` 在 `session/update` 的 `tool_call` 帧里。两者字段名不同，检测器分别处理。
4. **Codex 的 TOML 顶层键顺序**：任何 `key = value` 出现在第一个 `[table]` 之后都会落进那张表。所以渲染顺序必须是「顶层标量（含 `default_permissions` 与 provider 设置）→ 各 `[table]` → mcp 工具表」，写成"先拼 mcp 再往前插"会把 `model_providers` 吞进 permissions 表（本包踩过并修掉）。
5. **分离式启动的 `PYTHONPATH` 是必需的**：`launch_session` 起的是 `python -m opsb_core.drivers.registry`，没有 `cli/` 就起不来。所以那条路径只能靠「套 sandbox + 子进程再清洗」，不能把 `PYTHONPATH` 直接删掉。
6. **平台源码对 worker 仍可读**（同进程树的 MCP server 要读），所以 import 边界靠环境清洗 + 检测，不靠禁读；`~/.local/bin/opsb` 启动器本身算合法入口（brief 写明 CLI 兜底），检测器按文件名排除。
7. **检测只看工具输入，不看结果**：worker 把读到的内容写进文件再 `read` 回来的形态，或者用后台进程（`job_output`）间接读，检测不到。
8. **同一秒内的多次同类命中会合成一条**：ACP 行时间戳是微秒、DSH 是毫秒，跨源去重只能到秒；告警本身按 worker×类别一次，所以不影响告警口径。

## 7. 风险与后续

- **Codex permission profile 未在真实二进制验证**：本机没装 `codex`，`render_worker_permissions` 的 `[permissions]` 块只在 TOML 解析层验证过。旧版本会忽略它（或被每回合的 `sandboxPolicy` 覆盖），所以纵深防线是平台 sandbox；若合入后真机上 Codex 因未知键拒绝启动，删掉 `render_worker_permissions` 的调用即可，其余层不受影响。
- **DSH 与 gemini/pi 的读边界只有平台 sandbox 一层**：靠 seatbelt，依赖 macOS（`prepare_worker_command` 本来就在非 darwin 上直接报错）。Linux 上没有等价实现，只能靠检测。
- **`config.json` 条件 deny 的代价**：一旦文件里出现凭据形状的键，worker 侧 MCP server 读不到真实配置，`parhelion_status`/`queue` 的阈值退化为默认值。这是有意的取舍（宁可退化也不泄漏），但值得在 4.2.0 发布说明里提一句：凭据应放 `cj_accounts.json`，不要写进 `config.json`。
- **P08 的 `brief.py` 冲突**：见 §2.4，合并时删我的一行、语义移进 P08 契约段。
- **不自动停止**：告警只提示，管理员要在协作页或经 `parhelion_agent_stop` 决定；如果希望"连续同类命中自动转 waiting_input"，需要另立需求（会影响 4.2 的 worker 可用性口径）。
- **检测器是词法规则**：`sys.path` + 平台目录、四类路径、`_all_team_sessions` 之外的新绕行（例如通过 `pip show`/`importlib` 定位平台根后再 import）可能漏；补规则比补机制便宜，后续遇到新形态加到 `worker_boundary.classify_text` 即可。
- 建议后续：把 `boundary_flags` 也放上 `opsb status`/协作页横幅（像 `DriverSuspensionBanner` 那样），让管理员不点开 worker 也能看见。
