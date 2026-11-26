# P06 账本工具、代码拼装种子、压缩时机、状态卡逐例差距（4.2 C #3 #4 #5 #7；4.1 #13 #14）

分支：`impl/P06-ledger-seed`。在 P05 合入后的 `main` 上开发。

## #3 模型写入

- MCP 新工具 `parhelion_note(type, claim, why?, scope?, sha?, evidence?, confidence?, revisit_if?, supersedes?, ...)`，worker 与 orchestrator 均可用，返回新 id。
  - 字段按 `docs/v4.2/research/compaction-memory.md` §5.3；
  - 平台字段被拒绝；
  - 模型写入的 `confidence` 最高为 `inferred`，只有平台能写 `measured`；
  - 更新假设状态时提交一条带 `supersedes` 的新行。
- `parhelion_submit_gate` 新增参数 `hypothesis_id`、`predicted_effect`。缺少时沿用 P05 由提交说明自动生成的那条，并在返回里提示「已自动生成 L-xx」。不得因为缺少这两个参数而拒绝提交。

## #4 查询

- `parhelion_ledger(view=open|unexploited|failures|case:<id>|since:<id>, problem?, worker?, limit?)`：每条一行（id、类型、claim、关键数字）。
- `parhelion_recall(query, limit?)`：检索账本、`state/handoffs/*.jsonl`，以及该 worker 已归档的会话流水（只读）。
  - 建一个小倒排索引，缓存在 `<workspace state>/recall/`，绝不写入日志目录；
  - 结果给出片段与来源（文件#行）。

## #5 由代码拼装压缩种子

`ledger/seed.py`：目标 6–10K 字符，上限取 `agents.seed_budget_chars`。按下列顺序拼装，最后一行是行动指令：

1. 固定规则，原文照录：
   - 比赛规则，取该比赛 `AGENTS.md` 中的规则段；
   - 管理员指令，取总线上发给本 worker 的 directive；
   - 平台规则，取现有的 `RULES_LINE` 等常量。
   
   同时作为 `constraint` 条目钉住。
2. 状态卡的扩展版（见下文 #13 #14）。
3. 账本摘要：
   - 全部未验证的假设，按优先级排；
   - 全部未利用的洞见；
   - 最近 15–20 条失败方向，每条一行，带 id；
   - 其余写成「另有 N 条，用 `parhelion_ledger(view=failures)` / `parhelion_recall` 查」；
   - 在研用例的最新逐例测量与噪声带。
4. 模型叙述，不超过 1500 字，用 P05 的固定标题与校验器。
5. 原始尾部：最近几轮原文（上限可配置），以及 `git status`、`git diff --stat` 和在改文件的路径。
6. 行动指令，替换 `CHECKPOINT_TAIL`：「取最高优先级的 open 假设或未利用洞见，说出它的 id；重新推导任何东西之前先调用 parhelion_recall；然后跑测试。若重读文件，说明原因。」

ACP 新会话与 Codex 前置都用这份种子。开关 `agents.seed_scheme=legacy|ledger_core|ledger_full`。默认值由回放结果决定，见下文验收。

## #7 压缩时机

- 在判题回执到达，或阶段边界上压缩。阶段边界指：没有进行中的工具调用、刚有测试终态，或刚有假设被判定。
- 此时上下文要不低于阈值的 40%（可配置）；现有的 `compact_threshold` 保留为硬上限。
- 正在执行工具调用时不压缩。

## #13 #14 状态卡带上逐例数据

`services/state_card.py`：
- 简版 `build(...)` 保持 2000 字节硬上限，用于 brief。
- 扩展版 `build(..., extended=True)` 用于种子与 `parhelion_status(detail=true)`，内容有：
  - 全部用例相对同代基线的逐例加速比与置信度。与门禁用同一套数据：`estimator._case_bench_pair`，加上 P01 的 `receipt_predict` 与用例映射；
  - 逐例差距 `(ours − tbest)/tbest`，tbest 的观测时间；
  - 最近 3 条回执摘要（SHA、状态、官方分、是否刷新最高分）；
  - 工作树绝对路径。
- 主控不再需要手算逐例比例，`parhelion_status` 对主控也给出这份数据的紧凑表。

## 调研补充（`docs/v4.2/research/agent-optimization-practice.md`）

**R4 摘要封顶。** 第 3 段账本摘要只列 16–20 条在研条目，其余写成「另有 N 条，用 recall 查」。模型不得整段改写摘要，只能提交增量条目，由代码合并。上线判据以遗忘探针与成本为准，不以「质量提升」为准：调研显示记忆主要省成本，对最终质量影响不明显。

**R5 失败指纹与尝试预算。** 每条失败与实验记一个稳定指纹：用例集、瓶颈类别、diff 类别、结果。
- 同一假设或同一瓶颈类别最多 3 次尝试，计数跨重启、新会话和改名都不清零。
- 超出后，工具返回里附警告与相关账本 id，要求换瓶颈，或给出 `revisit_if` 理由。只警告，不阻断。

**重测拦截与近似重复过滤（§3 指标「压缩后重测已测 SHA 与级别 = 0」，以及 R7）。**
- `parhelion_test` 收到同 SHA、同级别、同套件版本的请求时，如果已有正式运行，直接返回已有结果与账本 id，不排设备。显式 `force_rerun=true` 并写明理由时才重跑。
- 对候选 diff 做近似重复检测：先比 diff 哈希，再比规范化 diff 的相似度。高于阈值时返回最相近实验的结果与 id，并提示「给出差异理由后再测」。

**R8 目标排序与平台期规则。**
- 扩展状态卡按「权重 × 与 tbest 的差距」给出预期收益最大的前 3 个用例，并写明数值目标，即当前同代差距。
- 连续 5 次实验没有提升时，工具返回提示换瓶颈类别或方法类别。

## 回放与上线

- 在 P03 的回放里注册 `ledger_full`（即本包的种子）。对本场流水运行 `--schemes current,ledger_core,ledger_full,full_history`，得到覆盖率与 bootstrap 置信区间，写入 `docs/plan-4.1-4.2/reports/P06-replay.md`。
- `ledger_full` 的覆盖率显著高于 `current`（置信区间下界 > 0）时，`seed_scheme` 默认设为 `ledger_full`。否则默认保持 `ledger_core`，并在报告里给出差距分析。

## 验收

- 单测：
  - `parhelion_note` 的字段校验与平台字段保护；
  - 门禁请求缺少 `hypothesis_id` 时自动生成；
  - 各 ledger 视图与 recall 的检索结果及来源；
  - 种子的段落顺序、预算截断（先截尾部，再截失败列表，固定规则与 open id 永不截断）、行动指令；
  - 压缩时机的三种边界与「工具调用中不压缩」；
  - 状态卡两种版本的大小上限；
  - 新工具在 schema、handler 与角色三处登记一致。
