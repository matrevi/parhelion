# 11 · 控制面 MCP 与异步测试（工作包 A）

> 索引：[10-DETAILED-INDEX.md](10-DETAILED-INDEX.md) · 分支：`v4/a-mcp`

## 1. 要解决的问题

- `tool_parhelion_test_plan(execute=true)` 用 `subprocess.run` 一直阻塞到测试结束（见 `cli/opsb_core/mcp/handlers_bench.py`）。Harness 的工具超时是 60 秒，结果 604 次调用里 320 次超时，worker 随后重试，造成重复 run。
- `serve_stdio`（`cli/opsb_core/mcp/server.py`）逐行同步处理请求；worker 身份的请求经 `forward_worker_request` 转发到 daemon 并等待，且没有超时。一个长调用会挡住同一会话后面所有的 status / squad / inbox 请求。
- `status` 平均返回 13KB，`kb_query` 平均返回 40KB，挤占 worker 的上下文。
- 传入不存在的 SHA 时，报错只有一句 `Cannot resolve …`，worker 会继续编造 SHA。

## 2. 改动范围

修改：`mcp/server.py`、`mcp/tools.py`、`mcp/schemas.py`、`mcp/handlers_bench.py`、`mcp/handlers_kb.py`。
新建：`services/test_runs.py`、`cli/tests/test_test_runs.py`。
补充测试：`cli/tests/test_mcp_server.py`。

## 3. stdio 并发分发（`server.py`）

1. `serve_stdio` 建一个 `ThreadPoolExecutor(max_workers=8)` 和一把写锁。
2. 每读到一行、解析成功后，把"处理（转发或本地执行）+ 写回复"整体提交给线程池。写回复时持有写锁，保证每条回复完整占一行。
3. 回复可以乱序返回，JSON-RPC 用 `id` 对应请求。没有 `id` 的通知照旧不写回复。
4. 解析失败的行仍在读线程里直接写错误回复（同样持锁）。
5. 输入流结束后调用 `executor.shutdown(wait=True)`，等已提交的请求全部写完再返回 0。
6. `initialize`、`tools/list` 这类请求也走线程池，不做特殊处理。

测试（`test_mcp_server.py`）：用假的 handler，让第一条请求 sleep 1 秒、第二条立即返回，断言第二条回复先写出，且两条都完整。

## 4. 测试运行登记表（`services/test_runs.py`）

记录格式、位置和对外函数见索引 §3.4。

### 4.1 `submit(ws, *, problem, sha, level, cases, remeasure, build_only, agent_id, role) -> dict`

1. 用 `handlers_bench._problem_commit` 解析题目和 SHA。为避免循环导入，把这个函数移到 `test_runs.py`，`handlers_bench` 改为从这里导入。解析失败时抛出 `ValueError`，消息里附上该题仓库最近 5 个提交（`sha7 + 标题`，用 `git log -5 --format=%h %s`），提示 worker 从中选择。
2. `level` 只能是 `smoke`、`standard`、`final`，默认 `standard`；`cases` 是非空字符串列表或空列表。`build_only=true` 时把 level 记为 `build`。
3. **去重键**：`(title, sha, level, tuple(sorted(cases)), build_only)`。
   - 如果已有相同键的记录处于 `queued` 或 `running`，并且进程还活着（`os.kill(pid, 0)`），直接返回它，并带上 `joined: true`。
   - 如果已有相同键的记录在 30 分钟内以 `finished` 结束，且 `exit_code` 为 0，同时 `remeasure` 为 false，直接返回它，并带上 `reused: true`。
4. 否则新建记录（`status=queued`），然后拉起后台进程：
   - 命令：`[resolve_python_exe(ws), CLI_DIR/"opsb", "test", title, "--workspace", ws, "--sha", sha, "--level", level, *("--case", c), *("--remeasure"), *("--build-only")]`；
   - `cwd=ws`，`stdin=DEVNULL`，stdout 和 stderr 一起写到 `test_runs/<request_id>.log`，`start_new_session=True`；
   - 环境变量继承当前进程；`role == "worker"` 时设 `PARHELION_MCP_ROLE=worker`。
5. 写入 `pid`，把状态改为 `running`。再起一个守护线程 `proc.wait()`，进程结束后调用 `_finalize`。
6. 返回 `public_view(record)`（见 4.4）。

登记表写入一律用临时文件加 `os.replace`；同一个 `request_id` 的读改写用 `fcntl.flock` 锁住 `<request_id>.lock`。

### 4.2 `_finalize(ws, record)`

1. 从日志末尾往前找最后一个能解析成 JSON 对象的行，取 `run_id`、`exit_code`、`digest`、`report`、`reused`、`joined`、`deferred`、`reason`。
2. 状态：找到 JSON 且 `exit_code` 为 0 或 2 时记为 `finished`；其他情况记为 `failed`，并把日志最后 1500 字符写进 `error`。
3. 写入 `finished_at`。

### 4.3 `refresh(ws, request_id) -> dict`

读取记录。状态是 `running` 时：
- 进程已经不在了（daemon 重启过，守护线程丢失），立即调用 `_finalize`；
- 进程还在，就扫描日志里的 `OPSB_RUN_STARTED` 行，拿到 `dir` 后读取该目录下 `events.jsonl` 的最后一个事件，生成 `progress`：`{"phase": 最后事件名, "cases_done": case 事件数, "selected": n, "wait_sec": device_batch 里的值（如果有）}`。

### 4.4 `public_view(record) -> dict`

返回给工具调用方的视图，序列化后不超过 2.5KB：`request_id, problem, sha7, level, status, run_id, exit_code, progress, digest, report, joined, reused, reason, error(≤600 字符)`。

### 4.5 其他函数

- `wait(ws, request_id, wait_sec) -> dict`：`wait_sec` 限制在 [1, 45]，每 1 秒调用一次 `refresh`，结束或超时就返回 `public_view`。如果返回时状态已结束，把 `observed` 记为 true。
- `cancel(ws, request_id) -> dict`：对进程组发 SIGTERM，5 秒后仍在则发 SIGKILL，状态记为 `cancelled`。
- `list_undelivered`、`mark_delivered`、`compact_line`：见索引 §3.4。`compact_line` 的格式为 `"{problem} {sha7} {level} {status} {passed}/{total}" + 可选的 " · 慢例 {id} {min_us}µs" + 可选的 " · 相对基线 ×{geomean}"`。
- `gc(ws, keep_days=3)`：删除结束超过 3 天的记录及其日志。每次调用 `submit` 时顺带执行，最多每小时一次。

## 5. MCP 工具

### 5.1 新工具（`schemas.py` + `tools.py` + `handlers_bench.py`）

| 名称 | 角色 | 参数 | 行为 |
|---|---|---|---|
| `parhelion_test` | 两种角色 | `problem`(必填)、`sha`(默认 HEAD)、`level`(`smoke`/`standard`/`final`，默认 `standard`)、`cases`、`remeasure`、`build_only` | 调用 `test_runs.submit`，2 秒内返回 |
| `parhelion_test_wait` | 两种角色 | `request_id`(必填)、`wait_sec`(默认 30，最大 45) | 调用 `test_runs.wait` |
| `parhelion_test_cancel` | 两种角色 | `request_id` | 调用 `test_runs.cancel`；worker 只能取消自己提交的（比对 `agent_id`） |

- worker 调用时，`agent_id` 取 `_caller_agent_id`，role 记为 `worker`；orchestrator 调用时 agent_id 为空。
- 工具描述要短，并写明：先提交，再用 wait 取结果；结束时结果也会出现在收件箱里。

### 5.2 `parhelion_test_plan`（兼容别名）

- `execute=false`：返回不执行的计划，形状保持不变，只在 `command` 里把旧参数换成 `--level`。
- `execute=true`：先 `submit`，再 `wait(40)`，返回 `public_view`，并加一条说明 `"deprecated": "use parhelion_test + parhelion_test_wait"`。
- `mode` 的映射：`build-only` → `build_only`，`fast` → `smoke`，`full` → `standard`。`suite`：`adaptive` → `smoke`，`all` → `standard`。
- worker 传入 `profile` / `experiment` / `timing` / `repeats` / `bucket` 时，直接返回 ValueError，消息为 `"Workers select a level (smoke|standard|final); the platform owns the suite"`。orchestrator 可以继续通过 `execute=false` 看到这些参数的命令拼接。
- `tools.py` 里原来的 `call_args["_worker_request"]` 逻辑保留，新工具同样设置。

### 5.3 输出上限

- `parhelion_status`：
  - 默认 `include_queue=false`，`queue_preview` 最多 5 条，每条只保留 `title, sha7, status, estimated_score, delta_expected`；
  - `problems` 每条只保留 `title, status, current_score, best_score, leader_score, rank, head_sha, best_sha`；
  - 新增参数 `verbose=true` 时返回原来的结构。
- `parhelion_kb_query`（`handlers_kb.py`）：
  - 默认 `limit=3`；每条只保留 `id, pillar, title, confidence, tags` 和不超过 600 字符的 `summary`（取原来的摘要字段，没有就截正文）；
  - `full=true` 时返回原来的结构。
- `parhelion_bench_diagnose`：`high_residual_events` 最多 3 条，`coverage_grid` 只在 `verbose=true` 时返回。

### 5.4 编排类工具的 schema 改动（由 C 实现处理逻辑）

- `parhelion_sprint_start`：增加 `mode`，枚举 `solo | dual | full`，默认 `solo`，描述写"solo 每题 1 个 worker；dual 增加 algorithm/dataflow 双轨；full 再加 bench-author"。
- `parhelion_squad`：`recipient` 的描述改为"agent_id、队友角色名（如 algorithm、dataflow、bench-author）或 all；不传参数时返回队友列表"。

## 6. 验收测试

`cli/tests/test_test_runs.py`（用 `tmp_path` 构造工作区和假的 `opsb` 脚本，脚本按 §3.1 的协议打印输出后 sleep）：

1. `submit` 在 2 秒内返回，状态为 `running`。
2. 同一个键再提交一次，返回同一个 `request_id`，且 `joined` 为 true。
3. `wait` 拿到 `finished`，`digest` 原样透传，`observed` 变为 true。
4. 30 分钟内重复提交，返回 `reused`；带 `remeasure` 则新建记录。
5. 模拟守护线程丢失（直接把 pid 改成一个已结束的进程号），`refresh` 能正确收尾。
6. `cancel` 后状态为 `cancelled`，进程组已退出。
7. SHA 无效时，报错消息里包含最近的提交列表。
8. worker 调用 `test_plan` 时传 `profile`，被拒绝。
9. `status` 的默认输出序列化后不超过 2KB（用包含 14 道题的快照样本）。

`test_mcp_server.py`：§3 的乱序测试；worker 转发路径用假的 `forward_worker_request` 验证并发。

## 7. 不要做的事

- 不要修改 `agents/*`、`handlers_agents.py`，也不要修改 `case_bench.py`。
- 不要删除 `parhelion_test_plan`。UI 和旧的提示词还在使用它。
