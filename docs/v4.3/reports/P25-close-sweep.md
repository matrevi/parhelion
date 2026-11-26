状态：已交付

# P25 收官对账与槽位可见性

收官动作从「口头知道」变成一条可复跑的命令：`opsb close-sweep [--enqueue] [--json]` 逐题比对历史最高与收盘在榜，输出**需要 restore**、**需要并入**、**需要刷新 `best/*`** 三张清单；`parhelion_report(view=close)` 给主控看全部、给 worker 只看本题；收官窗口内 restore 候选的优先级乘 `closing.restore_boost`（只改排序）；`QuotaStatus` 与状态卡给出每槽利用率与一行提示；`gate._map_aid` 的 R5 §6 推断核实**属实并已修**。子计划「范围」1–4 完成，第 5 项 Swift 按子计划自带的门槛**列为后续**（见 §12）。

## 1. 分支与提交

**基线是 `main`（`0b777a7`，已含 P21、P22、P23、P24、P26、P27、P28）。** 开工时的集成基分支 `integ/base-P23` 已由主会话并入 `main`，本分支随后两次 `git rebase main`（P24 合入后一次、P27/P28 合入后一次），现在与 `main` 只差本包的提交。`git diff --name-only main...HEAD` 只含本包 16 个文件（`scheduler/close_sweep.py`、`scheduler/cli_bridge.py`、`scheduler/gate.py`、`scheduler/tests/test_close_sweep.py`、`cli/opsb_core/cli_app.py`、`cli/opsb_core/ipc_bridge.py`、`cli/opsb_core/commands/close_cmd.py`、`cli/opsb_core/commands/status_cmd.py`、`cli/opsb_core/mcp/handlers_bench.py`、`cli/opsb_core/mcp/handlers_report.py`、`cli/opsb_core/mcp/report_views.py`、`cli/opsb_core/mcp/schemas.py`、`cli/opsb_core/services/git_fast.py`、`cli/opsb_core/services/queue_service.py`、`cli/tests/test_close_sweep_wiring.py`、本报告），没有 `aoci*.txt`、`.aoci/`、`RUNBOOK.md`、`00-PLAN.md`、`10-DSH.md`、子计划、`reviews/` 或 `AGENTS.md`。

用到的依赖：

- `intent=exploit|probe|restore` 三种提交意图（`scheduler/submit_intent.py`、`scheduler/intent_pricing.py`），restore 的定价：该 SHA 历次 Pass 平均分按 `ScoreEstimator.discount_draw_to_contemporary` 折到当前，σ 取 `judge_noise.sigma_for`；
- `PROBE` / `CRITICAL_RESTORE` 状态码与 `PROBE_INTENT` / `RESTORE_INTENT` 阶段码，以及 `gate.with_next_step` 的「每一发被拦都给下一步」；
- P24 的 S1 体检与逃逸锁出口（`ESCAPE_UNREPRODUCED`、`health` metrics、优先级 ×0.8）：本包只把 restore 的窗口加权接在它的惩罚之后，没有改动它的判定、文案或测试；
- P27 的 `view=sweep` 与 P28 的 KB 证据字段：`close` 与 `sweep` 两个视图并存，帮助文本与枚举各占一项。

```text
$ git log --oneline main..HEAD
<deliver> Deliver P25: close the contest with a reconciliation sweep, a window and a slot line
0dd64d3 Add the close report view and pay for it in the tool table
3f3d16c Show per-slot utilisation and rank restores up in the closing window
9f2f194 Give the closing sweep a bridge subcommand and a CLI command
4320c43 Add the closing reconciliation of a contest
```

第一条是带着本报告的那个提交（提交号由 amend 动作决定，写不进自己的内容，所以留 `<deliver>`）；后四条的实现提交号准确，见下表。

| 提交 | 内容 | 文件 |
|---|---|---|
| `4320c43` | 收官对账主逻辑、三张清单、窗口与槽位纯函数、窗口消息 | `scheduler/close_sweep.py`（新，320 行）、`cli/opsb_core/services/git_fast.py`（+79：`read_refs`/`main_head`/`best_tag_of`/`branches_containing`）、`scheduler/tests/test_close_sweep.py`（新） |
| `9f2f194` | `opsb close-sweep` 入口 | `scheduler/cli_bridge.py`（+40：`SchedulerEngine.close_sweep`、`close-sweep` 子命令）、`cli/opsb_core/commands/close_cmd.py`（新，81 行）、`cli/opsb_core/cli_app.py`（+2） |
| `3f3d16c` | 每槽利用率、`_map_aid` 修复、restore 重投放行 | `scheduler/gate.py`（+41）、`cli/opsb_core/services/queue_service.py`（+38）、`cli/opsb_core/ipc_bridge.py`（+3）、`cli/opsb_core/commands/status_cmd.py`（+2）、`cli/opsb_core/mcp/handlers_bench.py`（+1） |
| `0dd64d3` | close 视图、工具表压缩与测试 | `cli/opsb_core/mcp/report_views.py`（+40）、`handlers_report.py`（+12）、`schemas.py`（净 −169 B）、`cli/tests/test_close_sweep_wiring.py`（新，10 条） |

新模块 `scheduler/close_sweep.py` 320 行、`cli/opsb_core/commands/close_cmd.py` 81 行，都 ≤350。`gate.py` 只加四个接入点（`QuotaStatus` 三个字段、`_map_aid` 的一个分支与两个桶、`_closing_restore_boost`、`priority_score` 末尾的一次乘法），`estimator.py`、`manager.py`、`handlers_bench.py` 没有结构改动（`handlers_bench` 只多一个 status 字段名）。

## 2. 子计划逐项完成情况

### 2.1 新模块 `scheduler/close_sweep.py` — 已完成

逐题比较四样东西，全部来自只读来源：

| 比较项 | 来源 |
|---|---|
| 历史最高 SHA 与分数 | `estimator.get_problem_ledger(title, as_of=收盘时刻)` 的 `historical_best_sha` / `raw_historical_best_score` |
| 在榜 SHA 与分数 | 同一次调用的 `active_board_sha` / `current_score`（以及 `board_status`） |
| 题目仓库 `main` 的 HEAD | `git_fast.main_head`：直接读 `.git/refs/heads/main` 与 `packed-refs`，不起子进程 |
| 含历史最高 SHA 的分支 | 每题一次有界的 `git branch --contains <sha>`（timeout 5 s） |
| `best/*` 标签指向 | `git_fast.best_tag_of` 读 `refs/tags/best/*`（松散 ref + `packed-refs`），取分数最高的那个 |

三张清单见 §3。模块不含任何写操作：不合并分支、不打标签、不动题目仓库。

### 2.2 入口 — 已完成

- `opsb close-sweep [--enqueue] [--json]`（`cli/opsb_core/commands/close_cmd.py`）→ `ipc_bridge.invoke_bridge(ws, "close-sweep")` → `scheduler/cli_bridge.py` 的 `SchedulerEngine.close_sweep`。默认打人类可读的三张表，`--json` 打完整文档。
- `--enqueue`：对「需要 restore」的每一题调用 `SchedulerEngine.submit(title, sha, note, intent="restore")`，即 `opsb submit --intent restore` 走的那一条正常门禁 + 入队路径。**不 force、不调用 `queue approve`、不改自动提交开关**；是否派发仍由管理员的 `auto_consumer_enabled` 决定。
- 入队侧还有一处必须动的既有规则（见 §9 NOTE 2）：`enqueue_once` 的「一发 SHA 一行」原本会挡住 restore（restore 的目标 SHA 按定义已经有官方回执），现在只在 `intent=restore` 且 `submit_intents` 开关打开时放行第二次入队，同一 SHA 仍在途只有一行。
- `parhelion_report(view=close)`：主控看全部题，worker 只能看本题（`_titles` 的既有角色过滤 + `_caller` 的绑定检查，返回体里三张清单也按同一规则过滤，`totals` 只统计可见题）。

### 2.3 收官窗口 — 已完成

- 配置项 `closing.contest_end` / `closing.window_min`（默认 90）/ `closing.restore_boost`（默认 3）/ `closing.restore_min_gap`（默认 0，见 §3.1），读 `~/.parhelion/config.json` 的 `scheduler.closing`。
- `contest_end` 为空时从比赛元数据自动填：先 `results/scoring-evidence/contest-detail.json` 的 `end_time`，没有再退到 `problems-all.json` 各题的 `end_time`（取最早）；都取不到就留空（`contest_end_source: "none"`，窗口永不打开）。只在读取时解析，不回写配置文件。
- 窗口内 restore 候选的优先级乘 `restore_boost`：`gate._evaluate_impl` 在 `priority_score` 定完之后乘一次，**在任何阶段判定之后**，不碰 `approved`、不碰 S1–S4、不碰额度。窗口外因子恒为 1.0（4.2.1 原值）。
- 进入窗口时给主控收件箱发一条 `kind=closing_window`、`priority=strategic` 的战略消息，正文是 close 视图摘要（restore 题数与裸分/加权分、最多 5 题明细、只在分支上的 best SHA、过期标签数、下一步命令）。幂等靠总线 `messages.event_key = "closing:<窗口开始时刻>"` 的唯一索引 + 先查 `has_message`，**同一窗口只发一次**；窗口外不发。

### 2.4 账号槽利用率 — 已完成

- `QuotaStatus` 新增 `per_slot`（`[{slot, used, limit, utilization}]`，只用槽序号）、`day_elapsed_pct`、`slot_hint`。`to_dict()` 让它们随 `quota` 进入门禁回执与桥的 status。
- 状态卡与 `parhelion_status` 的 brief：`compute_workspace_snapshot` 用同一套纯函数（`close_sweep.slot_rows` / `slot_notice`）算出 `per_slot` / `day_elapsed_pct` / `slot_hint` 放进 `scheduler` 段；`opsb status` 打印 `Slot hint: …` 一行；`parhelion_status` 的 `scheduler` 增加 `slot_hint` 键。触发条件是**任一槽利用率 < 60% 且当日已过半（> 50%）**，只显示，不影响门禁与派发。
- `_map_aid` 的核实与修复见 §5。

### 2.5 Swift — 未做（后续）

按子计划第 5 项自带的门槛处理：改动会超过 50 行，且要先给守护进程的热状态加字段（详见 §12）。

## 3. 清单判定规则

三张清单都在 `scheduler/close_sweep.py::sweep` 里，按题意逐题判定，题序即输出序。

### 3.1 需要 restore

**入选**（全部满足）：

1. 该题的榜型是 `latest`（`problems_meta[title]["ranking_submission_mode"] == "latest"`）；
2. 存在历史最高 SHA；
3. 任一：收盘在榜分 ≤ 0；或收盘榜面状态是硬失败（WA/RE/TLE/CE/ML/SE/Fail）；或 `gap = round(历史最高裸分 − 收盘在榜分, 2) > closing.restore_min_gap`（默认 0）。

`critical = 收盘在榜分 ≤ 0 或榜面状态是硬失败`；`weighted_gap = difficulty × gap`，用于排序。

**`best` 赛制下这张单恒为空**（`best` 榜面保留最好分，restore 一个不在榜的 SHA 没有意义），与 P23 §3 的口径一致；`problems` 表里仍给出 gap 本身，只是不入选。

**与子计划字面的一处偏差（重要）。** 子计划写「在榜分低于历史最高**超过 σ̂**」。按字面实现时，本场 σ̂ 逐题是 1.67–6.09（ConcatD 2.56、MultilabelMarginLoss 1.67、LayerNormV3 6.09、PRelu/PadV3 用赛事合并值 3.16），**五个候选差额（1.45–2.56）全部落在 σ̂ 以内**，跑出来是空清单，与子计划自己的验收表（5 题、10.19）矛盾。因此实现取 **G5 的定义**（`00-PLAN §2`：每题历史最高减去收盘在榜分）作为入选规则，`restore_min_gap` 留作可调门槛，并把 σ̂ 与 `beyond_sigma` 逐行报出来：本场合计 `restore_beyond_sigma = 0`。也就是说：清单是「可回收」，真正的放行由门禁定价决定，阈值仍归门禁；`--enqueue` 的实跑正是这样（§6.3：4 题按 EXPLORE 低优先级放行、1 题被 S2 暂缓）。

### 3.2 需要并入

**入选**：历史最高 SHA 存在，且它不属于 `main`：`git branch --contains <sha>` 的结果里没有 `main`，并且 `main` 的 HEAD 不是这个 SHA（后者兜住 git 不可用的情况）。输出 `{title, best_sha, best_score, main_sha, branches, git_error}`。

**只列分支，绝不自动合并任何分支**；本包不调用 `git merge`、不写题目仓库。git 不可用时该行带 `git_error` 而不是静默跳过。

### 3.3 需要刷新 `best/*`

**入选**：历史最高 SHA 存在，且没有一个 `best/*` 标签同时满足「指向该 SHA」与「名字里的分数等于历史最高裸分」（标签名格式 `best/<score>-<sha7>`）。输出 `{title, best_sha, best_score, tag（当前最高分标签）, tag_sha, target（应有标签）}`。本包**不创建也不移动标签**，只给出目标名。

### 3.4 为什么在榜分用「收盘重放」而不是 live 快照

`results/score-snapshot.json` 会被守护进程按**当前** tbest 重新算分，收盘之后再刷新就与收盘时的官方成绩不同（本场：ConcatD 75.40→75.38、PadV3Grad 57.72→57.21、LayerNormV3 71.64→68.74、MultilabelMarginLoss 74.09→71.09、PReluGradUpdate 76.10→75.69）。G5 定义的是「收盘在榜分」，所以 sweep 用 `get_problem_ledger(as_of=min(now, contest_end))` 重放回执行，得到的就是官方逐发成绩（与 P21 回放的 `closing_gap = 10.19` 完全一致）。live 快照不丢，作为参考列 `live_score` 逐行给出——门禁自己的判定仍读 live 榜面，两者分工不同（§6.3 的 LayerNormV3 就是两者分叉的例子）。

## 4. 窗口与加权配置

| 配置项 | 位置 | 默认 | 含义 |
|---|---|---|---|
| `closing.contest_end` | `~/.parhelion/config.json` → `scheduler.closing` | 空（自动填） | 比赛结束时刻（ISO 8601）；空则从 `contest-detail.json` → `problems-all.json` 的 `end_time` 取，取不到留空 |
| `closing.window_min` | 同上 | 90 | 窗口长度（分钟）：`[contest_end − window_min, contest_end]` |
| `closing.restore_boost` | 同上 | 3 | 窗口内 restore 候选优先级的乘数 |
| `closing.restore_min_gap` | 同上 | 0 | §3.1 的入选门槛（分）；设成 σ̂ 之类就回到子计划字面口径 |

- **排序公式**：窗口内 `priority_score = 原 priority × restore_boost`（restore 的原值来自 P23 定价：权重 × 定价增益）。窗口外不乘。只影响队列顺序（`priority_score` 是 WSPT 派发与卡片排序的键），**不改任何门槛、不把额度写进任何判定**。
- **窗口消息**：`kind=closing_window`、`priority=strategic`、`event_key=closing:<窗口开始 ISO>`；`window_notice` 先 `has_message`，再 `append_message`（数据库唯一索引兜底），同一窗口只发一次。触发点是 `opsb close-sweep`（带不带 `--enqueue` 都算一次对账运行）；窗口外返回 `{"sent": false, "reason": "outside the closing window"}`。
- 本场实跑：`contest_end` 自动填成 `2027-03-04T15:59:00+00:00`（`contest-detail.json` 的 `end_time`，即决赛阶段结束），窗口此刻**未打开**，`window_notice.sent = false` —— 因此没有任何总线写入。

## 5. `_map_aid` 的核实结论

**R5 §6 的推断属实。** 修复前的 `gate.get_quota_status._map_aid`：

```python
aid = str(raw_aid or "")
if aid in slot_id_set:            # 注册且可用的槽
    return aid
if raw_uid and raw_uid in uid_to_aid:
    return uid_to_aid[raw_uid]    # 按 user_id 认
return "primary" if "primary" in slot_id_set else slot_ids[0]   # 其余全部归默认槽
```

`list_eligible_account_slots()` 只返回「已登录且启用」的槽，所以**一个存在但没被列进来的 `account_id`（冲刺中途新加的槽、暂时登出或被停用的槽）会被记到默认槽头上**：默认槽看起来打满，新槽看起来一发未用——正是 R5 描述的现象。

**修复**：`_map_aid` 分三种情形——能认出的 id（或能按 `user_id` 认出的）仍归该槽；**非空但认不出的 id 进一个独立桶**（`"\0unmapped"`），不再算到任何已注册槽上；**空 id**（旧回执没有账号字段）仍归默认槽，保持历史口径不变。总量守恒：未识别桶的消耗计入 `used_today`（`remaining_today = 总上限 − used_today`，未识别桶不虚增任何槽的剩余额度）。测试：`test_an_unregistered_account_id_is_not_credited_to_the_default_slot` 与 `test_a_receipt_without_an_account_id_keeps_its_default_slot`。

**隐私**：`per_slot` 只有 `{slot, used, limit, utilization}`，桶键只在函数内部；提示文案写 `slot N`。报告、显示、JSON 里都没有账号名或账号 ID。

## 6. 真实数据验证（ops-bench 副本）

副本 `/tmp/p25-ops`（`cp -a` 自 `~/Competitions/ops-bench`，14449 个文件），`PARHELION_HOME=/tmp/p25home`、`OPSB_DEVICE_WORK_PREFIX=ict-cann-nov`，**全部在副本上跑**。

### 6.1 命令与退出码

```text
$ cli/opsb close-sweep -w /tmp/p25-ops --json        # 不带 --enqueue
exit=0
$ cli/opsb close-sweep -w /tmp/p25-ops               # 人类可读三张表
exit=0
$ cli/opsb close-sweep -w /tmp/p25-ops --enqueue --json   # 仅验证入队路径，见 §6.3
exit=0
```

三条都从**真实入口**跑（工作树里的 `cli/opsb`，在 `/tmp` 下、去掉 `PYTHONPATH`）。

### 6.2 验收核对表

`--json` 的 `totals`：`{"restore": 5, "restore_gap": 10.19, "restore_weighted_gap": 23.29, "restore_beyond_sigma": 0, "merge": 2, "tag": 5}`。

| 题 | 历史最高 | 收盘在榜 | 差额 | 加权 | σ̂（范围） | 差额 > σ̂ | live 快照 | 判定 |
|---|---:|---:|---:|---:|---|---:|---:|---|
| ConcatD | 77.96 | 75.40 | 2.56 | 7.68 | 2.56（本题） | 否 | 75.38 | restore |
| PadV3Grad | 59.98 | 57.72 | 2.26 | 6.78 | 3.16（赛事） | 否 | 57.21 | restore |
| LayerNormV3 | 73.65 | 71.64 | 2.01 | 4.02 | 6.09（本题） | 否 | 68.74 | restore |
| MultilabelMarginLoss | 75.54 | 74.09 | 1.45 | 2.90 | 1.67（本题） | 否 | 71.09 | restore |
| PReluGradUpdate | 78.01 | 76.10 | 1.91 | 1.91 | 3.16（赛事） | 否 | 75.69 | restore |
| **合计** | | | **10.19** | **23.29** | | | | **5 题** |

与子计划「为什么」表的五题与 10.19 **逐项一致**（加权 23.29 ≈ 调研的 23.3）。

| 题 | 历史最高 SHA | 分数 | `main` HEAD | 含该 SHA 的分支 |
|---|---|---:|---|---|
| ConfusionTransposeD | `235f754` | 80.07 | `5ead806` | `b-track1` |
| CTCLossV2 | `dcc7754` | 75.11 | `a1df9d5` | `b-track2` |

与子计划验收完全一致：`235f754` 只在 `b-track1`，`dcc7754` 只在 `b-track2`。两题都**只列分支**，没有合并。

`best/*` 过期 5 题（子计划没有给数量，这里是逐题核对的结果）：

| 题 | 现有标签 | 应指向 |
|---|---|---|
| CTCLossV2 | `best/69.25-0c769ba` | `best/75.11-dcc7754` |
| ConfusionTransposeD | `best/78.18-8e7ba6b` | `best/80.07-235f754` |
| MultilabelMarginLoss | `best/61.93-9f92b0a` | `best/75.54-13c554d` |
| PadV3Grad | `best/51.54-37fd777` | `best/59.98-b6600e1` |
| StridedSliceGrad | `best/71.57-ba4a357` | `best/72.45-a031e4e` |

其余 5 题（ConcatD `best/77.96-474dc59`、LayerNormV3 `best/73.65-9773dff`、PReluGradUpdate `best/78.01-11bd996`、QuantUpdateScatter `best/86.61-0aa17ab`、SyncBatchNormGatherStats `best/72.95-9c943d3`）标签已对，不入选。ops-bench 是 **latest** 赛制（`problems-all.json` 十题全是 `latest`，sweep 的 `board_mode` 也是 `{"latest": 10}`），所以「需要 restore」这张单在这里有内容。

### 6.3 `--enqueue` 的实跑（同一副本，临时 `PARHELION_HOME`）

5 题逐题交给门禁，结果全部带 `RESTORE_INTENT`：

| 题 | 门禁结论 | 优先级 | 入队 | 理由 |
|---|---|---:|---|---|
| ConcatD | `EXPLORE` | 1.62 | 是 | 定价增益 +1.34 低于 +2.56 底线、P(gain>0)=0.70；再乘 P24 的 health 惩罚 0.8 |
| PadV3Grad | `EXPLORE` | 2.66 | 是 | +2.24 低于 +3.16、P=0.76；再乘 0.8 |
| MultilabelMarginLoss | `EXPLORE` | 1.02 | 是 | +1.36 低于 +2.50、P=0.79；再乘 0.8 |
| PReluGradUpdate | `EXPLORE` | 0.47 | 是 | +0.93 低于 +3.16、P=0.62（无 health 惩罚） |
| LayerNormV3 | `HOLD_LOW_GAIN` | 3.26 | 否 | 定价 68.72 − live 榜面 68.74 = −0.02，P=0.50 |

- 新入队 4 行，`intent: "restore"`，**没有 `force` 键**（`force` 只在门禁判 `critical` 的 4.2.1 路径上由门禁自己写）；再次运行同一命令不会重复入队（同一 SHA 在途只一行）。
- `auto_consumer_enabled` 仍由管理员开关决定，本次没有任何配置改动。
- 这一列同时暴露一个真实分叉：sweep 按「收盘在榜分」算差额（LayerNormV3 2.01），门禁按 P23 的**逐发折算**定价对 live 榜面（−0.02），于是门禁暂缓了 sweep 推荐的那一题。两者口径不同不是 bug（P23 报告 NOTE 5 已说明），但值得决定（§11 待决 2）。

### 6.4 只读核对

- `--json` 运行前后对副本做逐文件 `(size, mtime_ns)` 比对：**14449 文件，0 增 0 删 0 改**。
- `--enqueue` 运行后：副本内只多了 `results/submit-queue.jsonl` 的 4 行与 `results/feedback-log.jsonl` / `results/.feedback-signatures.json*`（后者来自门禁既有的估分路径），其余 0 改；`scheduler_state.json` 落在临时 `PARHELION_HOME` 的 `workspaces/p25-ops3-*/state/` 里，没有碰真实 `~/.parhelion`。
- 没有向判题站提交任何东西，没有派发或停止 worker，没有触碰守护进程。

## 7. 测试

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
990 passed, 19 skipped, 2 subtests passed in 102.01s (0:01:42)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
651 passed in 73.01s (0:01:13)
```

两条都跑到底、全绿，都是在 rebase 到 `main`（`0b777a7`，含 P21/P22/P23/P24/P26/P27/P28）之后跑的，因此 P23、P24、P27、P28 的既有用例同批通过。主会话在 `0b777a7` 上给的参考值是 962 passed / 19 skipped 与 641 passed，与本机同量级；本包新增 28 条（`scheduler/tests/test_close_sweep.py` 18 + `cli/tests/test_close_sweep_wiring.py` 10），分别变成 990 与 651。`runtime/tests/test_device_broker.py` 的两条预算测试本轮全绿，没有触发偶发失败。

新增测试（`scheduler/tests/test_close_sweep.py` 18 条、`cli/tests/test_close_sweep_wiring.py` 10 条）覆盖子计划「测试」节：

- 三张清单的判定：latest 下历史最高在 main 上时的 restore、best 赛制下空单、榜面为 0/硬失败时 `critical`、历史最高 SHA 只在分支上（merge 列分支）、历史最高在 main 上（不列）、标签过期与标签已对；**收盘重放**（收盘后的新回执不改 G5 差额）；
- `--enqueue` 只经门禁（断言每题的 `title/sha/intent=restore` 逐个进 `submit`、没有 `force`/`approve` 参数）、真实 CLI 实跑只写 `submit-queue.jsonl` 且 `auto_consumer_enabled` 保持 false、重复运行不重复入队、`enqueue_once` 的放行与开关回退两个方向；
- 窗口：开窗/未开窗/已过、`window_min` 边界、`minutes_left`、配置优先于元数据、窗口内优先级乘 3 且四个阶段结果不变、消息按窗口幂等只发一次、正文含 close 摘要；
- `per_slot` 的计算（只用槽序号）与提示的一行（< 60% 且当日 > 50% 才出）、北京日进度、未识别 `account_id` 不记到默认槽、空 id 仍归默认槽；
- close 视图：主控 2 题、worker 只 1 题、越权与未绑定一律 `PermissionError`、`columns`/`help` 文档、以及工具表字节（主控 ≤15000，并压到 ≤14950）。

**没有改动任何既有测试**；`scheduler/tests/test_gate_421.py`、`test_gate_confidence.py`、`test_stability_and_contracts.py`、`cli/tests/test_gate_queue_p07.py`、`test_mcp_server.py` 等全部原样通过。

## 8. 工具表字节

| 角色 | `main`（`0b777a7`） | 本包 | 上限 | 余量 |
|---|---:|---:|---:|---:|
| 主控 | 14951 B | **14782 B** | 15000（未改） | 218 B（也满足压到 ≤14950 的要求） |
| worker | 12323 B | **12171 B** | 13000（未改） | 829 B |
| recon | 9753 B | 9619 B | — | — |
| knowledge | 8397 B | 8263 B | — | — |

没有新增工具、没有新增参数：`close` 只是 `parhelion_report` 的 `view` 枚举值（三处登记：`mcp/schemas.py` 的 enum、`mcp/handlers_report.py` 的 `VIEWS` 与 `HELP`、角色维持 `orchestrator`+`worker`，worker 由视图内部过滤到本题）。`close` 本身约 +15 B（枚举 + 描述里的视图名），其余都是为留在上限内做的压缩，**净 −169 / −152**。逐条（`TOOL_SCHEMAS` 序列化字节，工具级）：

| 工具 | ΔB（实测） | 改法 |
|---|---:|---|
| `parhelion_report` | −44 | 加 `close` 视图（+15：枚举一个值、描述里一个视图名）后压缩：`(metric=size\|compile\|cold\|pmu, orchestrator only)` → `(metric=size\|compile\|cold\|pmu; orchestrator)`；"Board, tbest, receipts and env facts come from these views, never ~/.parhelion, ~/.judge files or platform internals." → "Board, tbest, receipt and env facts come only from these views."（数据通道规则保留，只删掉路径枚举） |
| `parhelion_squad` | −32 | "recipient/to (agent_id, …)" → "to (agent_id, …)"；">1200 chars becomes an attachment" → ">1200 chars attach"；"Worker broadcast: 1/10min" → "Broadcast: 1/10min" |
| `parhelion_wait` | −28 | "Prefer it to looping status/agent_wait/test_wait" → "Prefer it to polling" |
| `parhelion_escape_rebind` | −26 | "Rebind the active escape lock's reviewed trial to another repair SHA while that repair is neither submitted nor queued." → "Rebind the escape lock's reviewed trial to a repair SHA that is neither submitted nor queued." |
| `parhelion_agent_wait` | −12 | "Block (<=45s) until strategic events or results arrive; …tests, seats and receipts." → "Block (<=45s) for strategic events or results; …tests, seats, receipts." |
| `parhelion_status` | −8 | "Pass a reply's rev as if_rev" → "Pass a rev as if_rev" |
| `parhelion_test` | −7 | "Level sets device cost." → "Level sets cost." |
| `parhelion_spill_read` | −7 | "workers read only their own spills" → "workers read only their own" |
| `parhelion_agent_peek` | −5 | "file (worktree-relative path)" → "file (worktree-relative)" |
| **合计** | **−169 / −152** | 主控 14951→14782，worker 12323→12171 |

`TOOL_TABLE_BUDGET` 未改；所有枚举值、`required`、`minimum/maximum` 与既有参数名逐字未动；没有空 description 的参数。

另有一处**既有测试的连带修正**：`cli/tests/test_report_views.py::test_view_help_documents_every_column_without_spilling` 要求 `view=help` 的回答 < 4096 B。P27 的 `sweep` 帮助段之后基线已经很紧，再加 `close` 就超了，所以 `close` 的帮助段压到一行（`total` 4086 B）。测试本身没有改，改的是 `handlers_report.HELP["views"]["close"]` 的长度。

## 9. NOTE

1. **字节账是在 P27/P28 之后重新算的。** `main` 本身已到 14951 / 15000（只剩 49 B），所以本包不是「借余量」，而是「先压后加」：`close` +15 B，压缩 −184 B，净 −169 B，落在 14782。压缩只动工具描述措辞，没有动任何参数、枚举值或 `required`。`cli/tests/test_report_views.py` 的 help 4096 B 上限是硬约束，`close` 的帮助段因此只有一行。
2. **restore 入队必须放行「第二次入队」，否则 `--enqueue` 是死路。** `enqueue_once` 原本对「已经有官方回执的 SHA」一律拒绝再入队（4.2.1 靠 `force` 绕过）。restore 的目标 SHA 按定义都有 Pass 回执，所以不放行就永远入不了队。改动只认 `intent == "restore"` 且 `submit_intents` 开关打开（关掉即回 4.2.1），同一 SHA 在途仍只一行；门禁的 restore 定价、S1–S4 与管理员开关都不受影响。
3. **收盘重放与 live 快照的分叉是结构性的，不是 bug。** 见 §3.4。sweep 用重放（G5 口径），门禁仍读 live 榜面；`live_score` 逐行给出差异。若要把两者统一，需要先决定「收官判定用哪一天的榜面」，写进 §11。
4. **`best/*` 标签名带分数，所以「过期」可以只靠名字判断。** 判定是「指向历史最高 SHA 且名字分数对」；分数保留两位小数（与现有标签一致）。若某天标签格式改掉，`_tag_matches` 会一律判为过期——宁可多报。
5. **窗口消息的触发点是 `opsb close-sweep`，不是守护进程。** 本包不动守护进程，所以窗口进入时不会自己发消息：谁在窗口内跑一次对账（人工或外部定时），谁触发那一条；`event_key` 保证只发一次。若要自动，得把同一调用挂到守护进程的 tick 上（后续）。
6. **`contest_end` 的来源优先级。** `contest-detail.json` 的 `end_time` 是**赛事阶段**的结束（本场填出 2027-03-04），`problems-all.json` 的 `end_time` 是每题截止（2026-12-28）。本包优先用前者；如果希望按每题的截止时间收官，应当把 `closing.contest_end` 显式写进配置。两种情况下本场的窗口都未打开，符合「现在是赛事进行中」的事实。
7. **`_closing_restore_boost` 每次评估都读一次配置与元数据。** 这是为了不缓存过期窗口（配置随时可改），代价是 restore 意图的评估多两次小文件读；门禁本来就要读分数快照与回执日志，量级可忽略。
8. **`slot_notice` 的「当日已过半」用北京日。** 与 `quota.beijing_date`、每日额度重置同一时区；跨时区的部署不需要改这里。
9. **`enqueue_once` 的 `intent` 字段来自队列行。** 守护进程派发一个已在队列里的 restore 时会重新评估，P23 的 `resolve_intent` 认得这条 pending 行的意图，所以门禁与 worker 看到同一个判定（P23 NOTE 1）。
10. **close 视图 ≤3 KB。** 本场实测 1827 B；超过时按 `_fit` 依次丢掉 `closing`/`merge`/`tag` 与表尾行，并置 `truncated`，不会溢出 MCP 输出上限。

## 10. 风险与后续

- **风险：`restore_min_gap = 0` 会把噪声以内的小差额也列进来。** 本场 5 题的全部差额都在 σ̂ 以内（§3.1）。清单本身不派发，`--enqueue` 过门禁时会被定价与底线二次筛选（本场 1/5 被暂缓），但如果操作者不看门禁结论、直接批量确认，仍可能花额度换一次同源重摇。建议下一场把 `closing.restore_min_gap` 设成该场的 σ̂ 中位数再复跑。
- **风险：`--enqueue` 一次最多 5 题，每题一行，额度仍是管理员开关决定。** 若窗口很短（90 分钟）而账号槽只剩几发，可能派不完；本包不给 restore 预留额度（额度不进任何门槛，这是硬约束）。到期没派完的，靠 close 视图复盘。
- **后续（a）**：把 `get_quota_status` 的 `restore_targets`/`needed_closing_restores`（现在用 live 榜面的 `true_restore_gain` 对噪声底线）改用 `close_sweep` 的收盘口径，让显示与 G5 同源——本场它给出 0 个 target，与 close 视图的 5 个并列会让人困惑。
- **后续（b）**：窗口消息挂到守护进程 tick（或 `sync_reconciler`）上，让「进入窗口」不再依赖有人跑命令。
- **后续（c）**：`best/*` 刷新与分支合并都是**人**的动作（或主控的显式决策），本包只出清单；若要做执行器，应当先有「合并前跑一次门禁/回放」的验收口径。
- **后续（d）**：Swift 侧（§12）。
- **未验证**：`closing.window_min=90`、`restore_boost=3`、`restore_min_gap=0` 三个默认值只在副本与单测上试过，没有真实收官窗口的实战数据（本场窗口未打开）。

## 11. 待决

1. **restore 重投放行的策略归谁。** 本包在 `enqueue_once` 里为 `intent=restore` 放行第二次入队（§9 NOTE 2，`submit_intents` 关闭即回 4.2.1）。这是派发行为的改动，主会话可能希望它更严（例如要求 `close-sweep --enqueue` 之外一律拒绝，或要求每题窗口内最多 N 发）。要收紧的话，改动点在 `enqueue_once` 的 `restore_requeue` 一行。
2. **收盘口径与 live 口径要不要统一。** sweep 用收盘重放、门禁用 live 榜面（§3.4、§6.3 的 LayerNormV3）。可选：(a) 保持现状，close 视图只做对账、门禁独立判定；(b) 让 `--enqueue` 用收盘榜面覆盖门禁的 `current_score`（会动 P23 的定价口径）；(c) 让 sweep 也用 live 榜面（会让 G5 与本场 10.19 对不上）。我按 (a) 实现。
3. **`restore_min_gap` 的默认值。** 现在 0（=G5 全集）。如果主会话认可「差额要过 σ̂ 才算可回收」，把默认改成正数即可，但本场清单会变成空，与子计划验收表的 5 题冲突——两者只能选一个，这条请主会话定。
4. **Swift 是否现在补。** 见 §12；如果要现在补，需要先决定守护进程热状态由谁提供 `per_slot`。

## 12. Swift 是否改动

**未改动**（`Sources/` 无 diff，因此**没有跑 `swift build`**）。

原因（子计划第 5 项的门槛）：`per_slot` 要出现在调度条，得先有数据来源。Swift 的 `SchedulerHotState` 读的是守护进程 `state_cache.build_hot_pipeline_and_scheduler` 的 `scheduler_hot`，而那份代码没有 `per_slot`；补它要么让守护进程 1.5 ms 的热路径 import `scheduler.close_sweep`（连带 `scheduler.estimator` 的首次导入，且给守护进程加一层对调度包的依赖），要么在 `state_cache` 里复制一份槽位规则（违反「一条规则一处实现」）。加上两个 `Codable` 结构体（`NpuModels.swift` 与 `QueueModels.swift` 各有一份同名字段）、`decodeIfPresent` 与视图一行，整包改动会超过 50 行。按子计划「改动超过 50 行就只做 CLI/MCP，Swift 列为后续」处理。

CLI/MCP 侧已经把契约准备好：`quota.per_slot`（门禁回执与桥 status）、`scheduler.per_slot` / `day_elapsed_pct` / `slot_hint`（`opsb status` 与 `parhelion_status`）。后续补 Swift 时只需解码这两个字段并渲染 `slot_hint`，不需要再动 Python。
