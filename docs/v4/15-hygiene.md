# 15 · 卫生与清理（工作包 D）

> 索引：[10-DETAILED-INDEX.md](10-DETAILED-INDEX.md) · 分支：`v4/d-hygiene`

## 1. 要解决的问题

- pytest 往真实的 `~/.parhelion/workspaces/` 写入了 133 个目录（`ws-*`、`bus.db-*`），它们的 `meta.json` 指向 `/private/var/folders/.../pytest-of-*/...`。原因是测试没有把 `PARHELION_HOME` 隔离到临时目录。
- `SubmissionGate.evaluate` 每次评估都往 `results/feedback-log.jsonl` 写一行；状态刷新很频繁，结果第二场比赛累计了 43,039 条 gate 记录，文件达到 16MB。
- 设备主机的 `/tmp/opsb-npu-tickets/` 留有死票；这些目录没有清理入口。

## 2. 改动范围

修改：`conftest.py`、`scheduler/gate.py`（只改 `evaluate` 里的日志部分）、`scheduler/feedback_log.py`。
新建：`cli/opsb_core/commands/doctor_cmd.py`、`cli/tests/test_doctor.py`。
补充测试：`scheduler/tests/test_feedback_log.py`、`scheduler/tests/test_gate_log_verdicts.py`。

## 3. 测试隔离（`conftest.py`）

- 在 import 阶段执行：如果环境变量里没有 `PARHELION_TEST_KEEP_HOME`，就用 `tempfile.mkdtemp(prefix="parhelion-test-home-")` 创建目录，设置 `PARHELION_HOME` 和 `OPSB_HOME` 都指向它，并用 `atexit` 注册删除。
- 保留现有的 `OPSB_DEVICE_ISOLATED`。
- 检查 `cli/opsb_core/config.py` 是否在 import 时就把 `PARHELION_HOME` 读成常量。如果是，确认 conftest 在任何被测模块 import 之前执行。pytest 会先加载根目录的 conftest，所以一般没问题，但要在测试里验证：加一个测试断言 `config.PARHELION_HOME` 位于临时目录下。
- 跑全量测试前后对比 `~/.parhelion/workspaces/` 的目录数，确认没有新增。在汇报里写出这个对比结果。

## 4. 网关日志节流（`gate.py` + `feedback_log.py`）

1. `feedback_log.py` 新增 `log_feedback_on_change(workspace, kind, key, signature, **fields) -> bool`：
   - 状态文件：`<workspace>/results/.feedback-signatures.json`，内容为 `{kind: {key: signature}}`，读改写时用 `fcntl.flock` 锁住 `<同名>.lock`；
   - `signature` 与上次记录的相同就返回 False，不写日志；不同则调用 `log_feedback`，更新状态文件，返回 True。
2. `gate.evaluate` 改用 `log_feedback_on_change`：
   - `key = f"{title}:{sha7}"`；
   - `signature = json.dumps([status_tag, approved, [s.code for s in stages], gate_reason], sort_keys=True)`。
3. `log_feedback` 增加日志轮转：写入前如果文件超过 8MB，就把它改名为 `feedback-log.1.jsonl`（覆盖旧的 `.1`），再新建文件写入。
4. 读取函数（`tail_feedback` 等）的行为不变。

## 5. 清理命令（`commands/doctor_cmd.py`）

导出 `register_doctor_subcommands(sub)` 和 `run_doctor(args) -> int`。集成方会在 `cli_app.py` 里加一行注册调用，D 不要修改 `cli_app.py`。

```
opsb doctor clean [--apply] [--remote] [--agents [--include-errored]] [--logs DAYS] [--logs-max-mb MB] [--purge-trash] [--json]
```

- 默认只列出、不动手。`--apply` 才执行，并且是移动而不是删除。
- **本地**：
  - 遍历 `PARHELION_HOME/workspaces/*`。满足以下任一条件的目录视为污染：名字以 `bus.db-` 开头；`meta.json` 里的 `workspace` 路径包含 `/pytest-of-`；或者该路径不存在且目录名以 `ws-` 开头。
  - `--apply` 时移动到 `PARHELION_HOME/trash/<UTC 时间戳>/`。
  - 当前配置里登记的工作区（`config.json` 的 `workspaces` 和 `active_workspace`）对应的目录一律跳过。
- **远端**（`--remote`）：
  - 通过 `ssh -o BatchMode=yes <npu_ssh_host>` 执行一段 Python，列出 `/tmp/opsb-npu-tickets/*.json` 中 pid 已不存在的票据；
  - `--apply` 时删除这些票据。只动 pid 不存在的票据，不碰锁文件。
- **Agent 沙箱**（`--agents`）：
  - 每个 worker 会拿到 `workspaces/<id>/agents/<agent_id>/`，里面是一份独立的 `codex_home`（含一份 Codex 应用副本，约 100 MB）。worker 结束后 `AgentBusStore.cleanup_stopped` 只删数据库记录，沙箱目录会一直留着，实测 175 个目录累计 1.0 GB。
  - 判定口径与 `cleanup_stopped` 完全一致：`status IN ('stopped','error')` 且 `pid`/`pgid` 都已清空；另外把数据库里**没有任何记录**的沙箱也算可回收（同名孤儿）。数据库不可读时一律只列出、不移动。
  - `error` 状态的席位默认只列出、不移动（原因记为 `agent error (resumable)`）：显式 followup 可以让它续跑，Codex 续跑还要用沙箱里的 `codex_home`，移走就断了。确认不再续跑时，加 `--include-errored` 一并回收。
  - 登记工作区的**状态目录本身仍然不碰**，只回收它 `agents/` 下已被证明结束的沙箱——否则最需要清理的恰好是正在用的那个工作区。
  - `--apply` 时移动到 `PARHELION_HOME/trash/<UTC 时间戳>/agents/<workspace 目录>/<agent_id>`。
- **会话日志**（`--logs DAYS`、`--logs-max-mb MB`）：
  - 遍历 `PARHELION_HOME/logs/sessions/*`，回收超过 `DAYS` 天（默认 14）的驱动会话流水。
  - 单靠窗口不够：实测 176 个文件 125 MB 里 162 个、120 MB 都是最近 24 小时产生的，所以再加一条总量封顶——按 mtime 从新到旧装满 `--logs-max-mb`（默认 100 MB，`0` 表示关闭），装不下的更旧文件一并回收。
  - 这些是平台自己写的诊断 telemetry，与远端死票同类，所以 `--apply` 直接删除而不进 trash；用户数据（工作区目录、agent 沙箱）仍然只移动。
- **Trash**（`--purge-trash`）：
  - 列出 `PARHELION_HOME/trash/*` 各批次及占用；`--apply` 时清空。这是唯一会删除 trash 的操作，且必须显式指定，用于回收上面两步腾出的空间。
- 输出：人类可读的表格；`--json` 时输出 `{"local": [...], "remote": [...], "applied": bool}`，并在带对应开关时追加 `agents` / `logs` / `trash_purge` 三个数组。

## 6. 验收测试

1. `test_doctor.py`：在临时的 `PARHELION_HOME` 里构造 4 类目录（正常、`bus.db-`、pytest 路径、已登记）。dry-run 时一个都不移动；`--apply` 后只有两个被污染的目录被移到 trash，已登记的保留。远端部分 mock 掉 `subprocess.run`，断言生成的命令包含 `BatchMode=yes`，并且 dry-run 时远端脚本不包含删除动作。
2. `test_feedback_log.py`：相同签名写两次只记一行；签名变化后再记一行；超过阈值时会轮转（测试里用 monkeypatch 把阈值调小）。
3. `test_gate_log_verdicts.py`：对同一个候选连续评估 3 次，日志里只有 1 条 gate 记录。
4. 隔离测试：断言 `config.PARHELION_HOME` 不等于 `Path.home()/".parhelion"`。
5. Agent 沙箱：用真实 `_SCHEMA_SQL` 建库，放入 finished / error / running 三条记录外加一个无记录孤儿。dry-run 一个都不动；`--apply` 只移走 finished 与孤儿，error 与 `running` 原地保留；加 `--include-errored` 后 error 也被移走，工作区 `state/` 必须仍在；数据库删掉后所有沙箱都只能列出、不能移动。
6. 会话日志：三个不同 mtime 的 `*.jsonl`，dry-run 不动文件，`--apply` 只删超过窗口的两个，`--logs` 不带参数时等于默认 14 天；另用独立 `PARHELION_HOME` 验证总量封顶——4 个 400 KB 文件配 1 MB 预算时，只保留最新的两个，`--logs-max-mb 0` 关闭封顶。
7. Trash：dry-run 只列批次与占用，`--apply` 后 `trash/` 为空目录。
8. 权限：`ConfigRepository.save()` 落盘后 `config.json` 必须是 `0600`；从 `~/.config/parhelion` 迁移过来的同名文件同样是 `0600`。

## 7. 不要做的事

- 不要真的删除任何用户数据，只能移到 trash。这里的"用户数据"指工作区内容、bench-specs、agent 沙箱与任何用户可能改过的东西；平台自己写的诊断流水（`logs/sessions`）与远端死票同类，可以按保留窗口删除，但必须由 `--logs`／`--remote` 显式要求。
- 清理 trash 只能用 `--purge-trash`，不能顺手在别的分支里删。
- 不要修改 `cli_app.py`。
