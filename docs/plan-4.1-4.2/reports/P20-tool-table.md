状态：已交付

# P20 工具表瘦身（4.2 J-2）

日期 2026-11-26，分支 `impl/P20-tool-table`，基于 `main` `15eee65`（含 P16 与 J-1）。主控 25,007 → **14,601 B**、worker 22,066 → **12,507 B**，两条目标都达到；逐视图列说明移进 `parhelion_report(view="help")`，并在 `cli/tests/test_mcp_server.py` 加了一条把 15,000 / 13,000 钉住的测试。没有改工具名、参数名、参数类型、`enum` 既有取值、`required`、角色表、工具数或 `list_tool_schemas` 的过滤逻辑；没有碰 `aoci*.txt`、`.aoci/`、总线、守护进程、门禁、额度或判题站。

## 1. 分支与提交

```text
$ git log --oneline main..HEAD
524cd54 Shrink the platform tool table to the context budget (4.2 J-2)
```

本报告随最后一个 `Deliver P20:` 提交进入分支，因此上面是报告提交前的列表。改动四个文件，**没有新增源码或测试文件**（报告本身不在索引范围内——`aoci.txt` 里没有任何 `docs/plan-4.1-4.2` 对象，所以主会话不需要为 P20 做 `aoci scope acknowledge`，只需常规 `aoci_maintain` 刷新这四个文件）：

| 文件 | 改动 |
|---|---|
| `cli/opsb_core/mcp/schemas.py` | 全部描述字符串重写；`parhelion_report` 的 `view` enum 追加 `help` |
| `cli/opsb_core/mcp/handlers_report.py` | `VIEWS` 加 `help`；新增 `HELP` 表与 `_help()`；入口先分流 `help` |
| `cli/tests/test_mcp_server.py` | 新增 `test_tool_table_stays_within_the_context_budget` |
| `cli/tests/test_report_views.py` | 新增 `test_view_help_documents_every_column_without_spilling` |

## 2. 结果（§3.8 的目标）

口径就是任务书里的那一行：`json.dumps(list_tool_schemas(role), ensure_ascii=False, separators=(",", ":"))` 的 UTF-8 字节。

| 角色 | 工具数 | 改前 | 改后 | 变化 | 目标 | 余量 | token@0.33 改前 → 改后 |
|---|---:|---:|---:|---:|---:|---:|---:|
| orchestrator | 29 | 25,007 B | **14,601 B** | −10,406（−41.6%） | ≤ 15,000 | **399** | 8,252 → **4,818**（−3,434） |
| worker | 24 | 22,066 B | **12,507 B** | −9,559（−43.3%） | ≤ 13,000 | **493** | 7,282 → **4,127**（−3,155） |

拆开看（同一口径）：

| 成分 | orchestrator 改前 → 改后 | worker 改前 → 改后 |
|---|---:|---:|
| 删掉全部 `description` 键后的骨架 | 9,698 → 9,705 B（+7 = `view` enum 多出的 `"help"`） | 8,437 → 8,444 B |
| 描述文本（含键的语法开销） | 15,309 → 4,896 B（−68%） | 13,629 → 4,063 B（−70%） |
| 描述字段个数 | 137 → 40（其中嵌套 105 → **8**） | 同样 |

说明与 §3.8 的估算差在哪：§3.8 的骨架 11,823 B 是把 137 个 `description` 字段全部留成空串量出来的，所以那时描述文本只剩 3,177 B。本包把其中 97 个嵌套键整个删掉（105 → 8），键本身的语法开销不再计入骨架，骨架因此降到 9,705 B，同样 15,000 B 里能写 4,896 B 描述文本——**总量更低，同时保住了更多句子**。这个拆法也意味着：以后加参数不会再自动占十几个字节的空键开销。

## 3. 改法

### 3.1 `parhelion_report` 的逐视图列说明移入工具（−2,306 B）

schemas 里的三段视图说明（原本 2,148 字符）搬进 `handlers_report.HELP`，由 `view="help"` 返回：

```json
{"view":"help",
 "columns":{"per_case":{...19 列...},"diff":{...9 列...},"history":{...9 列...}},
 "views":{"per_case":"...","diff":"...","history":"...","queue":"...","board":"...","bottleneck":"...","env":"..."}}
```

- 实测回复 **2,452 B**，低于 `mcp.output_cap_bytes` 默认 5,000，所以 `bound_output` 原样返回、不会 spill（stdio 验证里 `spilled` 不存在、`isError=false`）。
- 每个列名的含义都保留了：`gap_pct`「percent above tbest」、`speedup`「ratio against the base SHA from the state card」、`verdict` 三态、`noisy`、`cal`、`wa`、`run` 等；`queue`/`board`/`bottleneck`/`env` 用一句散文列出字段与纪律（`queue` 永不返回账号字段、`board` 只读缓存不联网、`env` ≤1KB 且无凭据字段）。
- schema 里只剩三样：视图名列表（含 `help`）、一句「用它代替读 `report.json` / `submit-queue-outcome.jsonl` 的临时脚本」、P15 数据通道的一句话版本（board/tbest/回执/环境信息只从这些视图来，不读 `~/.parhelion`、`~/.judge`，不碰平台内部）。
- 不需要 problem、也不需要 worker 绑定：`help` 在 `_caller()` 之前分流，任何调用者都能问（测试覆盖了未绑定的 worker 身份）。

### 3.2 其余描述按「一句话说做什么 + 必要的约束」重写

原则：工具名前缀已经给了名字，描述里不再重复；参数只在其名字、类型、enum 说不清时才留一句话。举例：

- `parhelion_wait`：「…Prefer this over looping parhelion_status, agent_wait or test_wait; omit timeout and the wait lengthens itself (max 100s).」
- `parhelion_test` 的 `level` 描述（保留，且是唯一一处写全三档的地方）：`smoke: quick check; standard (default): all cases plus warm timing; final: candidate validation`。
- `parhelion_submit_gate`：「a call is a preview, enqueue=true queues the SHA when it passes」，保住预览与入队的区别。
- `escape_*` 保留各自的既有规则：`rebind` 的「only while the bound repair was neither submitted nor queued」、`status` 的 reviewed-trial 绑定、`reproducer` 的「confirm and verify a repair」。
- `parhelion_ledger` 的 `view` 是唯一没有 enum 的参数，它的一句话说明保留。

### 3.3 只改描述，外加一处被明确允许的结构变化

用脚本把 `git show HEAD~1` 的旧表和新表逐层比对（去掉所有 `description` 后比较）：32 个工具的 `name`、`roles`、`inputSchema`（属性名、类型、`minimum`/`maximum`、`enum`、`required`、`items`）**全部逐字一致**，唯一的非描述差异是：

```text
parhelion_report.inputSchema.properties.view.enum:  [...,"env"] -> [...,"env","help"]
```

即 §3.8 认可的「`view` 仍是 string，只是多一个取值」。工具数仍是 29 / 24，`list_tool_schemas` 的角色过滤一行未动。

## 4. 逐工具字节表

32 行 = 主控 29 个 + 3 个 worker-only（`agent_report`、`squad`、`agent_inbox`）；「worker」列是「该角色能否看到」。按改前字节降序。

| 工具 | 改前 | 改后 | 变化 | worker | 牺牲了什么 |
|---|---:|---:|---:|---|---|
| `parhelion_report` | 3,112 | 806 | -2,306 | 是 | 逐视图列说明移入 `view="help"`；P15 那句话压成一句 |
| `parhelion_note` | 2,142 | 1,610 | -532 | 是 | 删逐类型字段映射（见 §7）；保留 240 字符 claim 上限与 supersedes 规则 |
| `parhelion_squad` | 1,579 | 549 | -1,030 | 是 | 保留信封字段与 1200 字符附件规则；删 recipient/to 优先级、request_id 去重、message 旧写法 |
| `parhelion_test` | 1,577 | 772 | -805 | 是 | 保留三级区分；删 `compare`（A/B 配对）、`force_rerun` 的「需 reason」、`remeasure`、`format` |
| `parhelion_agent_peek` | 1,286 | 601 | -685 | — | 保留四个 `what`、worktree 相对路径限制、8 KB 上限；删单文件 256 KB 与「用更窄 lines」 |
| `parhelion_wait` | 1,278 | 692 | -586 | 是 | 保留「优先于循环 status」；删无变化时回包字段、`until`/`timeout`/orchestrator-only（`since` 保留） |
| `parhelion_status` | 1,263 | 489 | -774 | 是 | 保留 `if_rev` 与「等待用 parhelion_wait」；7 个参数的单句说明全删 |
| `parhelion_test_plan` | 1,160 | 737 | -423 | 是 | 已弃用：删 `mode` 映射与 `suite` 说明 |
| `parhelion_kb_query` | 1,125 | 705 | -420 | 是 | 删每页条数（3/8）与 `full` 为 `format` 别名 |
| `parhelion_agent_spawn` | 1,037 | 611 | -426 | — | 删全部参数说明（squad 归组、collab_mode、worktree_mode、axis 等） |
| `parhelion_kb_record` | 952 | 885 | -67 | 是 | 删「acceleration」「strictly」 |
| `parhelion_sprint_start` | 877 | 592 | -285 | — | 保留 `mode` 三档与自动提交开关；参数描述压到一行 |
| `parhelion_ledger` | 794 | 484 | -310 | 是 | 保留 `view` 语法（唯一没有 enum 的参数）；删 orchestrator-only 与分页说明 |
| `parhelion_spill_read` | 787 | 334 | -453 | 是 | 删 `offset`/`limit` 默认值 |
| `parhelion_submit_gate` | 776 | 519 | -257 | 是 | 保留「预览 vs 入队」；删 `hypothesis_id`/`predicted_effect` 示例 |
| `parhelion_agent_wait` | 696 | 407 | -289 | — | 删 `timeout_sec` 默认/上限与 `strategic_only` 语义 |
| `parhelion_escape_rebind` | 657 | 377 | -280 | 是 | 保留「须未提交未入队」；删 `receipt`/`sha`/`review` 单句说明 |
| `parhelion_test_wait` | 635 | 389 | -246 | 是 | 删 `format` 两档说明 |
| `parhelion_agent_messages` | 631 | 390 | -241 | — | 删 `limit` 默认 60 |
| `parhelion_recall` | 629 | 377 | -252 | 是 | 删 `refresh` 与 orchestrator-only 说明 |
| `parhelion_agent_report` | 597 | 463 | -134 | 是 | 压成一句 |
| `parhelion_kb_feedback` | 516 | 487 | -29 | 是 | 删「Bayesian promotion/demotion」的具体机制 |
| `parhelion_escape_reproducer` | 441 | 297 | -144 | 是 | 顶句压短（删「local」「bad SHA」的重复表述） |
| `parhelion_bench_diagnose` | 407 | 265 | -142 | 是 | 删 `verbose` 说明 |
| `parhelion_pmu_card` | 401 | 249 | -152 | 是 | 顶句压短；删 `top_n` 默认 5 |
| `parhelion_escape_status` | 381 | 284 | -97 | 是 | 删状态枚举展开（pending/reproduced/fixed） |
| `parhelion_agent_send` | 346 | 306 | -40 | — | 删「generalization hint」与「external Harness」 |
| `parhelion_elf_cold3_check` | 334 | 259 | -75 | 是 | 只删了一个形容词，语义不变 |
| `parhelion_agent_stop` | 289 | 261 | -28 | — | 删「its isolated」 |
| `parhelion_agent_inbox` | 269 | 235 | -34 | 是 | 压短 |
| `parhelion_test_cancel` | 229 | 218 | -11 | 是 | 加「your own」，只删了几个词 |
| `parhelion_harness_pool` | 219 | 168 | -51 | — | 压短 |

（单工具字节是 `json.dumps({name, description, inputSchema})` 的紧凑字节；数组序列化再为每个工具加 2 B 分隔与括号，所以 29 个主控工具的子项和 14,571 B 对应表头 14,601 B。）

## 5. worker 首次提示推算（P16 §4.2 口径）

P16 实测：sprint worker 的工具表 27,911 B，其中平台切片 8,322 B（15 工具），首次提示 9,843 token（系统提示 2,829 B + 工具表 + brief 1,762 B = 32,502 B），工具表占 86%。由此：

- 非平台（内置）部分 = 27,911 − 8,322 = **19,589 B**，标定 32,502 / 9,843 = **3.302 B/token**。
- 今日平台切片 22,066 B → 工具表 41,655 B → 首次提示 46,246 B ≈ **14,005 token**，工具表占 90.1%（与 P16 的「约 41.7 KB」一致）。
- 本包后平台切片 12,507 B → 工具表 32,096 B → 首次提示 36,687 B ≈ **11,110 token**，工具表占 87.5%。

即 worker 首次提示 **−9,559 B ≈ −2,895 token（−20.7%）**，平台切片本身 −43.3%（≈ −2,895 token/次调用，因为表随每次调用发送）。主控侧同口径：平台切片 20,864 → 14,601 B（−6,263 B，−30%）。

## 6. 测试与验证

两条全量（在提交后的树上跑）：

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
787 passed, 19 skipped, 2 subtests passed in 76.60s (0:01:16)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
540 passed in 59.87s
```

基线是 785 / 19 与 538，两处各 +2，正是新增的两条：

1. `test_tool_table_stays_within_the_context_budget`（`cli/tests/test_mcp_server.py`）：按 §3.8 的口径序列化两个角色，断言 ≤ 15,000 / ≤ 13,000，并顺带钉住 29 / 24 的工具数。失败信息直接指向该改哪里（压描述、把列说明搬进 `view="help"`、或用理由抬预算）。
2. `test_view_help_documents_every_column_without_spilling`（`cli/tests/test_report_views.py`）：`help` 回复 < 4,096 B（低于输出上限、不会 spill）、覆盖 `VIEWS − {help}` 的全部视图、`per_case`/`diff` 的列名与 `PER_CASE_COLUMNS`/`DIFF_COLUMNS` 顺序一致、每列都有非空说明、未绑定的 worker 也能问。

**没有改任何既有断言。** `test_report_tool_is_registered_for_both_roles` 原先断言 `enum == list(handlers_report.VIEWS)` 且每个取值都出现在描述里；`help` 同时进了 `VIEWS` 和描述，所以它原样通过。全仓也没有别的地方按视图名白名单分支（`grep per_case` 只剩视图模块和 `per_case_us` 字段）。

stdio 最小调用（工作树内，真实比赛工作区，只跑 `tools/list` 与 `view="help"`，不触碰 `queue` 视图，因此不会经 `cli_bridge status` 写任何日志）：

```text
$ python cli/opsb mcp serve --workspace /Users/winbeau/Competitions/mat-chem-sim-pred
initialize -> serverInfo {'name': 'parhelion-mcp', 'version': '4.1.0'}, protocol 2024-11-05
tools/list -> 29 tools, 14601 B compact
tools/call parhelion_report(view="help") -> isError False, reply 2452 B, spilled False
help keys: ['view', 'columns', 'views']; per_case columns: 19
```

没有改 Swift，因此没有跑 `swift build`。

## 7. 「描述压到 24% 后还够用」的诚实评估

描述文本从 13,184 → 4,267 字符（主控，32%），worker 11,776 → 3,553 字符（30%）。**够用，但不是无损**，下面的取舍是明确做的：

**仍然完整的地方。** 每个工具都还有一句能独立读懂的话；任务书点名必须保留的四条语义都在（`wait` 的「优先于循环 status」、`test` 的三级区分、`report` 的 P15 数据通道、`submit_gate` 的预览 vs 入队），`escape_*` 各自的既有规则也在。参数层面的 `enum`、类型、上下限一个没少——模型仍能从 schema 本身知道合法取值。

**被压到只剩最小语义的工具。** `harness_pool`（168 B）、`agent_inbox`（235 B）、`pmu_card`（249 B）、`elf_cold3_check`（259 B）、`agent_stop`（261 B）、`test_cancel`（218 B）、`kb_feedback`（487 B）、`kb_record`（885 B）现在只有「做什么」，没有任何「怎么用」的提示。这几个都是低频或零参数的，我判断代价可接受。

**真正有可发现性损失的四处**（按我认为的风险排序）：

1. **`parhelion_note` 的逐类型字段映射没了。** 原来一句话列出「hypothesis 用 status/test_plan/priority、insight 用 actionable_next/est_gain/cases、failure 用 approach/why_failed/cost_spent/retry_conditions、decision 用 chosen/rejected/reason、constraint 用 text/rule_source」（约 200 B）。现在模型只能看到 33 个扁平属性名，靠名字猜哪个属于哪个类型。**缓解**：猜错不会写坏数据——`ledger.model.build` 会拒绝该类型不认识的字段，并回一句 `unknown field(s) for <type>: X`，代价是一次往返而不是一条错账；`ledger` 的视图和 brief 里也还有用法示例。要补回来大约 +200 B，主控若要补，请同时抬 §6 的预算或在别处找 200 B。
2. **`parhelion_test(compare=...)` 的 A/B 配对不可从表里发现了。** P10/P11 的成对测量以前有一句「A/B paired run against this baseline commit, or 'board'」，现在只剩 `compare: string`。这是我为 805 B 的降幅付的主要代价之一；如果主控认为 A/B 是日常操作，最便宜的回补是把 68 B 的那句放回去（余量 399 B 够）。
3. **`parhelion_report` 的 P15 那句话不再点名 `opsb_core` 与 `_all_team_sessions`。** 现在写的是「never from `~/.parhelion` or `~/.judge` files or from platform internals」。规则本身没变，执行也没变（`worker_boundary` 仍按原样检测并在协作页标红），但「import 平台模块也算越界」这一层需要模型从「platform internals」自己推。要更硬，可以把 `opsb_core` 三个字放回去（+13 B）。
4. **参数级默认值基本都撤了**（每页条数、单文件 256 KB、`offset=0`、`limit=3000`、`--wait_sec` 上限、`limit` 默认 60 等）。这些在 handler 里有默认，猜错最多多一次调用；但 `parhelion_agent_peek` 的「单文件 256 KB」和 `kb_query` 的「brief 3 条 / full 8 条」是模型用来决定「要不要翻页」的依据，撤掉后可能多问一次。我认为这是最划算的一类删减。

**给后面包的判断依据**：这 29 个工具平均每个只剩约 170 B 描述（4,896 B / 29，含键的语法开销；29 个顶句 + 8 个参数句）。再加工具时，最可能的做法不是继续压这些句子，而是走 §3.8 明确留给 4.3 的 J-3（按角色/题目后置工具），或者给高频工具做「返回里带 hint」的第二通道（`report(view="help")` 就是这类做法的样板）。

## 8. 守则核对（没改什么）

- 只在本工作树改文件、只提交到 `impl/P20-tool-table`；没有合入 `main`、没有 push、没有在主检出切分支或提交。
- 没有改 `aoci.txt` / `aoci.meta.txt` / `aoci.code.txt` / `.aoci/`；没有调用 `aoci_maintain` / `aoci_update_entry`（本包没有新增文件，索引用 `git`/`grep` 就能定位）。
- 没有重启/停止守护进程、没有 `daemon reload`、没有派发或停止 worker、没有清总线、没有 `queue approve`/`force`、没有提交判题、没有改自动提交开关。
- 没有安装插件或应用、没有写 `~/.dsh`、没有开子代理或新工作树。
- 回放数据、总线库、真实比赛数据只读；stdio 验证只发 `initialize`、`tools/list`、`report(view="help")`，其中 `help` 在 `_caller()` 之前分流，不读工作区；没有对真实比赛目录跑 `cli_bridge status`，也没有用 `queue` 视图做验证。
- 仓库里没有出现真实设备值（报告与代码里只有 `opsb_core`、`~/.parhelion` 这类平台路径）。

## 9. NOTE（发现的非显然约束）

- **`VIEWS` 是 `view` enum 的唯一真源。** `test_report_tool_is_registered_for_both_roles` 断言 `enum == list(handlers_report.VIEWS)`，所以加视图时必须两处一起改；`help` 就是按这条走的。`help` 必须在 `_caller()` **之前**分流，否则未绑定身份的 worker 会被 `_caller` 拒掉——它本来就不需要 problem。
- **删掉 `description` 键比留空串便宜 17 B/个。** 97 个嵌套键从「空串」变成「不存在」省下 1,649 B，这正是本包能在 4,896 B 描述文本下压到 14,601 B 的原因；如果以后有人恢复「所有参数都写说明」的习惯，会立刻撞上 §6 的测试。
- **`parhelion_report` 的 schema 描述仍必须列出每个 `view` 取值。** 那条既有断言逐个检查「视图名出现在描述里」，包括 `help`；这是唯一一条间接约束 schema 文案结构的测试。
- **`mcp.output_cap_bytes` 默认 5,000，`help` 回复 2,452 B。** 若以后把列说明扩到 5,000 B 以上，它会先被 `paging.repage`（`parhelion_report` 是分页工具）处理再走 spill；`help` 没有分页键，实际会直接 spill。所以 §6 的测试同时钉了 4,096 B 上限。
- **平台切片不是 worker 的全部工具表。** P16 §4.2 里 41.7 KB 是「内置 19,589 B + 平台切片」；本包只动平台那一半，所以首次提示降幅（−20.7%）小于平台切片降幅（−43.3%）。要继续压首次提示，下一步只能动内置工具或后置策略。

## 10. 已知风险与建议

- **余量只有 399 / 493 B。** 这是有意的：测试就是闸门，任何新增工具或加长描述都会红。代价是主会话若在审查中想放宽某条描述，必须同时在其他地方省出同样的字节，或明确抬预算并在提交信息里写理由。
- **A/B 配对（`compare`）与类型字段映射**是两处最值得考虑回补的语义（合计约 270 B）；只要余量够就能放回，建议由主会话在审查时决定，而不是本包自行判断「够不够日常」。
- **§7 第 3 条**（P15 那句话不再点名 `opsb_core`）建议主会话过目一次：检测逻辑没变，但文案弱了一层。若要补，+13 B。
- **没有真机验证**：本包只改工具表与工具自己的帮助回复，不涉及设备、队列、判题或门禁，所以没有需要真机的步骤；stdio 的最小调用已在工作树内跑通。
- 4.3 的 J-3（按角色/题目后置工具、工具搜索）没有做，按 §3.8 留给 4.3；本包腾出的 10,406 B 是它的起点而不是终点。
