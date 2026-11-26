状态：已交付

# P27 派活情报与全队广播 — 交付报告

分支 `impl/P27-brief-broadcast`，基于 P26 合入后的 `main`（交付前已 `git rebase main`，含
`6d4a61f` 审查记录与基线、`f67f085` 回放入口修复）。新模块 `cli/opsb_core/evidence.py` 117 行
（上限 350）。

## 1. 分支与提交

```
$ git log --oneline main..HEAD
869bc1d Add the P27 intel block, workspace broadcasts with evidence classes, the sweep view and two comms metrics
```

`869bc1d` 是代码与测试（一次提交，全绿状态）。本报告的提交是本分支第二个提交，标题以
`Deliver P27:` 开头。分支上没有别的提交。

## 2. 子计划逐项完成情况

| 子计划条目 | 状态 | 位置 |
|---|---|---|
| 1. 启动包/扩展状态卡的情报段（榜首与加权空间、最近三发、假设与停手条件、规则摘要） | 已完成 | `services/state_card_ext.py`（`intel`、`render_intel`、`intel_text`）、`drivers/startup_pack.py`（第 5 段） |
| 2. `parhelion_squad(to="all")` 全员广播、回合边界合并投递、worker 频控、主控不限 | 已完成 | `agents/manager.py::squad`、`agents/peer.py`（`broadcast_gate`/`note_broadcast`） |
| 3. 证据等级 `evidence: {class, ref}`、`待验证` 标记、旧信封兼容、协作页 | 已完成 | `opsb_core/evidence.py`（新）、`agents/peer.py`、`agents/bus_store.py`、Swift 两处 +17 行 |
| 4. `parhelion_report(view=sweep, metric=…)`，主控限定，4KB，只读磁盘结果 | 已完成 | `mcp/report_views.py::_sweep`、`mcp/handlers_report.py` |
| 5. `sprint_metrics` 两项指标（广播条数、按证据等级的消息数） | 已完成 | `replay/sprint_metrics.py::comms_metrics`、`commands/replay_cmd.py` 一行输出 |
| 「测试」节：情报段内容与字节预算；广播全体/不自送/频控；证据等级校验与标记；旧信封；sweep 字段、角色与字节上限 | 已完成，新增 17 条 | `cli/tests/test_p27_intel_broadcast_sweep.py`（新） |

没有「部分完成」或「未做」的子项。

### 2.1 情报段：样例与字节数

真实工作区（`mat-chem-sim-pred` 的只读副本 `/tmp/p27/ws`）上按题生成启动包，**没有派发**，用
`startup_pack.build` 直接构建：

```
### NeighborListPbcFixedK  pack 4527 B / budget 21600 B / intel 459 B / cuts []
INTEL NeighborListPbcFixedK (weight x5): leader 84.64, board 23.55, weighted gap 305.45
recent: d70506f pair-tested back-to-back in one clean windo… 24.74 · 3f33362 standard 15/15 PASS (suite v0, run bf31c9ca… 24.86 high · 14ee90e orch: CRITICAL RESTORE - board is 0, restor… 20.70 high
stop when: attempts left 3/3 · 1/6 paid shots without a new high · no open hypothesis: record one before the next change
rule: unknown, sigma 0.51 (contest, df 5)
```

```
### CGTensorProduct  pack 4463 B / budget 21600 B / intel 447 B / cuts []
INTEL CGTensorProduct (weight x4): leader 87.79, board 28.70, weighted gap 236.36
recent: 879278d Candidate 879278d (branch sprint-solo, HEAD… 24.88 · 3727609 Candidate 3727609 vs incumbent 9654f85. 15/… 28.70 high · f8a1b62 Candidate f8a1b62 (branch sprint-solo) vs i… 0.00
stop when: attempts left 3/3 · 1/6 paid shots without a new high · no open hypothesis: record one before the next change
rule: unknown, sigma 0.51 (contest, df 5)
```

（原文 4 行，UTF-8 459 B / 447 B；`INTEL_MAX_BYTES = 700`。真实工作区里没有 P22 的规则快照，
所以 `rule:` 落到 `unknown`；有快照时该行形如 `rule: best, sigma 2.56 (problem, df 5)`，由单测钉住。）

四项数据来源：

- **榜首与加权空间**：先读 `results/scoring-evidence/live-board-tbest.json` 的
  `frontier_top1`（榜首）与 `our_score`（在榜），差乘难度权重（`opsb.workspace.json` 的
  `difficulty`）；没有 live board 时回落到 `brief.py` 用的精简榜单缓存
  `board-summary.json` 首行 + `score-snapshot.json`。全部是本地缓存，**不联网**。
- **最近三发**：回执账本最后三条已判回执的 sha7、机制关键词、判分、是否在榜。
  机制关键词优先取同一 SHA 的账本假设/实验条目 `claim`（`measurement` 自动行跳过），否则取回执
  `note`。
- **当前假设与停手条件**：`ledger.view.query(rows,"open")` 的未关闭假设 id、按
  `ledger/budget.py` 的 `ATTEMPT_BUDGET=3` 算出的剩余尝试数、连续未上在榜的已判回执数（上限 6，
  对应 AGENTS.md 第 6 条）。
- **规则摘要**：复用 P22 的 `judge_rules.status_line`，一行写榜型与 σ̂（含 scope/df）。

字节预算：情报段是第 5 段（Problem ledger）的一部分，现有的收缩顺序 `TRUNCATE_ORDER=(5,3,4)`
先缩第 5 段，所以超预算时情报段按现有顺序收缩；单测 `test_intel_is_a_bounded_section_of_the_startup_pack`
钉住「先缩第 5 段」。**契约本身不加行**：`brief.compose_session_brief` 一个字没动。

计量：`build()` 返回 `intel_bytes`；`record_size` 把它写进 P03 计量的同一条
`tool=startup_pack` 行（`mcp_meter.record_call` 新增可选 `extra` 字段，聚合读取忽略它）。
单测断言该行 `extra.intel_bytes == pack["intel_bytes"]`。

### 2.2 广播与证据等级协议

**范围与投递。** `parhelion_squad(recipient="all")`（`to` 同义）是本工作区**所有在跑席位**
（`list_agents(include_stopped=False)`：排除 `stopped`/`error`），**发送者自己除外**；定向发送
（agent_id / axis / role_label）保持原语义（同题队友）。投递沿用 P14：每席位入 peer 队列，在
收件方回合边界合并为**一条** `squad_to_worker`/`peer` 总线行 + 一次唤醒；收件方账本行沿用 P14 的
`source=peer` 行，**写进收件方自己的题**（跨题广播时不再写发送者的题）。

真实输出（两个题、三个席位）：

```json
{"ok": true, "broadcast": true, "recipients": ["dee-a674c3", "dee-79e890"], "delivery": "next turn boundary"}
```

**频率限制。** worker 席位每 `BROADCAST_MIN_SEC = 600` 秒最多一条全员广播，记在
`<workspace state>/peer/broadcast.json`（flock 保护，最多保留 200 个发送者）。超限时**回复说明且不
投递**（不抛异常、不入队）：

```json
{"ok": false, "broadcast": true, "limited": true, "delivered": false,
 "reason": "a worker seat sends at most one workspace broadcast per 10 minutes; retry in 600s or send to named seats",
 "retry_after_sec": 600.0, "recipients": []}
```

主控不受限：主控没有席位记录，`manager.squad` 以 `sender="orchestrator"` 走广播路径，`broadcast_gate`
的 `role` 为 `orchestrator` 时直接放行；MCP handler 在没有 `_caller_agent_id` 且角色不是 worker 时
用它签名。连续两次主控广播都返回 `ok:true`（单测与样例都验证）。

**证据等级。** 信封新增**必填** `evidence: {class, ref}`：

- `class` 只取 `local`、`paired`、`online_receipt`（常量定义在
  **`cli/opsb_core/evidence.py` 的 `CLASSES`**；P28 合入后从同一个模块导入，不要再定义一份）；
- `ref` 指向运行 ID 或 `submission_id`（也接受 P14 的 `path#anchor`），≤300 字符；缺失即
  `ValueError`（`evidence is required` / `evidence.ref is required`）；
- `class != online_receipt` 的消息，收件方正文**前**加一行
  `待验证 (<class>) — <class> evidence, not an online judge receipt`，消息头写
  `evidence=<class>:<ref>`；收件方账本行 `why` 写 `peer message <id> from <sender> (evidence <class>)`；
- 旧信封（没有 `evidence`，或仍是 P14 的裸字符串）按 `local` 处理：`evidence.normalize` 把字符串
  收成 `{class: local, ref: <原字符串>}`，渲染时同样标 `待验证`；
- 总线 `messages` 表新增 `evidence_class` 列（批次取最弱的一档），协作页据此显示 `待验证`。

投递后的真实文本（同一批次里一条 `local`、一条 `online_receipt`，只有前者被标）：

```
[peer batch ×2] merged at your turn boundary; answer them together.
待验证 (local) — local evidence, not an online judge receipt
[peer 41b96227d16f] from=dee-86eb0f refs=- evidence=local:run-7f3a9c21
claim: the small bucket repeats once; try a 2-stage gather
[peer aee9441c8651] from=dee-86eb0f refs=- evidence=online_receipt:sub-20261126-0001
claim: judged row
```

该批次总线行的 `evidence_class` 为 `local`（最弱一档）。

### 2.3 `sweep` 视图样例（当前工作区只读生成）

`parhelion_report(view="sweep", metric="size")`，在 `mat-chem-sim-pred` 的只读副本上运行，14 题、
1477 B：

```
columns: ["title", "board_sha", "board_score", "run_sha", "basis", "size_bytes", "public_us"]
["BodySliceExtract", "9f796ce", 25.66, "c088369", "formal", null, [2.77, 1.98, 1.94, 2.21, 1.98]]
["BucketPackMask",   "c0b9df3", 38.19, "c0b9df3", "formal", null, [2.28, 1.95, 1.98, 1.46, 2.29]]
["CGTensorProduct",  "3727609", 28.7,  "a614e64", "formal", null, [11.1, 1.97, 1.85, 2.08, 2.78]]
["EdgeIndexCoalesce","4f954c3", 15.01, "4f954c3", "formal", null, [2.92, 237.73, 1165.2, 317.38, 177.38]]
```

同一命令在 `ops-bench`（旧布局，没有 `bench-runs` 的 formal 报告）上，指标来自最新一次存储运行，
`basis=latest`，10 题 969 B：

```
metric=size:    ["CTCLossV2", "dcc7754", 75.11, "", "latest", 603200, [5.56, 4.51, 4.37, 5.63, 5.45]]
                ["ConcatD",   "f60fa82", 75.4,  "", "latest", 799312, [4.14, 4.2, 5.17, 4.24, 4.11]]
metric=compile: ["CTCLossV2", "dcc7754", 75.11, "", "latest", 26.72,  [5.56, 4.51, 4.37, 5.63, 5.45]]
                ["ConcatD",   "f60fa82", 75.4,  "", "latest", 48.63,  [4.14, 4.2, 5.17, 4.24, 4.11]]
metric=cold:    ["ConcatD",   "f60fa82", 75.4,  "", "latest", 0.17,   [4.14, 4.2, 5.17, 4.24, 4.11]]
```

- 列：`board_sha`/`board_score` 是榜单当前显示的那条回执（`live-board-tbest.json` 的
  `board_submission_id` → 回执），`run_sha`/`basis` 是最新**正式**运行（`bench-runs/*/report.json`
  `formal:true`）的 SHA 与依据（`formal` / `latest` / `-`），指标列按 `metric` 改名：
  `size_bytes`（内核 `.o` 总和）、`compile_sec`（构建墙钟秒）、`cold_tax_us`（3 次冷启动税）、
  `pmu`（主导 pipe 与百分比），`public_us` 是该回执的线上公开例耗时（没有回执就省略）。
- 只读：数值全部来自 `results/**` 的存储运行与 `fast_feedback` 的 ELF/cold3/PMU 卡，
  **不触发任何设备运行**（`pmu_torch_card.build_pmu_torch_card` 只读日志，模块内唯一写文件的
  `bench_scaffold` 不在调用链上）。14 题耗时约 0.35 s。
- 角色：`view=sweep` 只给主控——handler 层 `_caller` 先拒 worker，视图层再拒一次
  （`PermissionError: view=sweep is orchestrator only`），有测试。
- 大小：`SWEEP_BYTES = 4096`，`_fit` 先丢 `source` 再丢行尾；120 题的用例被压到 ≤4096 B 且
  `truncated:true`。
- 当前工作区这一场（`mat-chem-sim-pred`）的存储运行没有内核体积/PMU/冷启动遥测，所以
  `size`/`cold`/`pmu` 三列是 `null`；`ops-bench` 的旧运行有这些遥测，上面第二段是真实数值。
  `pmu` 在两场上都是 `null`：最新一次存储运行没有 PMU 计数块（更早的 ops-bench 运行里有，
  抽取器可用），视图按「没有就省略」处理，不猜。

### 2.4 工具表字节（改前 → 改后 + 压缩清单）

`json.dumps(list_tool_schemas(role))` 的 UTF-8 字节，上限 `TOOL_TABLE_BUDGET` **未改**
（主控 15000 / worker 13000，测试里仍是这两个数）：

| 角色 | 改前（`main`） | 改后 | 差 | 余量 |
|---|---:|---:|---:|---:|
| 主控 | 14867 | **14803** | −64 | 197（目标 ≤14844 ✓） |
| worker | 12522 | **12124** | −398 | 876 |
| recon | 9726 | **9465** | −261 | — |
| knowledge | 8430 | **8234** | −196 | — |

主控表的变化可以拆成三块（都是实测序列化字节，条目含 name/description/inputSchema）：

| 结构变化 | 字节 |
|---|---:|
| `parhelion_squad` 整条**新增**到主控表（描述 210 + schema 408） | **+676** |
| 其中 `evidence` 对象让该条 schema 由 273 → 408（两张表都受影响） | +135 |
| `parhelion_report` schema 由 415 → 482（`metric` 参数 + `sweep` 枚举） | +67 |
| 28 条描述压缩合计（见下表；含 squad 自身 −8、report −108） | **−838** |

也就是说：主控表 14867 − 64 = 14803，是「多一条工具」与「28 条描述变短」两件事抵消后的净值，
不是靠调上限。

压缩的是**描述文字**，不是语义。逐条（「省」= 描述 + 该工具参数描述合计）：

| 工具 | 描述改前 | 改后 | 省 | 其中参数描述 | 改动要点 |
|---|---:|---:|---:|---:|---|
| `parhelion_agent_peek` | 330 | 194 | **136** | 0 | 去掉重复的 why 句，保留 what 四种取值与「绝对路径/.. 拒绝」「8KB」 |
| `parhelion_report` | 347 | 246 | **108** | −7 | 仍逐一列出 10 个视图名（测试要求）、保留数据通道一句；`problem` 参数说明改短 |
| `parhelion_wait` | 246 | 178 | **71** | −3 | 保留「优先用它、omit timeout 会自延长、max 100s」；`since` 说明改短 |
| `parhelion_note` | 164 | 117 | **67** | −20 | 保留「status 变化要新行 + supersedes 或 id」；`claim`/`problem` 说明改短 |
| `parhelion_test` | 181 | 138 | **63** | −20 | 保留 request_id 2s 与 device cost；`level` 三档语义不变 |
| `parhelion_kb_query` | 137 | 101 | **36** | 0 | 保留 pillar/分页语义 |
| `parhelion_submit_gate` | 196 | 160 | **36** | 0 | 保留「preview / enqueue=true / hypothesis_id 或从 commit 推导」 |
| `parhelion_test_wait` | 133 | 99 | **34** | 0 | 保留 30/45 秒上限 |
| `parhelion_sprint_start` | 83 | 64 | **34** | −15 | 五种 mode 的枚举值与含义不变（换成 `|` 写法） |
| `parhelion_status` | 167 | 135 | **32** | 0 | 保留 if_rev / not_modified |
| `parhelion_escape_status` | 135 | 113 | **22** | 0 | 保留 WA/RE、reproducer、reviewed-trial、next step、worker 只见本题 |
| `parhelion_agent_wait` | 130 | 109 | **21** | 0 | 保留 ≤45s 与「优先 parhelion_wait」 |
| `parhelion_elf_cold3_check` | 84 | 64 | **20** | 0 | 保留 `.o/.text/template` 与 3-run DVFS |
| `parhelion_test_cancel` | 65 | 50 | **15** | 0 | 保留「worker 只能取消自己的」 |
| `parhelion_ledger` | 123 | 108 | **15** | 0 | 保留五个视图名（枚举在参数里，未动） |
| `parhelion_agent_report` | 109 | 94 | **15** | 0 | 保留 kind=result 即压缩检查点 |
| `parhelion_spill_read` | 115 | 101 | **14** | 0 | 保留 next_offset 与「只读自己的」 |
| `parhelion_escape_rebind` | 133 | 119 | **14** | 0 | 保留「只有未提交且未入队时可重绑」 |
| `parhelion_agent_messages` | 84 | 71 | **13** | 0 | 保留 message/diff/commit |
| `parhelion_escape_reproducer` | 106 | 95 | **11** | 0 | 语义不变 |
| `parhelion_recall` | 103 | 92 | **11** | 0 | 保留 file#line 来源 |
| `parhelion_agent_spawn` | 113 | 102 | **11** | 0 | 保留「阻塞 active_driver 自派」「无并发上限」 |
| `parhelion_harness_pool` | 70 | 62 | **8** | 0 | 语义不变 |
| `parhelion_agent_stop` | 75 | 67 | **8** | 0 | 语义不变 |
| `parhelion_squad` | 218 | 210 | **8** | 0 | 重写为含 `evidence{class, ref}`、`all`、频控的新契约 |
| `parhelion_kb_feedback` | 70 | 64 | **6** | 0 | 语义不变 |
| `parhelion_pmu_card` | 54 | 49 | **5** | 0 | 语义不变 |
| `parhelion_bench_diagnose` | 85 | 81 | **4** | 0 | 语义不变 |
| **合计** | — | — | **838** | −84 | 28 条 |

契约削弱核对（脚本逐条比对 `main` 与分支的 `TOOL_SCHEMAS`）：

- **枚举值一个没删**：唯一的枚举差异是 `parhelion_report.view` **新增** `sweep`
  （`per_case, diff, history, queue, board, bottleneck, rules, sweep, env, help`，顺序与
  `handlers_report.VIEWS` 一致；有测试钉住）。
- **必填性一个没变**：所有工具的 `required` 与 `main` 逐字相同（`parhelion_report` 仍是 `["view"]`，
  `parhelion_squad` 仍无 required——`evidence` 的必填性在 `inputSchema.properties.evidence.required`
  里，即对象内的 `["class","ref"]`）。
- **参数只删不增语义**：改动过的参数描述 **6 条**（`report.problem`、`test.level`、`note.claim`、
  `note.problem`、`sprint_start.mode`、`wait.since`），全部是同一含义的更短表述；新增参数只有
  `report.metric`，没有空 description（不写字段，而非写空串），取值由枚举给出、语义写在工具描述里。
- **用途一眼可辨**：每个工具描述仍是一句「做什么 + 调用方不能弄错的约束」，最短的
  `parhelion_pmu_card` 仍是 "PMU bottleneck card: ours/torch ratio per bucket."。

## 3. 全量测试与 Swift

```
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
868 passed, 19 skipped, 2 subtests passed in 89.18s (0:01:29)
```

（`main` 上实测 851 passed / 19 skipped；本包 +17 条新测试。基线里 `P26` 之后是 850，rebase 带进
`f67f085` 的 1 条后为 851。）

```
$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
607 passed in 74.65s (0:01:14)
```

（`main` 上实测 590；本包 +17。）两条都是 rebase 之后跑到底的末行；
`runtime/tests/test_device_broker.py` 的两条预算测试本轮没有偶发失败，无需单跑。

**Swift 有改动**：`Sources/Parhelion/Domain/AgentMeshBusModels.swift` +10、
`Sources/Parhelion/Features/AgentMesh/AgentMeshInspectorView.swift` +7，共 **+17 行**（门槛 30 行），
`decodeIfPresent` 读可选 `evidence_class`，非 `online_receipt` 的 peer 行显示 `待验证` 徽标。
`swift build` 通过：`Build complete! (5.46 sec)`。

## 4. 真实数据验证（只读）

数据只读：真实比赛目录未写入、未移动；需要状态目录的操作都在 `PARHELION_HOME=/tmp/p27/home`
下做，比赛数据先复制到 `/tmp/p27/ws`。会话日志、总线都只读，只出聚合数字。没有新增
`~/.parhelion/workspaces` 目录，没有派发或停止 worker，没有访问判题接口。

1. **启动包（真实题目，不派发）**：见 §2.1，`NeighborListPbcFixedK` 4527 B（情报段 459 B）、
   `CGTensorProduct` 4463 B（情报段 447 B），都在 21600 B 预算内、无收缩。
2. **`opsb replay metrics` 在 2026-11-26 那场上**（`mesh-*.jsonl` 135 份，窗口
   2026-11-25T14:30:56Z .. 2026-11-26T05:41:17Z，15.172 h）：
   ```
   Orchestrator polls 205 (13.5/h): ...; squad calls 0
   Peer messages 332 (21.9/h): broadcasts 12; evidence local 0, paired 0, online_receipt 0, legacy 332
   ```
   JSON：`"comms": {"squad_messages": 332, "broadcasts": 12, "per_hour": 21.9,
   "evidence": {"local": 0, "paired": 0, "online_receipt": 0, "legacy": 332}}`。
   这一场是 P27 之前的数据，所以 332 条全是 `legacy`（信封里没有结构化等级）；**全员广播 12 条**
   正好对应上一场「12 次 to=all」的规模，作为下一场的对照基线。
3. **`sweep`（只读）**：见 §2.3，当前工作区 14 题 1477 B、`ops-bench` 10 题 969 B
   （size/compile/cold 有真实数值）。
4. **广播与证据等级**：见 §2.2 的真实输出（三个席位、两个题；超限回复；主控连发不限额）。

## 5. NOTE（源码事实与非显然约束）

1. **主控表多了一整条 `parhelion_squad`（676 B，主控表口径）。** 子计划只写「`to="all"`，
   主控不限」，没有说主控要看得见这条工具；但主控要让广播有用就必须能发（扫描 → 广播是 §4 的
   价值链条），而 `parhelion_squad` 在 4.2 是 worker-only。代价与收益都写在 §2.4；如主会话认为
   不值得，删掉 `roles` 里的 `"orchestrator"` 并把 `tool_parhelion_squad` 的 orchestrator 分支
   收回即可（其余广播/频控/证据逻辑不受影响，主控表立刻回到 14803 − 676 = **14127 B**）。
2. **榜首分来自 live board，不是 `brief.py` 那个 `board-summary.json`。** 当前工作区没有
   `board-summary.json`，但 `results/scoring-evidence/live-board-tbest.json` 有逐题 `frontier_top1`
   （榜首）与 `our_score`（在榜），所以先读它、再回落 `board-summary.json` + `score-snapshot.json`；
   两个来源都是本地缓存，都不联网。
3. **「是否新高」用的是 `scheduler.receipts.beat_board`**：对一条 Pass 且有分的回执它返回 True，
   含义是「这条回执入榜了」，不是「分数高于此前所有发」。情报段按这个语义打 `high`。
4. **「六发无增益」与 `agents.no_gain_streak` 不是同一个数。** 情报段数的是「最近连续未上在榜的
   已判回执」；平台侧 `judge_feedback` 用的是折扣期望增益（同一 `epoch_gains` 口径）。两者在
   「Pass 但分数低于此前最好」时会分开：`beat_board` 只问这一发是否入榜，所以情报段把它算作有增益，
   平台的折扣口径不算。也就是说情报段的 6 发计数可能比平台侧来得晚；要不要改成同一口径见「待决」。
5. **`evidence` 从 4.2 的字符串变成了对象**：`{"type": "string"}` → `{"type":"object", ...}`，
   发送时必填。P14 的 peer 协议单测（7 条因此先红后绿）已补上 `evidence={"class":"local","ref":...}`；
   `peer.envelope` 仍是唯一的校验点，两个入口（MCP handler / daemon 路由）共用 `squad_body`。
6. **总线加列是幂等迁移**（`evidence_class TEXT NOT NULL DEFAULT ''`），沿用 P26 加 `seat` 的写法：
   `CREATE TABLE` 不变，`_init_db` 每次补列；旧进程按列名读（`sqlite3.Row`）不受影响，旧行读出
   空串。协作页只对非空且 ≠ `online_receipt` 的 peer 行显示 `待验证`。
7. **`mcp_meter` 新增可选 `extra` 字段**（P27 只为 `intel_bytes`）：不传就不写，`summarize` 不
   认识它，旧读数不受影响。
8. **`sweep` 的 `basis`**：旧布局（`ops-bench`）没有 `bench-runs/*/report.json` 的 `formal` 标记，
   这一列会写 `latest`，表示「指标来自最新一次存储运行」，不是伪造的正式运行。
9. **`pmu` 在两场都是 `null`**：最新一次存储运行没有 PMU 计数块（同一题的更早一次运行有，抽取器
   对 `KERNEL_JSON` 的 `metrics` 段可用）。这是「没有就省略」，不是视图缺陷。
10. **情报段在 `startup_pack` 第 5 段内部**，所以它随第 5 段一起被裁剪；单独调用
    `section_ledger` 也会包含它（`intel=` 参数可注入已算好的文本，`build` 只算一次）。

## 6. 风险与后续

1. **主控表余量只剩 197 B**（14803 / 15000），worker 876 B。P28 实测占 156 B，放得下；再下一个包
   几乎没有余量，届时应按 P28 的实测值重新压缩或把 `view=help` 一类内容移出 schema。
2. **28 条描述被改写**，虽然逐条核对过枚举/必填/用途，仍需主会话按 §2.4 清单复核措辞。
3. **广播频控是本地文件**（`peer/broadcast.json`），清空状态目录会重置窗口；它只限流不保证正确性，
   与额度、门禁无关。
4. **`sweep` 每题要读一次存储运行并建卡**（14 题约 0.35 s）；题目数大幅增加时可考虑缓存。
5. **情报段的「机制关键词」在缺假设时退化成回执 `note`**（当前工作区的账本只有 `measurement`
   自动行，所以样例里显示的是提交备注），这是子计划写明的回落顺序，不是缺失。

## 7. 待决

1. **主控是否保留 `parhelion_squad`**（676 B 的预算对价）。保留 = 主控能一条命令广播并在
   4.3 里兑现「主控不限」；不保留 = 主控表更宽裕，但主控没有全员广播通道。我的选择是保留
   （理由与撤销成本见 NOTE 1）。
2. **`high` 的语义**是否改成「分数高于此前所有发」而不是「入榜」（NOTE 3）。改动只在
   `state_card_ext.intel` 的一处判定，但需要一个新的判据来源。
3. **`sweep` 是否要按指标回溯到「最近一次带该指标的运行」**（现在统一用最新一次存储运行，所以
   `pmu` 在部分题上是 `null`）。回溯会让每题的 `basis` 随指标不同，语义更复杂、耗时约翻倍。
4. **「六发无增益」是否改用平台自己的折扣增益口径**（NOTE 4）。改了以后情报段与
   `judge_feedback` 的 stagnation 事件就会同一个数，但每次建包要多算一遍 `epoch_gains`。
5. **情报段 `stop when` 在没有任何未关闭假设时的措辞**（现在是
   `no open hypothesis: record one before the next change`），是否要改成直接给一条建议动作。
