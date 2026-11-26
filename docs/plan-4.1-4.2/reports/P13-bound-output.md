状态：已交付

# P13 统一输出上限 `bound_output`（4.2 E）

日期 2026-11-26，分支 `impl/P13-bound-output`，起点 `main` `a48b3c9`（含 P12、P14、P18）。子计划里的两块内容（统一出口、分页、回读、两种格式、计量、回收）与并入的两条审查跟进项（`handlers_report` 拆分、`peer/` 进 doctor）都已完成，没有未做项。

没有改 `aoci.txt` / `aoci.meta.txt` / `aoci.code.txt` / `.aoci/`；没有调用 `aoci_maintain` / `aoci_update_entry`；没有重启或停止守护进程、没有 `daemon reload`、没有派发或停止 worker、没有清理总线、没有 `queue approve` / `force`、没有向判题站提交、没有改 `auto_consumer_enabled`；额度没有进入任何门禁阈值或暂缓理由。改动只在工作树里，未合入 `main`、未 push。

## 1. 分支与提交

```text
$ git log --oneline main..HEAD
081f60a List spill and peer retention in opsb doctor clean
46b9855 Bound every MCP tool reply with paging and spill files
27d3516 Split the report views out of handlers_report
```

本报告随最后一个 `Deliver P13:` 提交进入分支，因此上面三条是本报告提交前的列表。

## 2. 逐项完成情况

### 2.1 唯一出口与小结果（已完成）

`mcp/server.py` 的 `tools/call` 仍是唯一出口：stdio（`serve_stdio`）、守护进程 HTTP（`/v1/mcp`、`/v1/mcp/worker/<id>`）与 worker 回环最终都走 `handle_jsonrpc_message`。现在结果先由 `mcp/bound.py:bound_output` 处理再进 `mcp_meter.record_call`：

- 序列化后 ≤ `mcp.output_cap_bytes`（默认 5000，`config.json` 的 `mcp` 段可配）的回复原样返回，`bound_bytes == result_bytes`；
- 超过上限时先尝试分页（2.4），只有连一页都放不下才落盘（2.2）。

`DEFAULT_CONFIG` 新增 `"mcp": {"output_cap_bytes": 5000, "spill_keep_days": 7, "spill_max_mb": 200}`；每个键单独取默认值，用户配置只写其中一个键时其余仍是默认。

### 2.2 落盘（已完成）

超过上限且不可分页的结果写到 `<workspace state>/spill/<agent_id>/<ts>-<tool>.json`（`<ts>` 为 UTC 微秒时间戳；主控调用没有 `agent_id` 时目录名用 `orchestrator`），返回：

```text
{spilled: true, path, total_bytes, head, tail}
```

- `path` 是绝对路径，文件内容就是完整的紧凑 JSON，`total_bytes` 等于文件字节数；
- `head` 是按 JSON 结构重建的摘要（对象/数组/标量，不是字符串前缀）：先整份保留顶层标量与短列表（≤240 B），再把剩余预算给长列表的前若干项、嵌套对象里的标量、截断的长字符串，未进入摘要的键列在 `_more_keys`；
- `tail` 是序列化文本的尾部（约 1 KB），在 JSON 转义序列之外切分，末尾的错误信息因此保留。

工作区状态目录用的是 `drivers.base.resolve_workspace_state_dir`——`services/test_runs.registry_dir` 正是 `resolve_workspace_state_dir(ws) / "test_runs"`，没有另拼路径。

### 2.3 失败保留尾部（已完成）

工具抛异常时 server 仍把 `{"ok": false, "tool", "error"}` 交给同一个 `bound_output`：超限落盘后 `head` 里能看到 `ok: false`，`tail` 里保留错误末尾（单测用 `RuntimeError` 造 4 KB 前缀 + `boom-tail` 验证）。落盘本身失败（例如磁盘满）时不抛回调用方，回复退化为 `{spilled: false, truncated: true, spill_error, total_bytes, head, tail}`。

### 2.4 分页（已完成）

新模块 `mcp/paging.py` 提供 `offset` / `limit` / `cursor` 三种走法与统一的 `page: {offset, limit, total}` + `next_cursor`。`cursor` 是上一页的 `next_cursor`（`o<offset>`），优先于 `offset`，非法 cursor 抛 `ValueError`。覆盖子计划点名的全部工具：

| 工具 | 分页对象 | 默认页 | 说明 |
|---|---|---|---|
| `parhelion_agent_messages` | `messages` | 60（上限 500） | 多取一行判断是否还有下一页；`offset>0` 的页不再 ack（没展示的行必须保持未读） |
| `parhelion_kb_query` | `results` + `entries` | brief 3 / full 8（上限 50） | 一次多取一行；`anti_patterns` 不翻页，按页大小截断；`result_count` / `anti_pattern_count` 反映本页 |
| `parhelion_ledger` | `lines` | 20（上限 40） | `shown` 仍是本页行数，`matched` / `total` 是整视图计数 |
| `parhelion_report` history | `receipts` | 10（上限 50） | 按“最新在前”排列，`offset` 从最新往回数 |
| `parhelion_report` queue | `items` | 20（上限 50） | 与 `opsb queue list --json` 同源；`count` / `by_kind` 仍是整队计数 |

“先分页再落盘”在出口处再兜一层：可翻页工具即使 handler 这一页仍超上限，`paging.repage` 会把这一页缩到放得下并重算 `next_cursor`，只有单行本身就超上限才落盘。单测用 `output_cap=700` 把 35 行 ledger 逐页走完，断言 35 条无重复无遗漏且没有产生 spill 文件。

### 2.5 回读（已完成）

新工具 `parhelion_spill_read(path, offset?, limit?)`：

- worker 只能读 `<spill>/<自己的 agent_id>/` 下的文件（`_worker_request` + `_caller_agent_id`，符号链接解析后再做包含判断）；主控可读整个 spill 根；
- 返回 `{path, total_bytes, offset, limit, text, next_offset}`，按字节窗口读，单窗上限 `output_cap - 700`，`next_offset` 走到文件尾为 `null`；
- 回读结果被列入 `NO_SECOND_SPILL`，`bound_output` 对它只做长度检查、不再落盘；单测断言读完后 spill 目录里的文件数不变。

### 2.6 两种格式（已完成）

| 入口 | brief（默认） | full |
|---|---|---|
| `parhelion_status(format=)` | 现有紧凑结构（每行一题、`queue_preview` 最多 5 条） | 与 `verbose=true` 相同（`format` 会先在 `_status_args` 归一化成 `verbose` 再算 `rev`） |
| `parhelion_kb_query(format=)` | 3 条、每条摘要 600 字符（保留 `KB_DEFAULT_LIMIT` / `KB_SUMMARY_LIMIT`） | 完整条目，默认 8 条；`full=true` 仍是同义的老写法 |
| `parhelion_test_wait(format=)` | 现有 `public_view`（约 2.5 KB 的自缩 digest） | 完整记录（`public_view(record, full=True)`，不缩 digest、不截 error），仍过 `bound_output` |
| 测试结果消息 | 现有 `compact_line` 一行 | `parhelion_test(format=full)` 把 `result_format` 记进 run 记录，投递时是“一行摘要 + 完整记录的 bounded 文本”；超上限时就是 spill 信封，worker 用 `spill_read` 取回 |

`parhelion_status` 的 `rev` / `if_rev` 语义不变：带 `format` 的调用按归一化后的参数算 rev（`format=full` 与 `verbose=true` 同 rev），不带 `format` 的老调用 scope 与以前逐字相同；`if_rev` 命中仍返回 `{not_modified: true, rev}`。

### 2.7 计量（已完成）

`metrics/mcp_meter.record_call` 新增 `spilled` 与 `bound_bytes` 两个字段（缺省参数，旧调用不受影响）。行格式变为：

```text
{ts, agent_id, role, tool, args_bytes, result_bytes, ms, is_error, spilled, bound_bytes}
```

`result_bytes` 是调用方实际收到的（bounded 之后的）字节；`bound_bytes` 是完整的 pre-bound 回复，两者之差就是上限省掉的字节；没有落盘/缩页时两者相等。

### 2.8 回收与 `opsb doctor clean`（已完成）

- `bound.reap`：每次写新 spill 后按“7 天 / 200 MB，新的先留”回收该工作区的 spill 文件（可经 `mcp.spill_keep_days` / `mcp.spill_max_mb` 配置），返回删除的路径；
- `opsb doctor clean` 新增 `retention` 段：扫 `<PARHELION_HOME>/workspaces/*/state/{spill,peer}`，用同一策略列出过期文件（`workspace` / `section` / `reason` / `age_days` / `size_bytes` / `applied`）。**默认只列不删**；`--purge-retention --apply` 才删。peer 的附件、`quotes/`、`index.jsonl` 与 spill 一起列出，但它们不会被自动回收（附件是消息正文，后续回读可能还引用），这一点写在命令自述里；
- `--json` 输出里同样有 `retention`，`_print_table` 有对应段落。

### 2.9 并入的审查跟进项（已完成）

- **`reviews/P12.md` 第 1 项**：`mcp/handlers_report.py` 拆成 `handlers_report.py`（入口、角色、`per_case`、`diff`）与 `report_views.py`（`history`、`queue`、`board`、`bottleneck`、`env` 与共用的整形 helper），两处都按常规格式书写，最长行 < 130 字符。拆分与行为无关：拆分提交 `27d3516` 里用临时对照模块在 P12 fixture 上逐字段比较了 `history`（有/无 `problem`、带 `limit`）、`board`、`env`、`per_case`、`diff`，输出完全相同（对照代码未进仓库）。共享 helper 现在定义在 `report_views`，由 `handlers_report` 重新导出，现有测试仍从 `handlers_report` 导入。
- **`reviews/P12.md` 第 2 项**：history / queue 改为分页，取代原先的 4 KB 截断（`_trim`）。其余视图仍保留 `per_case`/`diff` 4 KB、`env` 1 KB 的预算。
- **`reviews/P14.md` 第 1、5 项**：spill 与 `<workspace state>/peer/`（附件、`quotes/`、`index.jsonl`）一起进 doctor 列表，默认只列不删。
- `reviews/P12.md` 第 3 项（`_docs()` 跨题）会改行为，按要求不做；第 4、5 项也不做。

### 2.10 工具计数（已完成）

新工具 `parhelion_spill_read` 三处登记：`mcp/schemas.py`（schema 与 `roles: ("orchestrator","worker")`）、`mcp/tools.py`（`TOOL_HANDLERS` 与 `__all__`，并加入 `ROLE_AWARE_TOOLS` 以拿到 `_worker_request`）、角色（schema 里显式声明）。计数 27 → 28，更新了 `cli/tests/test_agent_mesh_http.py`（1 处）与 `cli/tests/test_mcp_server.py`（2 处）。没有为 P08 的 `parhelion_agent_peek` 预留位置（它会自己从 28 改成 29）。

## 3. 测试

两条全量测试都在工作树内跑，末行：

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
736 passed, 19 skipped, 2 subtests passed in 69.98s (0:01:09)
```

```text
$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
489 passed in 56.78s (0:00:56)
```

基线（`main`，P12/P14/P18 合入后）是 716 passed / 19 skipped 与 469 passed，本包各 +20。新增测试在 `cli/tests/test_bound_output.py`（20 条），覆盖子计划验收的全部项：

- 小结果原样返回（`bound_output` 与 `handle_jsonrpc_message` 各一条）；
- 超限落盘：路径在 `<state>/spill/<agent>/`、文件内容 == 完整 JSON、`total_bytes` == 文件大小 == `bound_bytes`、head 保留顶层标量与短列表、tail 是文本尾部；
- 失败保留尾部（直接调用 + server 异常路径各一条）；落盘失败不打断调用；
- 分页：ledger 逐页 35 条无重复无遗漏且不落盘、offset/cursor 一致与非法 cursor、`agent_messages` / `kb_query` / report 的 queue 与 history 各自走完所有页；
- `spill_read`：越权（别人的 agent 目录、相对路径穿越、spill 根之外）、own 目录可读、窗口拼接等于原文件、读完不再落盘；
- brief/full：status（含 rev/if_rev）、kb_query、test_wait、测试结果消息（brief 一行，full 为 bounded 完整记录，超限即 spill）；
- 回收：先按 7 天/字节预算删过期 spill，`opsb doctor clean` 列出 spill 与 peer、默认不删、`--purge-retention --apply` 才删；
- 计量：`spilled` / `bound_bytes` 落到行里且 `bound_bytes > result_bytes`。

同时更新了三处既有断言以匹配新契约：工具计数（27→28，三处）、meter 行字段集（+`spilled`/`bound_bytes`）、`parhelion_ledger` schema 属性集（+`offset`/`cursor`）；`cli/tests/test_orchestration_v4.py` 的伪 `test_runs` 模块补了 `result_message`。

`swift build`：本包没有改 Swift（只改了 Python 与测试），按预期未运行。

## 4. 回放估算（用 P03 的 `opsb replay metrics`）

**命令**（工作树代码，只读；`--no-cost` 只为缩短运行）：

```text
PYTHONPATH=cli ~/.parhelion/venv/bin/python cli/opsb replay metrics --json --no-cost
```

输出窗口：`2026-11-25T14:30:56.914692+00:00 .. 2026-11-26T05:41:17.607328+00:00`，15.172 小时，135 个 worker 会话日志。

**分母（写清楚）**：本场 worker 会话日志里**每一次 `parhelion_*`（平台 MCP）工具调用**的结果字节，取自 `replay metrics --json` 的 `tools` 段并按 `parhelion_` 前缀过滤：2,782 次调用、7,810,115 B。用同一个 loader（`replay.loader.load_agent`）逐次重算得到的逐工具合计与 `tools` 段完全相等（7,810,115 B），可复核。**不含**：编排侧的轮询类调用 205 次（`orchestrator` 段只统计 `agent_wait`/`test_wait`/`agent_send`/`status`/`wait` 的计数，日志不落结果字节）与非 MCP 工具（`bash`、`read`、`job_*` 等）。`mcp-calls.jsonl` 目前没有行——P03 的计量在冲刺结束后才合入，本场没有经它记录的调用，所以只能用会话日志这一路。

**逐次模型**（上限取默认 5000）：

1. `size ≤ 5000`：不变；
2. 可翻页工具（`agent_messages`、`kb_query`、`ledger`、`report`）且 `size > 5000`：按一页 5000 B 计；
3. 其余工具且 `size > 5000`：按 spill 信封 2400 B 计。信封实测：三种超限形状（大列表、20 KB 错误串、嵌套逐例行）分别 2,287 / 2,235 / 2,430 B，其中 head 959–1,024 B、tail 1,027 B、path 119 B；取 2,400 为上限。

**结果**：

| 项 | 数值 |
|---|---|
| 平台 MCP 调用 | 2,782 |
| 原始字节 | 7,810,115 |
| 全部经 `bound_output` 后 | 3,175,915 |
| 省下 | 4,634,200（**−59.3%**） |
| 超上限的调用 | 287（10.3%）：66 次翻页（全为 `kb_query`）、221 次落盘 |

按工具（只列字节最大的几项）：

| 工具 | 调用 | 超限 | 原字节 | bounded | 变化 |
|---|---:|---:|---:|---:|---:|
| `parhelion_status` | 332 | 135 | 2,901,312 | 585,843 | −79.8% |
| `parhelion_kb_query` | 84 | 66 | 2,083,285 | 342,868 | −83.5% |
| `parhelion_agent_inbox` | 194 | 53 | 780,871 | 319,125 | −59.1% |
| `parhelion_test_plan` | 597 | 17 | 711,938 | 662,210 | −7.0% |
| `parhelion_test_wait` | 322 | 0 | 450,475 | 450,475 | 0% |
| `parhelion_submit_gate` | 115 | 1 | 371,762 | 365,540 | −1.7% |
| `parhelion_bench_diagnose` | 79 | 0 | 166,056 | 166,056 | 0% |
| `parhelion_kb_record` | 33 | 13 | 116,748 | 69,682 | −40.3% |
| 其余 9 个 `parhelion_*` | 1,026 | 2 | 227,668 | 214,116 | −6.0% |

**口径说明**：可翻页工具一律按“整页 5000 B”计是偏保守的——handler 自己的默认页更小（status/brief 通常 <2 KB、kb brief 3×600、ledger 每页 20 行），所以实际省得更多；反过来，估算**只算省下的字节**，没有把 worker 为取全文可能多出的 `spill_read` 调用算进去（那是新增的上下文与调用数，量级约 2–3 KB/次，远小于被替换的 5–50 KB）。两种效应方向相反，报告按“只看上限动作”的净字节口径给数。

## 5. 真实数据与工作树验证

1. **回放**：上面第 4 节。只读性也核过：`replay metrics` 自身不写任何文件；跑完后 `~/.parhelion/cache/.cache_scheduler_bridge.json` 等变化是**常驻守护进程**周期性刷新造成的（不跑我的命令、空等 25 秒同样在变），与本命令无关。总线只以 `mode=ro` 打开（`replay/sources.py` 自身声明）。报告与本文不含 cookie、账号名、用户 ID、token。
2. **`opsb doctor clean` 干跑**（真实 `PARHELION_HOME`，无 `--apply`）：新段落输出 `Spill and peer retention (0, 0.0 MB): (none)`——P13/P14 尚未上线，真实 home 下还没有 spill 与 peer 文件；该命令没有写任何文件。spill/peer 的列出与删除用临时 `PARHELION_HOME` 的单测验证（见 3.）。
3. **工作树 stdio 最小调用**（临时 `PARHELION_HOME` + 临时工作区，不碰真实比赛目录）：

   ```text
   $ printf '<initialize>/<tools/list>/<status brief>/<status full>' | \
       PARHELION_HOME=/tmp/p13-smoke-home PYTHONPATH=cli ~/.parhelion/venv/bin/python \
       cli/opsb mcp serve --workspace /tmp/p13-smoke-ws
   tools: 28
   brief isError: False ...
   full  isError: False ...
   ```

   再把该临时 home 的 `mcp.output_cap_bytes` 设为 300 重跑：`parhelion_status` 返回 `{spilled: true, total_bytes: 659, path: .../state/spill/orchestrator/20261126T141758050159Z-parhelion_status.json, head: {contest, npu, problems, rev, sampled_at, scheduler}, tail: ...}`；随后 `parhelion_spill_read(path, limit=200)` 返回 `{offset: 0, limit: 200, next_offset: 200, text: {"sampled_at": ...}`，spill 目录仍只有 1 个文件（回读不二次落盘）。
4. **真实比赛目录**：没有对 `/Users/winbeau/Competitions/mat-chem-sim-pred/**` 跑任何会写 `feedback-log.jsonl` / `.feedback-signatures.json` / `.candidate-archive.json` 的命令；所有写入验证都在临时 `PARHELION_HOME` 的临时工作区里完成。

## 6. NOTE（非显然约束与源码事实）

1. **状态目录解析**：`services/test_runs.registry_dir(ws)` = `resolve_workspace_state_dir(ws) / "test_runs"`；`bound.spill_root` 直接用同一个 `drivers.base.resolve_workspace_state_dir`，再拼 `spill`。主控调用没有 `agent_id`，落盘目录名用 `orchestrator`。
2. **`bound_bytes` 是 pre-bound 字节**，不是 bounded 之后的：计量行里 `result_bytes` 已经是“实际返回”，所以再用一个“返回后”的字段没有信息量；两者之差才是上限省掉的量。没有落盘/缩页时两者相等（测试断言）。
3. **`head` 是重建的 JSON 值，不是文本前缀**：`head` 可能是对象/数组/标量，规则是“顶层标量与短列表整份优先，然后才给长列表/长字符串”；`tail` 是文本串，切点在 JSON 转义之外。二者都在信封里，信封本身是合法 JSON。
4. **history / queue 的 4 KB 截断被分页取代**（P12 审查第 2 项的原意）：`history` 现在**最新在前**，`offset` 从最新往回数；旧实现取的是“最后 `limit` 条（按时间正序）”再 `_trim` 到 4 KB。queue 同理改为分页，不再 `_trim`。其余视图的 4 KB / 1 KB 预算不变（`per_case`、`diff`、`env`、`board`）。
5. **`kb_query` 的 `page.total` 是下界**：`total = offset + 已取到的行数`（底层查询不暴露总数），`next_cursor` 仍然是精确的（每次多取一行判断是否还有）。深翻页时请求的 `limit` 会增大（`offset + page + 1`），没有额外上限。
6. **`parhelion_agent_messages` 的 ack 规则**：`offset`/`cursor` > 0 的页不 ack（没展示的行必须保持未读）；第一页保持原来的 `ack=True`。页大小上限 500。
7. **spill 回收只在写新 spill 时触发**（`bound_output` 落盘后调 `reap`）。一个工作区若长期不再产生 spill，旧文件会留到下一次落盘或操作者显式 `--purge-retention --apply`；`doctor clean` 的 `retention` 段就是给这种情况看得见的。
8. **`spill_read` 每窗上限是 `output_cap - 700`**（默认 3000），且它在 `NO_SECOND_SPILL` 里：即使配置的上限极小，回读也不会二次落盘，这是“回读不二次落盘”的直接实现。
9. **`opsb mcp call` CLI 不经过 bound**：子计划把 `mcp/server.py` 的 `tools/call` 定为唯一出口；CLI 子命令直接调 `call_tool` 用于调试，本次没有改它。
10. **`format` 与 rev 的归一化**：`parhelion_status` 只在参数里出现 `format` 时才把它折成 `verbose` 再算 rev，所以老客户端存的 rev 不受影响；`format=full` 与 `verbose=true` 同 rev；`format=brief` 与完全不带参数的调用体相同但 rev 不同（文档只承诺“同参数同数据同 rev”）。
11. **测试计数**：`cli/tests/test_agent_mesh_http.py` 1 处、`cli/tests/test_mcp_server.py` 2 处从 27 改到 28；P08 的 `parhelion_agent_peek` 需要自己再改到 29。
12. **`handlers_report.py` 的重新导出**：共享整形 helper（`_trim`、`_bytes`、`BIG`、`ENV_BYTES` 等）现在定义在 `report_views`，`handlers_report` 以 `from .report_views import ...` 重新导出，所以既有测试（从 `handlers_report` 导入）无需改动。
13. **`DEFAULT_CONFIG` 的新 `mcp` 段**：用户配置里若写了 `mcp`，`ConfigRepository.load` 会整段替换（只有 `scheduler` 是逐键合并），所以 `bound` 对每个键单独用 `get` + 默认值，不依赖段内完整性。
14. **回放用的“结果字节”是 worker harness 记录的工具结果文本**，不含 JSON-RPC 信封；计量行的 `result_bytes` 同样不含信封，两者口径一致。
15. **测试结果消息的 `format` 存在 run 记录里**（`result_format`，`parhelion_test` 提交时写入；join/reuse 沿用第一条记录），投递时由 `services/test_runs.result_message` 决定 brief 一行或“一行 + bounded 全文”。`cli/tests/test_orchestration_v4.py` 的伪 `test_runs` 模块因此要补一个 `result_message`。
16. **P12 审查第 3 项未做**：`_docs()` 跨题扫描会改行为（`sha` 为空时可能选中别题），按要求不做；第 4、5 项也不在本包。

## 7. 已知风险与建议后续

1. **多一次回读的代价**：落盘后 worker 只看到约 2.3 KB 的信封，要取全文需再调一次 `spill_read`（每次 ≤3 KB）。对本场的字节账仍是净省（5–50 KB → 2.3 KB + 可选 3 KB），但会多一次工具往返；`status` 的 135 次超限大多来自 `verbose`/`detail` 调用，后续可以考虑让 `parhelion_status` 的 `case_detail.rows` 也支持分页，或把 detail 的逐例表改成按需视图。
2. **合并后第一次上线要重启守护进程**（server 与 `handlers_*` 都改了，`20-REMAINING` §4 已写明 4.2.0 统一重启一次）；本包不重启。
3. **history 的顺序变化**：改成最新在前 + 分页。若 GUI 或外部脚本依赖原来的时间正序，需要同步；本仓库的测试按 `submission_id` 取值，未受影响。
4. **深翻页**：`kb_query` 与 `agent_messages` 的“多取一行”会随 `offset` 增大而增大单次查询量（KB 结果排序本身无上限）；正常深翻不现实（KB 条目数百条），若真出现可在 `paging.page_window` 加一个 `offset` 上限。
5. **回收时机**：只在写新 spill 时回收。若希望“即使没有新 spill 也按天清”，可在 daemon 的周期任务里调 `bound.reap`（本包没接，避免改守护进程的调度面）。
6. **peer 保留策略**：本包只列不删。附件是消息正文，删掉会让后续回读断链；如果要自动回收，建议先由 P16 统计引用年龄再定。
7. **P16 的实测**：合并后 `mcp-calls.jsonl` 会带 `spilled` / `bound_bytes`，可以直接用真实计量验证本文第 4 节的估算（当前只能靠会话日志）。P08 的 `agent_peek` 会自动继承上限（所有 `tools/call` 都过 bound）。
