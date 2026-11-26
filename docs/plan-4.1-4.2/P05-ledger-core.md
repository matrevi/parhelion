# P05 账本内核、平台自动写入、交接校验、修正 checkpoint（4.2 C #1 #2 #6 #8）

分支：`impl/P05-ledger-core`。在 P03、P04 合入后的 `main` 上开发。这一步改动最小，目的是立刻止住遗忘（`docs/v4.2/00-PLAN.md` §7 第 2 步）。

## 证据

- `agents/manager.py` 的 `_checkpoint`（约 :215）用 `stage[:room]` 保留开头、丢掉结尾。`CHECKPOINT_MAX_CHARS=4000`，`CHECKPOINT_TAIL="Continue from the state card. Do not re-read files you already summarized."`。
- `agents/adapters.py`：
  - ACP 的 `compact`（约 :230）让已有约 350K 上下文的模型「Write a short handoff」，再新开会话；
  - Codex 的 `compact`（约 :356）走原生压缩，原生压缩保留最近的用户消息、丢掉助手推理。
- 字段与类型以 `docs/v4.2/research/compaction-memory.md` §5.3 为准，交接校验以 §5.7 为准。

## #1 账本内核

新包 `cli/opsb_core/ledger/`，每个模块 ≤350 行：
- `store.py`：
  - 每个 worker 一个只追加的 JSONL，放在 `<contest>/results/ledger/<problem>/<worker>.jsonl`；修改以新行加 `supersedes` 表示；
  - 追加时持 flock，单行 ≤1KB，超长字段截断并标记；
  - id 为 `L-<seq>`，同一 worker 内唯一，跨压缩不变；跨 worker 引用写成 `<worker>/L-<seq>`；
  - 平台字段由代码填写，模型不能改，包括 `source=platform`、`measured` 置信度和 `diff_hash`。
- `model.py`：§5.3 的七种类型、公共字段与类型专属字段的校验，以及 `claim`/`why` 的长度上限。
- `view.py`：
  - 折叠 `supersedes` 得到当前视图；
  - 查询 `open`（open/queued/testing 的假设）、`unexploited`（没有被实验引用的洞见）、`failures`（新到旧）、`case:<id>`、`since:<id>`；
  - 题目级视图合并该题所有 worker 的账本。
- 去重：平台写入的条目按证据键（`test_run_id`、`submission_id`）幂等，重放不重复。

### 实验条目的预测与结果字段（调研 R5）

`experiment` 条目增加以下字段：
- 模型写：`expected_outcome`；
- 平台写：`actual_outcome`，由测试运行与回执填写；
- 判定：`was_diagnosis_correct`、`was_fix_effective`；
- 经验：`lessons`、`avoid_patterns`（带 scope 与 `revisit_if`）、`try_patterns`。

做法参照 KernelAgent 的 reflexion 结构，见 `docs/v4.2/research/agent-optimization-practice.md` R5。本包只定字段并由平台填写 `actual_outcome`；其余字段的写入口在 P06 的 `parhelion_note`。

## #2 平台自动写入

1. 测试完成时，接入 `services/test_runs.py` 的终态收尾（`refresh` / 守护线程收尾，以及 `manager.deliver_test_results`）。由 digest 写一条 `measurement`（逐例 min/median µs、噪声带、级别、套件版本）和一条 `experiment`（sha、`base_sha`、相对基线的逐例增量与 outcome：better/worse/neutral/noise/WA/RE/TLE/compile_fail；`diff_hash` 由 git 计算）。
2. 提交请求时，在 `parhelion_submit_gate` / 门禁请求入口处，如果该 SHA 还没有关联的假设，就由平台从提交说明与估分器的预测增益自动生成一条 `hypothesis`（`source=platform`、`confidence=inferred`、`predicted_effect` 取门禁的 μ 与 σ）。P06 会再加显式的 `hypothesis_id` 参数。
3. 回执到达时，接入 `manager.deliver_judgements` 或 P01 的规范回执入口。写一条 `measurement`（官方分与逐例时间），并按预测把该 SHA 关联的假设置为：
   - `confirmed`：实际增益落在预测的 μ±σ 内或更好；
   - `refuted`：增益 ≤0，或 WA/RE/TLE；
   - `inconclusive`：其余情况。
   
   判据写成常量并在报告里说明。同一 `submission_id` 只处理一次。

## #8 修正 `_checkpoint`

改为保留阶段结果的**结尾**。预算按「种子预算」重新分配：状态卡在前，阶段结果尾部在后，各自有上限。新增 `agents.seed_budget_chars` 配置，默认 8000，与 P06 的 6–10K 目标一致。在本包里它只影响 checkpoint 的容量。

## #6 交接校验（作用于现有压缩路径）

1. ACP 的交接 prompt 从「Write a short handoff」改为固定标题的叙述，不超过 1500 字：现在在哪、做到一半的事、未提交的改动与路径、下一个实验（写账本 id）、账本里还没有的判断。
2. 校验器 `ledger/handoff.py`，按 §5.7 实现：
   1. 每个 open 假设与 unexploited 洞见的 id 都必须出现在种子里，由代码追加一个「账本摘要」段落来保证；
   2. 叙述中的 SHA（`git cat-file -e`）、路径（工作树内）和测试运行 ID（登记表）都必须真实存在，不存在的标注 `[unverified]`；
   3. 与上一版叙述（存为账本 `decision` 类条目，或状态文件 `state/handoffs/<agent>.jsonl`）对比，上一版有、本版漏掉的决定与失败自动补回；
   4. 标题校验失败时修复一轮，再失败就退回只用账本的种子（`ledger/seed_min.py`：固定规则、状态卡、账本摘要、`git status` 与 `git diff --stat`）。
3. Codex：原生压缩之后，下一回合的 prompt 前置同一份经校验的账本摘要，而不是依赖压缩结果。
4. 交接全文与校验结果追加到 `state/handoffs/<agent>.jsonl`，供 P06 的 recall 与回放使用。

## 回放

在 P03 的 `replay/seeds.py` 注册 `ledger_core` 种子：沿用当前 handoff，加上用切点前事件重建的账本摘要，并应用 #8 的尾部保留。对本场流水运行 `opsb replay bench --schemes current,ledger_core,full_history`，结果写入 `docs/plan-4.1-4.2/reports/P05-replay.md`。

## 验收

- 单测：
  - 账本追加、并发 flock、`supersedes` 折叠、各视图；
  - 平台写入的幂等（重放测试结果和回执）；
  - 假设判定的三种结果；
  - `_checkpoint` 保留结尾；
  - 交接校验的四步，以及回退到只用账本的种子；
  - Codex 前置摘要。
  
  全部使用假 adapter。
- 回放报告：`ledger_core` 相对 `current` 的覆盖率差和置信区间。
