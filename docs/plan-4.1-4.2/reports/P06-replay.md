# P06 回放：代码拼装压缩种子（4.2 C #5 #7；4.1 #13 #14）

数据与口径同 P03 基线与 P05 报告：2026-11-26 冲刺的 13 个 worker（`mesh-dee-*.jsonl`，19 个切点，148 个探针）。日志只读，只经代码打印汇总，本报告不含消息原文。

```bash
S=mesh-dee-07013f.jsonl,mesh-dee-0f97d5.jsonl,mesh-dee-17d6d3.jsonl,mesh-dee-2180de.jsonl,mesh-dee-345d84.jsonl,mesh-dee-43695a.jsonl,mesh-dee-754bf6.jsonl,mesh-dee-7be6aa.jsonl,mesh-dee-a07dea.jsonl,mesh-dee-abcefb.jsonl,mesh-dee-b0c078.jsonl,mesh-dee-ba3f0e.jsonl,mesh-dee-cf7eae.jsonl
PYTHONPATH=cli:. ~/.parhelion/venv/bin/python cli/opsb replay bench \
  --sessions "$S" --schemes current,ledger_core,ledger_full,full_history --json      # 约 1.3 秒
```

## 1. 结论

- **`ledger_full` 与 `current` 打平，判据不成立，`seed_scheme` 默认保持 `ledger_core`。** `ledger_full` 覆盖 0.6419（95/148）[0.568, 0.716]，`current` 0.6419（95/148）[0.568, 0.716]。配对差（按 worker 聚类 bootstrap 1000 次，固定种子 20261126）为 0.000，95% CI [−0.032, +0.032]，多覆盖 3 个、少覆盖 3 个：置信区间下界远小于 0。按 P06 文档的判据，只有「显著高于 `current`（下界 > 0）」才默认 `ledger_full`。
- **`ledger_core` 仍然是最好的一个（+1 个探针，不显著）。** 0.6486（96/148），配对 +3 / −2，均值 +0.007，CI [−0.015, +0.032]。与 P05 报告的 +0.007 [−0.015, +0.032] 一致（重算后数值相同、区间略窄，因为这次统一用 `replay/score.compare` 的 worker 聚类重采样）。
- **差距分析：丢的都是 `idea_repeat`，一个也没多。** `ledger_full` 0.6419 = `current`；分类型看 `file_reread` 31/70、`idea_repeat` 48/61、`refuted_retry` 10/11、`retest` 6/6，与 `current` 完全一致；`ledger_core` 多出的那 1 个也是 `idea_repeat`（49/61）。原因在预算：`ledger_full` 把种子**整体**限制在 `agents.seed_budget_chars`（8000），而 `current` 的日志种子中位 6,794、`ledger_core` 中位 7,896、最大 9,713（P05 只约束检查点那一段，不约束整篇）。固定规则（回放里为空）、扩展状态卡（回放里用日志的 `STATE` 段代替）与行动指令都要占用同一份预算，留给「最近几轮原文」的空间就更少，而探针的答案主要落在原始尾部。
- **回放测不到 `ledger_full` 真正新增的三段。** 规则原文（比赛 `AGENTS.md` 规则段、管理员 directive、平台规则）、扩展状态卡的逐例数据与目标排序、工作树的 `git status`／在改文件路径——这四样在 2026-11-26 的日志里没有对应物：那批会话早于 `parhelion_note`，也没有保留扩展卡所需的逐例报告与回执。所以这张表测的是**同一批材料的不同拼装方式**（顺序、封顶、行动指令、原始尾部），不是 `ledger_full` 的全部价值。
- **`full_history` 仍是上界**：1.000 [1.000, 1.000]，多覆盖 53 个探针、少 0，均值 +0.358，CI [0.243, 0.457]。

## 2. 对照表

| 方案 | 覆盖率 | 95% CI | file_reread | idea_repeat | refuted_retry | retest | 种子长度中位 / 最大（字符） |
|---|---:|---|---:|---:|---:|---:|---|
| current | 0.6419（95/148） | [0.568, 0.716] | 31/70 | 48/61 | 10/11 | 6/6 | 6,794 / 7,693 |
| **ledger_core（默认）** | **0.6486（96/148）** | [0.574, 0.723] | 31/70 | 49/61 | 10/11 | 6/6 | 7,896 / 9,713 |
| ledger_full（本包） | 0.6419（95/148） | [0.568, 0.716] | 31/70 | 48/61 | 10/11 | 6/6 | 7,981 / 7,996 |
| full_history | 1.000（148/148） | [1.000, 1.000] | 70/70 | 61/61 | 11/11 | 6/6 | 1,118,495 / 2,014,308 |

配对差（相对 `current`：探针级差值的均值，CI 为按 worker 聚类重采样 1000 次）：

| 方案 | 多覆盖 / 少覆盖 | 均值差 | 95% CI | 落在哪个类型 |
|---|---|---:|---|---|
| ledger_core | +3 / −2 | +0.007 | [−0.015, +0.032] | idea_repeat +3/−2 |
| ledger_full | +3 / −3 | 0.000 | [−0.032, +0.032] | idea_repeat +3/−3 |
| full_history | +53 / −0 | +0.358 | [0.243, 0.457] | file_reread +39、idea_repeat +13、refuted_retry +1 |

CI 来自 13 个 worker，其中多数没有任何变化，所以区间窄不代表有把握；结论按「没有可检出的差异」写。

## 3. `ledger_full` 怎么构成，回放能做到什么

种子（`ledger/seed.py::assemble`）= `Preserve this checkpoint and await the next task.` + `RULES` + `STATE` + `LEDGER` + `HANDOFF` + `RAW TAIL` + 行动指令，整篇 ≤ `agents.seed_budget_chars`。

回放里各段的来源（`replay/ledger_full.py`）：

- `RULES`：空。回放没有工作区，读不到比赛 `AGENTS.md` 与总线 directive；平台 `RULES_LINE` 已含在日志里的 `STATE` 段末尾。
- `STATE`：日志里原种子的 `STATE …` 段原样搬过来（与 `ledger_core` 相同），不是扩展卡。
- `LEDGER`：用 `ledger.auto.apply_*` 在内存里重建（与 P05 同一套代码），再用 `ledger/digest.py` 的封顶规则渲染：在研条目最多 16 条带细节、其余只留 id，失败方向最多 18 行、其余写成「N earlier dead ends」，最后附最新逐例测量与噪声带。
- `HANDOFF`：日志里的旧交接叙述（≤1500 字，P05 之后写的那批本来就合规）。
- `RAW TAIL`：切点前最后 2 轮回复（各截 2000 字符）加最后一次阶段结果，超预算时从**开头**丢，保留结尾。
- 行动指令：替换 `CHECKPOINT_TAIL`（`Take the highest-priority open hypothesis …`）。

预算不足时的截断顺序（回放里实测会触发）：先丢原始尾部 → 再把账本摘要退到只有 id → 再缩叙述；固定规则与 open 假设／未利用洞见的 id 永不丢。

调参记录（都只改 `ledger_full` 的内部预算分配，判据与探针不动）：

| 变体 | 覆盖率 | 说明 |
|---|---:|---|
| 初次实现（只取最后一次阶段结果做尾部） | 0.6216（92/148） | 尾部比 `current` 短，丢 3 个 `idea_repeat` |
| 修好「先测量、再填充」的预算分配（此前多算了一次分隔符，导致回退到「没有尾部」的分支） | 0.6284（93/148） | 种子从中位 6,932 涨到 7,229 |
| 尾部 = 最近 2 轮回复 + 阶段结果 | 0.6419（95/148） | 与 `current` 打平，采用 |
| 尾部只取最近 1 轮 + 阶段结果 | 0.6419（95/148） | 同上；取 2 轮是生产里 `agents.seed_tail_turns` 的默认 |
| 账本摘要上限 1,200 / 2,600 / 4,000 字符 | 0.6419 三种相同 | 摘要本来就没到 1,200 字符，封顶不改变结果 |

## 4. 为什么默认仍是 `ledger_core`

判据（P06 文档「回放与上线」）：`ledger_full` 的覆盖率显著高于 `current`（CI 下界 > 0）才把 `seed_scheme` 默认设为 `ledger_full`；否则保持 `ledger_core` 并给出差距分析。0.6419 对 0.6419、CI [−0.032, +0.032]，不满足，因此 `ledger/seed.py::DEFAULT_SCHEME = ledger_core`，`ledger_full` 只能靠 `agents.seed_scheme=ledger_full` 显式打开。

差距的三个来源，按影响排序：

1. **整篇预算 vs 分段预算。** `ledger_full` 守 8000 字符上限，`current`/`ledger_core` 不守（最大 9,713）。探针的答案主要集中在原始尾部，而 `current` 的尾部天然更长。要让 `ledger_full` 追上，只能把整篇上限提到 10K（文档允许 6–10K，但上限取 `agents.seed_budget_chars`），或者把 `ledger_core` 也收紧到同一上限——后者会让 P05 的方案一起掉到约 0.62，比较才公平，但那是改默认行为，不在本包范围内。
2. **回放材料里没有本包新增的东西。** 规则、扩展状态卡、模型条目、工作树这四段在旧日志里没有对应物，`ledger_full` 相对 `current` 的**新**信息在回放里等于零，只剩拼装方式的差异。
3. **探针类型本身对尾部最敏感。** `file_reread`（70 个探针里 `current` 只覆盖 31）要看文件名，只有原始上下文里有；`idea_repeat` 也需要原话。只有 `full_history` 能全拿到。

## 5. 验证

- 新增 57 个测试：`cli/tests/test_ledger_tools.py` 30 个（`parhelion_note` 字段校验与平台字段保护、状态更新走 `supersedes`、跨题目权限、门禁 `hypothesis_id` 缺省自动生成与不可拒绝、各 ledger 视图、recall 的片段与 `文件#行` 来源与只读（含索引缓存不写日志目录）、重测拦截与 `force_rerun`+理由、近似 diff 与阈值、指纹跨改名稳定、3 次尝试预算、5 次平台期、警告随测试返回、三个工具在 schema/handler/角色三处登记一致），`cli/tests/test_ledger_seed.py` 27 个（种子段落顺序与行动指令、规则原文、预算截断顺序、id 不丢、R4 封顶、`constraint` 幂等钉住、`seed_scheme` 三档与默认值、交接与 Codex 两条路径、压缩时机的三种边界与「工具调用中不压缩」、40% 下限、硬上限、两种状态卡的大小上限与扩展卡内容、`parhelion_status(detail=true)`）。
- 全量：根目录 `582 passed, 18 skipped, 2 subtests passed`；`cli/tests runtime/tests` `377 passed`。
- 没有碰真实比赛目录、守护进程、worker 与判题站；回放只读日志，账本只在内存里重建。
