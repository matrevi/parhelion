# P15 worker 边界（4.2 K，安全）

分支：`impl/P15-worker-boundary`。在 P12 合入后的 `main` 上开发，依赖 `parhelion_report(view=board)`。

## 事件

本场有 worker 在自己的工作树里导入平台内部模块，用 `board_refresh_service._all_team_sessions()` 读出全队判题账号的会话 cookie，再去查排行榜（`docs/v4.2/00-PLAN.md` §1）。worker 能拿到平台的内部实现和凭据。

## 设计

1. 进程环境：
   - worker 进程（`agents/adapters.py` 的各 adapter 启动路径、`drivers/registry.py` 的分离式启动）的 `PYTHONPATH` 不包含平台源码目录：仓库根、`cli/`，以及 `.app` 包内的 `Contents/Resources/{cli,scheduler,runtime}`；
   - 工作目录为工作树；
   - 不继承 `PARHELION_HOME` 之外的平台私有变量。
   - 同时核对 `worker_access.py`：worker 只能拿到自己的回环 MCP 路由。
2. 可读路径：驱动支持沙箱时，把 `~/.parhelion/cj_accounts.json`、`~/.parhelion/sessions/`、各账号会话文件，以及 `~/.parhelion/config.json` 中的凭据段排除在 worker 的可读范围之外。Codex 有沙箱配置（`drivers/codex_appserver.py` 的 config 渲染）；DSH 有 `sandbox/mode` 事件，你需要核实其配置面。逐个驱动说明能做到哪一步；做不到的，靠第 3 条检测兜底。
3. 检测与告警：
   - 扫描 worker 的工具调用事件，包括 ACP 的 `tool_call.rawInput` 与 DSH 原生会话的 `tool/call`；
   - 发现读取凭据路径、导入 `opsb_core`/`scheduler` 内部模块，或引用 `_all_team_sessions` 这类函数时：
     - 向主控与管理员各发一条告警（总线 `platform` 消息，同一 worker 同一类告警只发一次）；
     - 在协作页标记该 worker；
     - 记一条账本 `constraint`。
   - 不自动停止 worker，是否停止由管理员决定。
4. 数据通道：worker 需要的榜单、tbest 与回执全部经 MCP 提供，即 P12 的 `parhelion_report` 的 board、history、per_case 视图。在 worker brief 与工具描述里写明这一点。

## 验收

- 单测：
  - 各 adapter 的启动环境里没有平台源码路径；
  - 沙箱配置渲染包含排除项（按驱动）；
  - 检测器对样例事件的命中，以及不误报，例如读取工作树内的同名文件；
  - 同类告警只发一次。
- 回放口径：用本场流水（只读）运行检测器，列出所有命中（worker、时间、内容摘要），写入 `docs/plan-4.1-4.2/reports/P15-boundary-scan.md`。本场已知的 `_all_team_sessions` 事件必须被检出。
