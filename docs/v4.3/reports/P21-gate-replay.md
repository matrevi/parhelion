状态：已交付

# P21 门禁回放基准与 4.3 指标

`opsb replay gate` 在比赛数据的副本上把每一发已判回执按它自己的 `submitted_at` 重新过一遍门禁，输出门禁统计、冲刺结果与可信度标注；两场真实数据的结果作为 4.3 的基线表。子计划五节全部实现。

## 1. 分支与提交

分支 `impl/P21-gate-replay`，基于 `main`（`c070ea1`）。

```text
$ git log --oneline main..HEAD
<deliver> Deliver P21: replay receipts through the gate on a workspace copy
b1fcba0 Keep the replay tests free of the fork deprecation warning
9ef5cc9 Test the replay against synthetic receipts and assert the workspace is untouched
9a278fb Add opsb replay gate, a repeatable ruler for the submission gate
```

第一条就是带着本报告的那个提交：提交号由提交动作本身决定，无法写进它自己的内容里，所以这里留 `<deliver>`；后三条的实现提交号是准确的。

改动文件（全部为新增或只加接入点）：

| 文件 | 行数 | 说明 |
|---|---:|---|
| `cli/opsb_core/replay/gate_replay.py`（新） | 332 | 副本准备、清单、逐发重放、两条开关组、并行 |
| `cli/opsb_core/replay/gate_metrics.py`（新） | 186 | 门禁统计 / 冲刺结果 / 可信度三块纯函数 |
| `cli/opsb_core/commands/replay_cmd.py` | +68 | `opsb replay gate` 注册与文本输出 |
| `cli/tests/test_replay_gate.py`（新） | 257 | 12 条测试（测试文件不受 350 行限制） |

两个新模块分别 332 与 186 行，均在 350 行以内。没有改 `scheduler/`、`mcp/`、Swift 或任何门禁代码；本包不含门禁行为改动，因此不需要新的 `GateConfig` 开关。

## 2. 子计划逐项完成情况

### 2.1 新模块与新命令 — 已完成

`cli/opsb_core/replay/gate_replay.py` + `cli/opsb_core/commands/replay_cmd.py`（注册在 `register_replay_subcommands`，解析器集中在 `replay_cmd.py`；`cli_app.py` 已有 `register_replay_subcommands`，未改）。

参数与子计划表一致：`--workspace`／`--board-mode best|latest`／`--gate NAME=VALUE`（可重复）／`--since`／`--until`／`--json`。另加四个：`--manifest`（见 2.2）、`--groups`（默认 `default,420`）、`--jobs`（默认 6）、`--detail`／`--keep-copy`（调试用）。`--gate` 的含义与 `opsb queue config --gate` 相同，走同一个 `GateConfig.set_switch`，只作用于本次回放。

### 2.2 严格只读 — 已完成

回放前把这些复制到 `tempfile.mkdtemp(prefix="opsb-gate-replay-")`：

- `results/` 整棵树（`feedback-log.jsonl`、候选档案、`<title>/.case-map.json`、`bench-runs/`、judge-compile 记录都在里面，门禁的写入只会落到副本）；
- 题目清单：原工作区有 `opsb.workspace.json` 就原样复制，没有就按 `load_problem_manifest` 的结果在副本里生成最小清单；
- 平台 `bench-specs`（S1 的 reference 套件从这里读），复制到副本专属的 `PARHELION_HOME` 下；
- 每个题目仓库的**只读对象克隆** `git clone --shared --no-checkout`（向源仓库只读对象，克隆本身写在新目录；实测单仓约 28 ms、100 KB 量级）。

回放期间 `PARHELION_HOME`／`OPSB_HOME` 指向副本内的目录（并把 `config.json` 的 `device` 段原样带过去，不复制其它凭据字段），结束（含异常）时恢复环境变量并 `shutil.rmtree` 副本。`--keep-copy` 才保留并打印路径。

测试断言：原工作区每个文件的 `(st_mtime_ns, st_size)` 前后完全一致；平台的 `PARHELION_HOME` 前后完全一致且没有多出 `workspaces/`；副本目录已删除；工作区里没有出现 `feedback-log.jsonl` 或 `.case-map.json`。

ops-bench 没有 `opsb.workspace.json`（只有 `results/` 与 `ict-cann-nov/problems.json`）。取舍：**不新增必需参数**。`load_problem_manifest` 已经能从 device 配置的 work-prefix 找到 `ict-cann-nov/problems.json`，所以副本里按它的结果生成最小 `opsb.workspace.json`（`manifest: "generated"`）；只有清单在别处时才用 `--manifest`（`manifest: "arg"`），已经有 `opsb.workspace.json` 时原样复制（`manifest: "workspace"`）。两场实测分别落到 `generated` 与 `workspace`。

### 2.3 逐发重放 — 已完成

回执取 `scheduler.receipts.canonical`（按 `submission_id` 去重），按 `submitted_at`（缺失时退回 `finished_at`／`queued_at`）排序。每一发调用 `SubmissionGate._evaluate_impl(title, sha, as_of=<submitted_at>, override_board_score=…, override_board_status=…)`。

榜面在**每一发之前**由真实回执重建：`latest` 取上一发的结果（失败记 0，`Pass` 但无分时保留上一个已知榜面并在 JSON 里标 `board_source="unknown"`），`best` 取历史最高。榜面与回放判定完全解耦——被拦下的发在历史里照常发生，所以这是**逐发判定，不是反事实模拟**；被拦后的历史不会照原样发生，拦截数是上界。

`Pass` 判分的取用顺序沿用 `receipts.official_score`（`score` → `timing_score.score` → `official_score`）。

### 2.4 每场输出三大块 — 已完成

- **门禁**：`by_stage`／`by_stage_code`（每个被拦的发按门禁自己的优先级 S1→S3→S2 只记一次主责阶段，另附该发所有失败阶段 `failed`）、`blocked_new_highs`（当时刷新该题历史最高）、`blocked_restores`（该 SHA 此前线上 Pass 过）、`frozen`（判题侧失败之后该题连续被 S1 拦下的发数、分布与最长连冻）、`restore`（归零榜面数 / restore 尝试数 / 获批数 / 获批率 / 每归零获批率）、`blocked_with_next_step`（被拦时门禁给出结构化 `next_step` 的发数）。
- **冲刺结果**（只依赖回执）：新高率、与峰值持平率（±0.01 分）、退步率、硬失败率（TLE/WA/RE）、分/判（榜面变化之和 ÷ 判题数，可穿透窗口）、同源重投占比、归零后恢复中位分钟、收官差额（每题历史最高 − 收盘在榜分）。
- **可信度标注**：S1 逃逸锁/TLE、S3、榜型与 restore 标 `credible`；S2 标 `approximate`，并给出有可配对证据（`estimate.matched_cases > 0`）的发数占比与估分方法的分布。

### 2.5 开关对比 — 已完成

`GROUPS = {default: {}, 420: {五个 4.2.1 开关全部翻回 4.2.0}}`，两组共享同一个副本与同一份回执历史，一次命令跑完。`--gate` 在组设定之上叠加。ops-bench 的赛题规则快照（`results/scoring-evidence/problems-all.json`）逐题写着 `latest`，所以 4.2.1 组走 latest；4.2.0 组因 `board_mode_from_rules=false` 走 best（与 4.2.0 的硬编码行为一致）。

### 2.6 测试 — 已完成

`cli/tests/test_replay_gate.py`，12 条：合成回执（三题 18 发，含 latest 归零后 restore、TLE 原样重投、判题失败后 S1 连冻、六发无增益触发熔断）、新高判定、restore 批准、只读断言、`--gate` 作用域、窗口、非法参数、`--manifest`、并行与串行一致、`gate_metrics` 手工构造的历史。

### 2.7 验收 — 已完成

两场 × 两组见 §4，R5 对照见 §5。不需要真机。

## 3. 测试

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
813 passed, 19 skipped, 2 subtests passed in 77.18s (0:01:17)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
552 passed in 61.55s (0:01:01)
```

`runtime/tests/test_device_broker.py` 的两条预算测试本轮全绿，没有触发偶发失败，因此不需要单独重跑。基线的 `801 passed / 19 skipped` 与 `540 passed` 加上本包新增的 12 条测试，分别变成 813 / 19 与 552。

Swift：本包没有改 Swift，**不需要 `swift build`**（也没有 `swift/` 下的 diff）。

## 4. 真实数据验收

不需要真机。两场都是先复制到临时目录再跑，原目录只读；下面的数字就是 4.3 的基线表。

### 4.1 两场 × 两组开关

| 指标 | ops-bench 4.2.1（latest） | ops-bench 4.2.0（best） | mat-chem 4.2.1（best\*） | mat-chem 4.2.0（best） |
|---|---:|---:|---:|---:|
| 重放发数 | 233 / 268 行 | 233 | 78 / 93 行 | 78 |
| **被拦** | **50（21.5%）** | **132（56.6%）** | **66（84.6%）** | **70（89.7%）** |
| — S1 SAFETY | 9 | 14 | 42 | 43 |
| — S2 NOISE_FLOOR | 41 | 105 | 24 | 27 |
| — S3 CIRCUIT_BREAKER | 0 | **13** | 0 | 0 |
| 阶段码（主责） | `BELOW_NOISE_FLOOR` 41、`ESCAPE_LOCK_ACTIVE` 6、`MISSING_CROSS_CORE_STRESS` 2、`MISSING_EXTENDED_PASSED` 1 | `BELOW_NOISE_FLOOR` 105、`CIRCUIT_OPEN_SWITCH_BASELINE` 13、`ESCAPE_LOCK_ACTIVE` 11、`MISSING_CROSS_CORE_STRESS` 2、`MISSING_EXTENDED_PASSED` 1 | `REFERENCE_SUITE_REQUIRED` 27、`BELOW_NOISE_FLOOR` 24、`EXIT_CODE_2` 9、`ESCAPE_LOCK_ACTIVE` 6 | `REFERENCE_SUITE_REQUIRED` 27、`BELOW_NOISE_FLOOR` 27、`EXIT_CODE_2` 9、`ESCAPE_LOCK_ACTIVE` 7 |
| **blocked_new_highs** | **18**（S2 13、S1 5） | **39**（S2 31、S1 6、S3 2） | 35 | 37 |
| blocked_restores | 10 | 33 | 8 | 8 |
| frozen（合计 / 最长） | 1 / 1 | 3 / 1 | 24 / 3 | 25 / 3 |
| 榜面归零次数 | 15 | 0（best 不会归零） | 0 | 0 |
| restore 获批 / 尝试 | **9 / 9（100%）** | — | — | — |
| 被拦时下一步非空 | 6 / 50（12%） | 11 / 132（8.3%） | 6 / 66（9.1%） | 7 / 70（10%） |
| 新高率 | **35.6%**（83） | 35.6%（83） | 33.3%（26） | 33.3% |
| 持平率 | 0.4%（1） | 0.4%（1） | 2.6%（2） | 2.6% |
| 退步率 | 50.2%（117） | 50.2%（117） | 20.5%（16） | 20.5% |
| 硬失败率 | **6.4%**（15） | 6.4%（15） | 24.4%（19） | 24.4% |
| 分/判（全窗口） | 742.14 / 233 = **3.185** | 752.33 / 233 = 3.229 | 374.94 / 78 = 4.807 | 374.94 / 78 = 4.807 |
| 同源重投占比 | 15.0%（35） | 15.0% | 12.8%（10） | 12.8% |
| 归零后恢复中位 | **20.3 min** | — | — | — |
| 收官差额 | **10.19** | 0.00（best 定义） | 0.00 | 0.00 |
| S2 可配对占比 | 186 / 233 = **79.8%** | 79.8% | 26 / 78 = **33.3%** | 33.3% |

\* mat-chem 工作区没有规则快照（`results/scoring-evidence/` 下没有 `problems-all.json`），`board_mode_from_rules=true` 读不到规则时回落 `best`，所以「4.2.1 默认」在这一场实际就是 best。榜型还未知，见 §9 NOTE 4。

补充（不是必需的两组，用来回答具体问题）：

| 补充 | 用途 | 结果 |
|---|---|---|
| mat-chem + `--board-mode latest` | 给 latest 口径的 G4/G5 | 拦 64/78（82.0%）；归零 17 次、restore 1/1 获批（每归零获批率 5.9%）；归零后恢复中位 390.1 min；收官差额 **37.64** |
| ops-bench + `--since 2026-11-10T11:13` | 与 R1 的 331.36 分/231 对表 | 194 发；涨幅 **327.96**（R1：331.36，差 1.0%）；按 00-PLAN 的除法（窗口涨幅 ÷ 全部判题数 233）= **1.41 分/判**（G7 基线 1.43，差 1.5%） |

### 4.2 00-PLAN §2 的 G1–G5、G7

| # | 基线（00-PLAN） | 4.3 目标 | 本次实测 | 判定 |
|---|---|---|---|---|
| G1 | 4 发（R5 手算，仅熔断） | 0 | ops-bench 4.2.1 **18**（S3 0）；4.2.0 **39**（S3 2） | 未达标（口径见 §5） |
| G2 | 13/231 = 5.6%（仅熔断） | ≤3%，且每发被拦都有可执行下一步 | ops-bench 4.2.1 **21.5%**、4.2.0 **56.6%**；结构化下一步只有 12% / 8.3% | 未达标 |
| G3 | TLE 永久冻结 | ≤1 发 | ops-bench 4.2.1 **1**（最长连冻 1）；4.2.0 3 | 达标（ops-bench） |
| G4 | 只在确认归零时放行 | 100%（有线上 Pass SHA 时） | ops-bench latest **9/9 = 100%**（15 次归零）；mat-chem latest 1/1 = 100%（17 次归零） | 达标 |
| G5 | ops-bench 10.19 裸分 | 下一场为 0 | ops-bench latest **10.19**（与基线完全一致，说明回放口径对得上）；mat-chem latest 37.64 | 基线复现 |
| G7 | 新高 35.6%、硬失败 6.4%、1.43 分/判 | 新高 ≥35%、硬失败 ≤6%、编译 TLE 0 | ops-bench **35.6% / 6.4%**；窗口口径 **1.41 分/判** | 复现 |

G6 属 P29，G8（工具表字节）本包不改 MCP 工具与参数，字节数不变（主控 14601 / worker 12507），未动上限。

### 4.3 可信度

- **可信**：S1 逃逸锁与 TLE（只读回执与结果目录）、S3（只读回执）、榜型与 restore（只读回执）。ops-bench 的 S1 全部是回执与本地结果目录能证实的事实；mat-chem 的 S1 有一类是「今天的套件读不回当年的证据」（见 §6 偏差 1）。
- **近似**：S2。ops-bench 186/233 发的估计方法带配对（`pairwise_simplex_v2` 177、`historical_sha_decay_adjusted` 33、`anchor_fallback` 12…），可配对占比 **79.8%**；mat-chem 只有 **33.3%**（`anchor_fallback` 42 发为主），S2 的拦截数在那场只能当方向性参考。

## 5. 与 R5 的 13/231 对照

先把三个数字分清：

| 口径 | 数 | 说明 |
|---|---|---|
| R5 §4 手算 | 13 / 231 | **只数熔断（S3）**，用衰减后的同期峰值 |
| 00-SUMMARY §4.3 复算 | 18 / 231 | 声明用「原始历史峰值」，称口径更严 |
| 本次 4.2.0 组 | **13 / 233**（S3） | 走门禁自己的 `get_circuit_breaker`（衰减后的同期峰值） |
| 本次同口径只保留有 `submitted_at` 的行 | 11 / 231 | 2 发 `Pass` 无分、无 `submitted_at`（`e635a6c`/`121c9c4`）被排除后 |
| 本次「原始峰值」复刻 | 15 / 233 | 同一规则把 `contemp_sc` 换成原始分（不加衰减），其余不变 |

**13 对上了，但成员不完全对。** 本次 S3 拦下的 13 发是：`3267c09`、`0aa17ab`（86.61，新高）、`e635a6c`、`121c9c4`、`dfa784f`、`ecf4af4`、`252b13e`、`e9cdd3b`、`6e9bf87`、`39b588e`、`91dd243`、`76e5a68`、`a031e4e`（72.45，新高）。R5 点名的 4 发「当时新高」在本次回放里的归宿是：

| R5 的高分 SHA | 本次判定 | 原因 |
|---|---|---|
| `0aa17ab`（QuantUpdateScatter 86.61） | S3 拦下 ✓ | 与 R5 一致 |
| `a031e4e`（StridedSliceGrad 72.45） | S3 拦下 ✓ | 与 R5 一致 |
| `ba4a357`（StridedSliceGrad 71.57） | **S1** 逃逸锁拦下 | 门禁 S1 先于 S3，主责阶段记 S1；只看 S3 会漏掉它——这是「只数熔断」口径的系统性低估 |
| `1dc3411`（PadV3Grad 42.77） | 放行 | 该时刻 12h 窗口内只有 5 发连续非改进：`12:37` 的 39.96 在衰减口径下算「改进」（11-18 的峰值 40.38 衰减后已低于它），计数器归零，之后 13:36、13:52、14:15、14:48、15:04 五发才到 5/6，因此 `CLOSED`；只有不加衰减的原始峰值口径才会到 6/6 |

**18 复现不出来。** 我把同一规则换成原始分（不加衰减）重算，得到 **15 / 233**：正好是本次的 13 加 `1dc3411` 与 `ba4a357`（就是 R5 另外两发新高）。再往上没有别的变体（去掉 12h 窗、用 `timing_score` 补分、按 231 行）能到 18。结论：13（衰减）与 15（原始）都是数据支持的，18 比数据多 3 发，来源无法从这批回执复现，很可能混入了 S1/S2 的拦截或手算时的窗口边界。**这正是 P21 要解决的问题**——以后 4.3 的门禁包一律报「全阶段 + 分阶段码 + S3 单列」三个数，不再用一个数字指代门禁。

另外两处 R5/调研的判断被回放证实或修正：

- 00-SUMMARY §6.2 把「`dcc7754`（CTCLossV2 75.11）会不会被逃逸锁拦下」列为【推断】。回放确认：**会**，而且 4.2.1 默认组也拦（该题的逃逸锁来自一次 RE，不是 TLE，所以 4.2.1 的 TLE 修复救不了它）——这正是 P24 要做的出口。
- 00-SUMMARY 说「一次判题侧 TLE 就能永久冻结整题」。ops-bench 4.2.0 组冻结 3 发（最长 1），4.2.1 组 1 发，说明 4.2.1 的 TLE 修复方向正确；G3 在 ops-bench 已达标。
- 4.2.1 默认组仍有 18 发被拦的新高，其中 **5 发是逃逸锁**（`af0807c` +16.53、`de25877` +10.58、`ba4a357` +3.94、`dcc7754` +2.58、`0d6ea06` +1.72），13 发是噪底（含 `ec0867d` 同源重判 +15.96）。这两类正是 P24 与 P23/P29 的目标。

## 6. 回放的已知偏差

1. **as-of 恢复不了当年的套件与仓库状态。** `bench-specs`、`results/scoring-evidence/problems-all.json`、题目仓库的 `reference.py` 都是**今天**的。mat-chem 的 27 发 `REFERENCE_SUITE_REQUIRED` 就是这一类：回执里的 `result.json` 没有 `scheme=reference`（早于 reference 取证），而今天的仓库 `reference.py` 让 S1 要求它。这类拦截**不是**当天门禁的判定，报告里单独标注；同理 `EXIT_CODE_2`（9 发）是当天本地验证真的失败，属于可信拦截。ops-bench 的仓库没有 `reference.py`，不受影响。
2. **S3 的 HALF_OPEN 分支带「未来知识」。** `SubmissionGate._prior_online_pass_shas` 是构造时对全历史扫描的，没有 `as_of`；S3 的 `HALF_OPEN_UNPROVEN_SHA` 用它判断「该 SHA 此前线上过没过」，回放里可能看到 as_of 之后的 Pass，从而少拦。本次两组都没有出现 HALF_OPEN 拦截，实际影响为 0，但这是已知面。
3. **熔断读的榜面与门禁读的榜面不是同一个来源。** `get_circuit_breaker(title, as_of=…)` 内部自己 `get_problem_ledger(as_of=…)`，不带 `override_board_score`；latest 下它的衰减锚点可能与我们注入的榜面差一拍。影响只到 S3 的 `gamma_p`，量级很小。
4. **仓库用 `--no-checkout` 对象克隆。** 所有 `git show/ls-tree/cat-file` 路径都正常（S1 的树校验、跨核静态扫描、`catalog()` 的 oracle），但任何读**工作树文件**（不带 revision）的路径会看到空树。实测 ops-bench 两场都与线上一致（线上也没有 `reference.py`）。
5. **只重放已判回执。** `rejected`/`skipped`/`duplicate`/`sync_error` 没到过判题站，不算「发」。两场分别排除 35 / 13 行。mat-chem 另有 2 行没有 sha（无法解析），ops-bench 有 2 行没有 `submitted_at`（用 `finished_at` 定位，JSON 里有 `shots.used_fallback_time=2`）。
6. **`Pass` 无分的发。** ops-bench 有 7 条（`[V3-025]`），榜面保留上一个已知值并在 `board_source="unknown"` 标出；它们对后续发的榜面影响因此是「乐观」的。
7. **不写 `feedback-log.jsonl`。** 回放调用 `_evaluate_impl`（纯判定）而不是 `evaluate`（会落日志），所以「同一发只记一次判决变化」这条去重逻辑没有被回放覆盖。
8. **窗口回放的收官差额只算窗口里出现过的题**；新高/持平/退步率以窗口起点为基线（榜面本身会用窗口前的发预热）。

## 7. 用法

```text
opsb replay gate --workspace <dir> [--board-mode best|latest] [--gate NAME=VALUE]...
                 [--since ISO] [--until ISO] [--manifest PATH] [--groups default,420]
                 [--jobs N] [--detail] [--keep-copy] [--json]
```

| 参数 | 说明 |
|---|---|
| `--workspace/-w` | 比赛目录（必填，只读） |
| `--board-mode` | 强制全部题目按 `best` 或 `latest`；省略时逐题读规则快照，读不到回落 `best` |
| `--gate NAME=VALUE` | 可重复，含义与 `opsb queue config --gate` 相同，只作用于本次回放（不写 config.json、不写桥接状态） |
| `--since` / `--until` | 按 `submitted_at` 的时间窗；不带偏移按 UTC |
| `--manifest` | 清单不在工作区里时显式指定 |
| `--groups` | 要对比的开关组，默认 `default,420` |
| `--jobs` | 按题并行的进程数，默认 6 |
| `--detail` | JSON 里带上逐发记录 |
| `--keep-copy` | 保留副本并打印路径（排查用） |
| `--json` | 机器可读输出 |

例：

```bash
# 两场基线
PYTHONPATH=cli:. ~/.parhelion/venv/bin/python cli/opsb replay gate --workspace ~/Competitions/ops-bench
PYTHONPATH=cli:. ~/.parhelion/venv/bin/python cli/opsb replay gate --workspace ~/Competitions/mat-chem-sim-pred

# 只看一个门禁开关的效果（同样只作用于本次回放）
... replay gate -w ~/Competitions/ops-bench --groups default --gate approve_low_gain_positive=false

# 与 R1 的对表窗口
... replay gate -w ~/Competitions/ops-bench --since 2026-11-10T11:13:00 --json > window.json
```

JSON 顶层：`workspace`、`copy`（副本、清单来源、仓数、复制耗时）、`window`、`shots`（行数/已判/重放/排除原因）、`board_mode.requested`、`groups.<name>`（`switches`、`board_mode`、`per_problem`、`gate`、`outcome`、`credibility`、`shots_detail`）、`elapsed_sec`。

## 8. 单场回放耗时（本机，`--jobs 6` 默认）

| 场 | 复制 | 回放 | 总 |
|---|---:|---:|---:|
| ops-bench（233 发 × 2 组 × 10 题） | 1.4 s | ≈ 67 s | **68.9 s** |
| mat-chem（78 发 × 2 组 × 14 题） | 2.3 s | ≈ 2.5 s | **4.8 s** |
| ops-bench 窗口（194 发 × 1 组） | 1.3 s | ≈ 53 s | 54.0 s |

时间里几乎全是估计器的 Simplex-MAP 标定：它按 (题目, 历史长度) 缓存，所以越到赛程后段越贵（ops-bench 尾部单发约 1.7 s，个别 5–7 s）。因此按题并行：串行（`--jobs 1`）在本机超过 10 分钟（没跑完就中止，未精确计时），`--jobs 6` 是 1 分钟级。后续每个门禁包跑一次，成本可接受。

## 9. NOTE

1. **ops-bench 的榜型其实是有记录的**：`results/scoring-evidence/problems-all.json` 逐题写着 `ranking_submission_mode: "latest"`。00-PLAN §1 说「榜型只能从赛题抓取拿到，当前工作区没有记录」，对 2026-11-26 那场成立，对 ops-bench 不成立——4.2.1 的 `board_mode_from_rules` 在 ops-bench 上确实会走 latest。P22 落地规则快照时可以直接复用这个文件。
2. **4.2.1 默认组在 ops-bench "latest" 下的拦截率 21.5% 里，41 发是 S2，且其中有 13 发是"当时新高"**。这些新高的涨幅分布极端：13 发噪底拦截里，9 发涨幅在固定噪底 2.5 分以内（`+0.03`、`+0.25`、`+0.28`、`+0.32`、`+0.97`、`+1.24`、`+1.42`、`+1.69`、`+2.41`），4 发超过（`+3.79`、`+4.63`、`+6.69`、`+15.96` 同源重判）；另有 5 发是逃逸锁（`+16.53`、`+10.58`、`+3.94`、`+2.58`、`+1.72`）。也就是说：**4.3 想让 G1 到 0，噪底那条线必须改成按 σ̂ 定价而不是固定 2.5 分**（与 00-SUMMARY §4.9 一致），否则挡住的既有噪声也有真涨幅。
3. **只有 S1 的 `ESCAPE_LOCK_ACTIVE` 与 `TLE_SHA_REPEAT` 带结构化 `next_step`**，S2 的所有拦截和 S3 的熔断拦截都没有。所以 G2 的「每一发被拦都有可执行的下一步」在 4.2.1 上只有 6/50。P23 若要满足 G2，需要在 S2/S3 的 `StageResult.metrics` 里补 `next_step`，本包只负责测量。
4. **mat-chem 那场的榜型未知**：工作区没有规则快照，`opsb.workspace.json` 也没写。默认（best）与 `--board-mode latest` 的收官差额分别是 0.00 与 37.64，差别很大。P22 落地规则快照后应能判定；在此之前，这场只把 `latest` 的那一行当参考。
5. **`--since/--until` 不带偏移按 UTC**；第一版直接 `_parse_iso` 会得到 naive datetime 并在比较时抛 `TypeError`，已修（`_bound()`），测试未覆盖这条（时间窗测试用的是带 `Z` 的字符串）。
6. **mat-chem 的 `frozen` 24–25 发主要是 S1 证据格式拦截带来的**（连冻最长 3），不是「TLE 永久冻结」。用它评估 G3 时要按阶段码拆开看。
7. **榜单口径核对成功**：ops-bench latest 的收官差额 10.19 与 00-SUMMARY §4.1 的「10.19 裸分」逐分一致，说明榜面重建（上一发结果 / 历史最高）与回执取分是对的；best 组的总分 752.33 也与 00-SUMMARY 的收尾裸分一致。

## 10. 风险与后续

- **风险：S1 在 mat-chem 上主要测的是「证据格式漂移」。** 27/78 的 `REFERENCE_SUITE_REQUIRED` 说明旧回执的取证格式与今天的套件不兼容。任何拿 mat-chem 的 S1 数字做的门禁比较都要先剔除这一类，否则会得出「4.2.1 拦了 85%」的错误结论。
- **风险：`_prior_online_pass_shas` 的未来知识**（§6 偏差 2）会让 4.2.0 组的 HALF_OPEN 拦截偏少；本次两组都没有 HALF_OPEN 拦截，但如果后续包的开关组合更多地走进 HALF_OPEN，需要在 `gate.py` 里给出 as_of 版本（那属于门禁改动，本包没动）。
- **后续：P23/P24/P25/P29 的验收统一用本命令**，报告格式建议固定为「全阶段拦截 / 分阶段码 / S3 单列 / blocked_new_highs / 结构化下一步比例」五项，避免再出现 13 与 18 这种口径混用。
- **后续：可以把 `blocked_new_highs` 的涨幅分布做成一等输出**（例如按 ≥2.5 分、<2.5 分两档计数），直接支撑 P29 的 σ̂ 定价。
- **后续：`--jobs` 的默认值可以按机器核数取 min(6, cpu)**；6 并发时 ops-bench 约 69 s。

## 11. 待决

1. **G1/G2 的基线口径要不要改。** 00-PLAN 把「13/231（5.6%）」当成回放总拦截率，但它是**只数熔断**的数；全阶段在 4.2.0 规则下是 132/233（56.6%），4.2.1 默认是 50/233（21.5%）。建议在 00-PLAN 里把 G1/G2 的基线改成「全阶段 21.5%（4.2.1）/ 56.6%（4.2.0）」，并把「S3 单列 13/233」保留为对照。这需要主会话改 00-PLAN，我没有动它。
2. **mat-chem 的榜型**（best / latest）需要 P22 落地规则快照后才能定；在此之前那场的 G4/G5 只能给两个口径各一行。
3. **`REFERENCE_SUITE_REQUIRED` 这类「证据格式漂移」是否要算进门禁指标**。建议在 4.3 的基线表里单列一栏「证据格式拦截」，并在 P24 里讨论是否给旧回执一个兼容证据等级。
4. **`--since/--until` 的时间窗语义**（榜面用窗口前的发预热、收官差额只算窗口内的题）是否需要另一种「纯截断」模式，等有第二个使用方再定。
