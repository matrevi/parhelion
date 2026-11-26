# P12 第 1 步：临时脚本与 CLI 绕行的用途聚类

本报告是 [P12](../P12-report-views.md) 第 1 节要求的聚类结果，供第 2 节实现 `parhelion_report` 的子代理直接使用。**本报告不含任何实现**。

## 1. 口径与数据范围

- **数据**：`~/.parhelion/logs/sessions/` 下 2026-11-26 冲刺的 13 个 worker 流水（`mesh-dee-07013f` `0f97d5` `17d6d3` `2180de` `345d84` `43695a` `754bf6` `7be6aa` `a07dea` `abcefb` `b0c078` `ba3f0e` `cf7eae`），只读打开。与 [P03-baseline](P03-baseline.md) 用的是同一组文件。
- **窗口**：2026-11-26T04:15:18Z – 05:41:17Z，1.433 小时，不设 `--since/--until`。
- **解析器**：`cli/opsb_core/replay/loader.py` 的 `load_agent`（重建 turn → tool call → result），分类口径复用 `replay/actions.py`。
- **聚类单位**：一次 **bash 工具调用**，与 P03 `actions.inline_python` 的单位完全一致，因此可以和「621 / 388 / 165」这组数字直接比较。672 次调用里共 683 个 Python 脚本体（11 次调用里有两个 `python` 调用）。**CLI 绕行按 shell 段统计：把命令按 `&& || ; |` 换行切开、剥掉 env 赋值与 `timeout`/`env` 前缀后，程序名是 `opsb` 的段算一次调用**，`--help` 单独计数。
- **分类方法**：确定性正则级联，首次命中即归类，无 LLM 参与、无人工标注。全部规则与命中数见 §4，复现代码见 §7。
- 日志原文不进上下文；本节所有数字由脚本聚合产生，只打印计数。

## 2. 总计数与对照表

`opsb replay metrics --sessions <13 个文件> --json --no-cost`（3.5 s，只读）：

| 指标 | 文档数字（`00-PLAN.md` §1） | 本次复现 | 差 |
|---|---:|---:|---|
| 临时 Python 脚本 | 621 | **672**（683 个脚本体） | +8.2% |
| 其中读 JSON | 388 | **461** | +18.8% |
| 其中解析 `report.json` | 165 | **179** | +8.5% |
| `opsb queue list` | 53 | **68**（真实执行 64 次，另 4 次 `--help`） | +28% |
| `opsb history` | 约 30 | **45**（真实执行 44 次） | +50% |
| `opsb bench verify-escape` | 14 | **16**（真实执行 9 次，另 7 次 `--help`） | +14% |
| `sed` + `python3` 的 `cd` 返回字节 | 约 1.5MB | 932,797 + 728,414 = **1,661,211 B（1.58 MiB）** | 一致 |

`cd` 开头的 shell 全部 1,848 次 / 4,295,605 B（`sed` 209、`git` 414、`python3` 360、`grep` 291、`ls` 146、`cat` 52、其他 376），与 P03 §2 的表逐格一致。

### 差异原因

1. **脚本的 621 / 388 / 165 来自一次更早的快照，不是口径不同。** 把 672 次调用按 `ts` 排序做累积：621 出现在 **05:13:02**，165 出现在 **05:13:46**，落在同一分钟；同一截断下调用 `json.load`/`json.loads` 的脚本是 385（文档 388）。因此这三个数字对应 **约 05:13:30 的一次快照**，而且文档的「读 JSON」定义是「脚本调用了 `json.load`/`json.loads`」，比 P03 用的「命令里出现子串 `json`」更窄。P03 §2 自己的表已经记录了全量窗口 672/461/179 与截至 05:25 的 662/452/175，说明 §1 的数字不是从完整日志得出的。
2. **CLI 绕行的三个数字不服从同一个截断。** 53 出现在 04:47、30 出现在 04:36、14 出现在 04:53，`history` 甚至早于 `queue list`。尝试过的四种定义（命令含模式、段程序为 `opsb`、出现次数、去重后不同命令）与任意单一子集都复现不出 53/30/14。P03 得到同样结论，**以 P03 口径 68/45/16 为准**；出现次数是 70/45/16。
3. **「约 1.5MB」与其余数字不是同一把尺。** 它量的是 `cd` 组合命令里 `sed`、`python3` 两组的**工具返回字节**（1.58 MiB，与文档在 5% 内一致），而不是这 621 个临时脚本的输出。

### 一个必须修正的口径缺口

`621`/`672` 只统计**工具返回字节**。模型写 heredoc 时，脚本源码本身也是上下文：

| 项 | 字节 | 每次均值 |
|---|---:|---:|
| 脚本源码（输入侧） | 530,220 | 789 |
| 工具返回（输出侧） | 1,217,259 | 1,811 |
| 合计 | **1,747,479（1.67 MiB）** | 2,600 |

脚本源码占 30%。评估 `parhelion_report` 的收益时应该用合计，否则会低估。

## 3. 分组结论

按「视图能不能替代」把 16 个聚簇分三组：

| 组 | 含义 | 次数 | 占比 | 成本（脚本+输出） | 估计可省 |
|---|---|---:|---:|---:|---:|
| **A** | 读平台已经掌握的数据，视图可直接替代 | 441 | **65.6%** | 1,236,931 B | 447,788 B |
| **B** | 直接读/导入平台内部实现，视图替代不了（边界问题，归 P15） | 91 | 13.5% | 101,034 B | 0 |
| **C** | 本地工作，与平台数据无关 | 140 | 20.8% | 409,514 B | 0 |

**A 组占三分之二，是 P12 的真实作用域。** 文档的 621 里只有约 71%（441 / 621）落在 A 组，其余 29% 不是平台数据读取；把验收口径写成「621 个脚本降 80%」会从一开始就不可达，建议改成「A 组 441 个调用降 80%」。

「估计可省」的模型：一次视图调用替代同一 `(worker, 目标 SHA7)` 的一组脚本，视图输出按 P12 的目标预算计（`per_case`/`diff` 4KB，其余 2KB）。实测同一查询平均写了 1.6–2.8 个脚本（见 §4 `查询数` 列），所以「一次视图替代两次调用」不是假设而是观测。

## 4. 各聚簇明细

672 次调用。`脚本B`/`输出B` 是这两类字节，`查询` 是 distinct `(worker, 目标 SHA7)` 数。

| 聚簇 | 组 | 次数 | 占比 | worker | 脚本B | 输出B | 均值B | 查询 | 建议视图 | 估计可省 |
|---|:--:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| `per_case` 逐例时间对比 | A | 185 | 27.5% | 13 | 104,759 | 419,264 | 2,832 | 97 | `per_case` | 126,711 |
| `platform_internal` 直读平台内部 | B | 91 | 13.5% | 13 | 29,764 | 71,270 | 1,110 | 28 | `env`（新）+ P15 | 0 |
| `source_edit` 改自己源码 | C | 85 | 12.6% | 11 | 193,423 | 66,722 | 3,060 | 22 | 无（见 §5.2） | 0 |
| `correctness` 逐例正确性 | A | 62 | 9.2% | 12 | 51,404 | 112,336 | 2,641 | 38 | `per_case`（扩展） | 8,092 |
| `board` 榜单/tbest | A | 47 | 7.0% | 8 | 33,151 | 62,029 | 2,025 | 17 | `board` | 60,364 |
| `queue` 队列 | A | 45 | 6.7% | 10 | 20,024 | 181,218 | 4,472 | 25 | `queue` | 150,042 |
| `history` 判题历史/回执 | A | 33 | 4.9% | 9 | 17,561 | 111,566 | 3,913 | 20 | `history` | 88,167 |
| `calibration` 校准/分桶 profile | A | 27 | 4.0% | 9 | 13,106 | 30,760 | 1,625 | 17 | `per_case`（桶列） | 0 |
| `numeric_repro` 数值复现 | C | 25 | 3.7% | 6 | 30,632 | 13,885 | 1,781 | 10 | 无 | 0 |
| `run_index` 运行/实验索引 | A | 21 | 3.1% | 8 | 8,285 | 31,383 | 1,889 | 15 | `runs`（新，可选） | 8,948 |
| `other` 未归类 | C | 20 | 3.0% | 11 | 7,547 | 79,931 | 4,374 | 12 | ? | 0 |
| `diff` 两个 SHA 的逐例差 | A | 13 | 1.9% | 5 | 8,992 | 15,389 | 1,876 | 9 | `diff` | 0 |
| `platform_action` 平台动作壳 | C | 5 | 0.7% | 3 | 2,001 | 6,503 | 1,701 | 3 | 无 | 0 |
| `score_model` 重算计分公式 | C | 5 | 0.7% | 2 | 5,951 | 2,919 | 1,774 | 4 | 无 | 0 |
| `gate` 门禁校验 | A | 4 | 0.6% | 2 | 2,226 | 5,777 | 2,001 | 2 | `history`（门禁列） | 3,907 |
| `kb` 知识库 | A | 4 | 0.6% | 2 | 1,394 | 6,307 | 1,925 | 3 | `history`（kb 段） | 1,557 |

两个补充交叉统计（不进上面的互斥桶，因为它们横跨多个桶）：

- **两个 SHA 的逐例差异**：含 ≥2 个不同 SHA7 的脚本 43 个（6.4%），其中同时读逐例数据的 30 个（**4.5%**）。严格的 `diff` 桶只认「脚本里显式构造 `runs={a: path_a, b: path_b}`」，因此偏小。
- **`correctness` 的 62 个里有 34 个读平台产出的正确性证据（`report.json` 的 `outputs`/`mismatches`、`cases_correctness.json`），35 个只读仓内的 `reference.py`/`DESIGN.md`。** 前者可被视图替代，后者不能（这是 P02/P17 的地盘）。

### 4.1 规则表（首次命中即归类，全部大小写不敏感）

| 聚簇 | 正则 |
|---|---|
| `queue` | `opsb\s+queue\b` \| `submit-queue(?:-outcome)?\.jsonl?` \| `\bqueue\.json\b` |
| `history` | `opsb\s+history\b` \| `submission-receipt\.json` \| `SCORE_HISTORY` \| `\bsubmission_id\b` \| `/tmp/judge_` \| `\bjudge_problem\b` \| `\bjudge_sub\b` \| `\bscore\b[^\n]{0,30}\bhistory\b` |
| `board` | `leaderboard` \| `leader\s*board` \| `/board\b` \| `\branking\b` \| `\btbest\b` \| `opsb\s+status\b` \| `live-board` \| `board-reconciled` \| `public_times_us` |
| `gate` | `verify-escape` \| `HOLD_[A-Z_]+` \| `REJECTED_[A-Z_]+` \| `bench\s+diagnose` |
| `diff` | `runs\s*=\s*\{` \| `sha_a` \| `sha_b` \| `\bbase\b[^\n]{0,80}\bcand` |
| `correctness` | `cases_correctness` \| `mismatch` \| `\breference\.py\b` \| `strict_fp32` \| `expected[^\n]{0,40}actual` |
| `per_case` | `report\.json` \| `result\.json` \| `cases_perf\.json` \| `case-profile` \| `per[-_]case` \| `outcome\.jsonl` |
| `calibration` | `calibration\.json` \| `divergence\.json` \| `\bbuckets\b` \| `\bvariants\b` \| `perf-buckets` |
| `run_index` | `bench-runs` \| `experiments/\*` \| `feedback\.json` \| `latest-bench\.json` \| `\brun_id\b` \| `started_at` \| `finished_at` |
| `platform_internal` | `\.parhelion/(?:workspaces\|cache\|hardware)` \| `agent_bus\.db` \| `\bimport opsb_core\b` \| `opsb_core\.cli_app` \| `from opsb_core` \| `~/\.local/bin/opsb` \| `\.cache_remote_judge` \| `\.cache_sync_reconcile` \| `cj_accounts` \| `~/\.judge` \| `\.parhelion/config\.json` \| `which\s+opsb` \| `command -v` \| `pip show` \| `sys\.path` \| `PYTHONPATH` \| `venv/bin/python` \| `sys\.executable` \| `importlib\.util` \| `estimator\.py` \| `scoring-evidence` |
| `source_edit` | `\.replace\(` \| `re\.sub\(` \| `sed -i` \| `open\([^)]*\.asc` \| `kernel\.asc` \| `main\.asc` |
| `numeric_repro` | `numpy` \| `np\.float32` \| `import struct` \| `def f32` \| `KS\s*=\s*f\(` |
| `score_model` | `math\.log\(\s*1\.5` \| `def (?:p\|pts\|score\|s)\s*\(` \| `100/15` |
| `platform_action` | `opsb\s+(?:test\|submit\|auth\|daemon\|doctor)\b` \| `opsb\s+bench\s+cases` |
| `kb` | `opsb\s+kb\b` \| `kb-evolution` \| `\bbugs/index\.json\b` |

### 4.2 各聚簇的文件、字段与典型形态

命令行骨架里的数值、SHA、问题名、绝对路径都已替换。`<wt>` 是 worker 工作树，`<run>` 是运行 ID。

**`per_case`（185，13/13 worker）** — 读的文件：`bench-runs/<run>/report.json`（全流水 179 条命令点名它；本桶内 172 条连 `json.load` 一起用）、`result.json`、`validation.log`、`latest-bench.json`。读的字段：`cases[]`、`cases[].id`、`status`、`device_us`、`min_us`、`finished_at`、`exit_code`、`summary`、`sha`。

```
cd <results>/<problem>/<sha>/bench-runs/<run> && python3 - <<'EOF'
import json; d=json.load(open('report.json'))
for c in d['cases']: print(c['id'], c.get('min_us'), c.get('device_us'))
EOF
```

**`platform_internal`（91，13/13 worker）** — 读 `~/.parhelion/{workspaces,cache,hardware}`、`state/test_runs/*.json`、`agent_bus.db`（`sqlite3 -json`）、`.cache_remote_judge_*.json`，并直接 `import opsb_core` / `sys.path.insert(0, '<平台源码根>')` 后调用内部模块。字段：`precision_ratio`、`testcase_status`、`seq`/`agent_id`/`kind`/`body`（总线）、`entries`（hardware）。

```
ls -d ~/.parhelion/venv && ~/.parhelion/venv/bin/python -c \
  "import opsb_core, os; print(os.path.dirname(opsb_core.__file__))"
sqlite3 -json ~/.parhelion/workspaces/<ws>/state/agent_bus.db \
  "select seq,agent_id,direction,kind,body from messages where ... limit 5"
```

**`source_edit`（85，11/13 worker）** — 只用 `kernel.asc` 一个文件，无平台数据。形态是 `p='kernel.asc'; s=open(p).read(); s=s.replace(old,new); open(p,'w').write(s)`，即用 heredoc 做批量文本改写。

**`correctness`（62，12/13 worker）** — `cases_correctness.json`、`report.json` 的 `outputs`/`mismatches`、仓内 `reference.py`。字段：`mismatches`、`params`、`outputs`、`strict_fp32`、`elements`、`shape`。

```
python3 - <<'EOF'
import json; d=json.load(open('report.json'))
for c in d['cases']:
    if c['status']!='PASS': print(c['id'], c.get('mismatches'))
EOF
```

**`board`（47，8/13 worker）** — `results/scoring-evidence/live-board-tbest.json`、`board-reconciled.json`，`opsb status`。字段：`problems[]`、`tbest`、`our_public_times_us`、`our_score`、`board_submission_id`、`cases`。48 个脚本（7.1%）出现 `tbest`。

**`queue`（45，10/13 worker）** — `opsb queue list`、`results/submit-queue-outcome.jsonl`。字段：`items[]`、`sha`、`title`、`status`、`score`、`submission_id`、`queued_at`、`timing_score`、`account_id`。**这是每次调用输出最大的聚簇（均值 4,472 B）**，脚本会把整个队列 JSON 打出来。

**`history`（33，9/13 worker）** — `opsb history`、`submission-receipt.json`、`submission-manifest.json`、`/tmp/judge_*.json`（判题接口原始响应）。字段：`score`、`testcases`、`result`、`history`、`current_score`、`best_score`、`timing_score`。

**`calibration`（27，9/13 worker）** — `profile.json`、`*-divergence.json`、`*-perf-buckets.json`。字段：`variants[]`、`params`、`base`、`buckets`、`seed`、`case_timeout`，即 bench-spec 的分桶与变体定义。

**`numeric_repro`（25，6/13 worker）** — 无文件，`numpy`/`struct` 复现 float32 舍入或 tie 行为，验证自己对正确性的理解。

**`run_index`（21，8/13 worker）** — `bench-runs/` 目录、`experiments/*/feedback.json`、`events.jsonl`、`plan.json`。字段：`run_id`、`level`、`status`、`started_at`、`finished_at`、`summary`。问的是「有哪些运行、什么级别、结论是什么」。

**`diff`（13，5/13 worker）** — 显式两个 SHA：`runs={'base': <path_a>, 'cand': <path_b>}`，读各自的 `report.json`/`result.json`/`cases_correctness.json`。字段：`sha_bad`、`verified`、`device_us`、`min_us`、`distribution`。

**`score_model`（5，2/13 worker）** — 重写计分公式 `p(t,tb)=1/(1+log(t/tb)/log(1.5))`，验证自己的分差推算。

**`platform_action`（5）** — 在 shell 里跑 `opsb test`/`submit`/`auth`/`bench cases`，外面套一层 Python 解析输出。

**`gate`（4）** — `opsb bench diagnose` + 解析 `HOLD_*`/`REJECTED_*`。

**`kb`（4）** — `opsb kb list --pillar bugs --json` 后解析，读 `kb-evolution.jsonl`。

**`other`（20）** — 剩下的：`calibration.json` 形状探查、`~/.parhelion/bugs/index.json`、`results/feedback-log.jsonl`、`bench/profiles/*`、以及少量一次性算术。

### 4.3 CLI 绕行（不是 Python 脚本，单独统计）

按 shell 段判定「程序是 `opsb`」后：**255 个 bash 命令里共 339 次 `opsb` 调用，其中 114 次（34%）是 `--help`**（82 个命令里含至少一次 help）。真正执行的调用 225 次：

| 子命令 | 次数 | 备注 |
|---|---:|---|
| `opsb queue list` | 64 | 最多的绕行 |
| `opsb history` | 44 | 第二多 |
| `opsb bench cases` / `bench status` / `bench suite` / `bench diagnose` | 34 | 用例表与套件元数据 |
| `opsb bench verify-escape` | 9 | **P03 的 16 里有 7 次是 `--help`** |
| `opsb kb *`（show / annotate-bug / list / query / history / rollback / feedback） | 14 | |
| `opsb queue submit-now` / `approve` / `config` / `skip` / `status` / `_worker-submit-one` | 16 | **写操作**，见下 |
| `opsb test` / `submit` / `status` / `auth` / `daemon` / `breakthrough` | 44 | 动作与状态 |

值得注意的四点：

1. **`--help` 占 34%，是最大的一类 CLI 流量。** worker 在 shell 里反复发现 CLI 用法，因为 MCP 侧没有等价的「这个视图返回什么」的自描述。`parhelion_report` 的 schema 描述本身就能吃掉这一块——这是文档没算到的收益。
2. **`opsb queue approve` 4 次、`queue submit-now` 5 次、`queue _worker-submit-one` 3 次、`auth judge-accounts` 1 次。** 这些是特权动作，不是只读报表，本应只走 MCP。属于 P07/P15 的地盘，但 **P12 的 `queue` 视图必须只覆盖「读」**，不能给它动作能力。
3. **只读绕行**（`queue list` + `history` + `status` + `bench cases/status/suite/diagnose`）合计 154 次，占 225 次真实调用的 68%。
4. P03 的 `cli_bypass` 口径把 `opsb bench verify-escape --help` 也算成一次绕行，因此 16 略高于真实执行数 9；`queue list` 的 68 同理（真实 64）。**比较时应注意这一层偏差。**

## 5. 文档未列且占比 ≥5% 的用途

### 5.1 `platform_internal`：13.5%（91 次，13/13 worker）

worker 在定位并直接使用平台内部实现：`import opsb_core`、把平台源码根塞进 `sys.path`、读 `~/.parhelion/workspaces/<ws>/state/test_runs/*.json` 与 `agent_bus.db`、读 `~/.parhelion/cache/.cache_remote_judge_*.json`、读 `cj_accounts.json` 与 `~/.judge/`。

**视图建议 `view=env`**（新，输出 ≤1 KB）：

```
{ "platform_root": <str>, "cli_version": <str>, "venv_python": <str>,
  "daemon": {"running": bool, "pid": int, "started_at": str},
  "workspace": {"root": <str>, "state_dir": <str>, "bench_runs_dir": <str>},
  "cache_dir": <str>, "disk": {"free_gb": float} }
```

命令行骨架：`ls -d ~/.parhelion/venv && ~/.parhelion/venv/bin/python -c "import opsb_core,os;print(...)"`、`sqlite3 -json .../agent_bus.db "select ... from messages"`。

**同时是 P15 的边界问题，不是视图能单独解决的。** 视图只回答「平台在哪、是什么版本、守护进程活着没有」，不该返回任何账号/凭据字段。worker 能 `import opsb_core`、能读 `cj_accounts.json` 与 `~/.judge/` 这件事必须由 P15 收口。**P12 不要为了让视图「够用」而把内部路径或凭据透出去。**

### 5.2 `source_edit`：12.6%（85 次，11/13 worker）

用 Python heredoc 批量改写自己工作树里的 `kernel.asc`。这不是数据读取，**没有视图能替代编辑动作**，而且它是脚本字节里最贵的一类（193 KB 脚本源码，每次均值 2,275 B，是所有聚簇中最高的）。

**建议**：不为它造视图；在 P12 报告与验收口径里把它从「可替代分母」中排除。如果一定要覆盖它的**验证**环节（改完回读确认），`view=diff` 的 `git diff --stat` 部分已经够用。相应地，「621 个脚本 / 降 80%」这个指标应该改成「A 组 441 个调用 / 降 80%」，否则目标从一开始就不可达。

### 5.3 `correctness`：9.2%（62 次，12/13 worker；可替代部分 34 次 = 5.1%）

worker 在定位 WA：逐例 `status`、`mismatches`、`outputs`、`strict_fp32`，再拿 `reference.py` 算期望值比对。P12 文档只列了「逐例对比（时间）」，没列正确性。

**视图建议**：在 `per_case` 里加正确性固定列，而不是新开视图——同一张表才解释得清「这例变快了但算错了」。每例增加 `passed`、`exit_code`、`mismatch_n`、`strict_fp32`，另带 P02 的复现标记。字段来源见 §6。

### 5.4 接近 5%、未单列视图的

- `history` 4.9%：已经在文档的视图列表里（`history`）。
- 两个 SHA 的逐例差异 4.5%（30 次）：文档列了 `diff`，但严格桶只有 1.9%。**`diff` 视图要用上 §6 的 `pairs`/`pair_bootstrap` 字段，否则会重算 P10 已经算好的配对区间。**
- `calibration` 4.0%：不需要新视图，`per_case` 加一列桶名即可。
- `run_index` 3.1%：不到 5%，但实现成本极低（读 `bench-runs/*/report.json` 的 6 个标量）。建议作为 `per_case` 的可选 `limit` 参数或未来的 `runs` 视图，**不在本次必做范围**。

### 5.5 `opsb --help` 发现：占 CLI 调用的 34%（114 / 339）

worker 在 shell 里反复 `opsb <子命令> --help`（`opsb bench verify-escape --help` 7 次、`opsb queue --help` 多次、`opsb --help` 多次），因为 MCP 侧没有等价的自描述。**它不是数据读取，但是可以吸收的流量**：`parhelion_report` 的 schema `description` 与各视图的固定列说明写清楚，就能省掉这部分发现成本。P12 §3 要求「工具描述里写明用 `parhelion_report` 代替临时脚本」，建议同时写明每个视图返回哪些列——这是本场唯一有直接证据支持的「文档即收益」。

## 6. 供 P12 实现直接使用的事实（数据形状）

以下都只读了形状，没有把内容写进本报告。

1. **`report.json` 有两个层次，字段不统一。** 60 份抽样里 27 份带 `level`/`suite_version`/`weights`/`timed`/`selection`/`digest`（套件级运行），33 份没有（单次 bench-run）。视图必须两种都能读。
2. **逐例时间不在 `perf` 里。** `cases[].perf` 是 **bool**（该例是否开启了计时），不是对象。时间字段直接挂在 case 上：
   - `device_us`: **list**（每次重复的采样）
   - `min_us` / `median_us` / `cold_us`: float **或 null**
   - `samples`: int，`timed`: bool，`cold_samples_us`: list，`timing_source`: str
   视图 MUST NOT 假设 `perf` 是 dict——那是最容易踩的一脚。
3. **P10 的配对 A/B 已经在 `report.json` 里。** 存在时：
   - `cases[].pairs` = `{"event": [[cand_us, base_us], ...]}`
   - `cases[].pair` = `{ratio, lo, hi, n, method, source, offset_us}`
   - `cases[].pair_bootstrap` = `{lo, hi}`
   - `cases[].base_device_us`/`base_event_us`/`base_status`，以及 `digest.paired = {base, source, geomean_ratio, cases: {id: [ratio, lo, hi]}}`
   60 份抽样里 30 份带这些字段。`diff` 视图直接读，不要重算。
4. **`report.json` 的路径不唯一**：`<results>/<problem>/<sha>/bench-runs/<run>/report.json` 是主形态，也有 `bench-runs/<sha>/`、`bench-runs/*/report.json`、`bench-runs/{run}/` 与 `~/.parhelion/workspaces/<ws>/bench-runs/`。实现要按 glob 扫，不要硬编码单根。
5. **`report.settings` 与 `report.environment` 含设备与环境标识字段**（`device`/`soc`/`arch`/`host` 及若干环境版本项）。视图绝不能把这些带出来（`AGENTS.md` §2.2）；过滤按 `cli/opsb_core/device_env.py` 的既有约定做，不要逐字段手写白名单。
6. **`queue` 的数据里有 `account_id`。** `queue` 脚本会顺手读它。视图不返回账号字段。
7. `report.summary` = `{total, passed, counts{STATUS: n}, verified}`；`official` 同构，是判题侧的口径。
8. 名单里 `status` 的取值至少有 `PASS`、`NOT_RUN`（实测两份样本），实现时按字符串透传，不要枚举断言。
9. `outcome.jsonl` 在流水里 20 次，几乎都是 `results/submit-queue-outcome.jsonl`（队列结果），不是逐例结果文件——`history` 视图要读的是它，不是 `per_case`。

## 7. 复现方法

```bash
S=mesh-dee-07013f.jsonl,mesh-dee-0f97d5.jsonl,mesh-dee-17d6d3.jsonl,mesh-dee-2180de.jsonl,\
mesh-dee-345d84.jsonl,mesh-dee-43695a.jsonl,mesh-dee-754bf6.jsonl,mesh-dee-7be6aa.jsonl,\
mesh-dee-a07dea.jsonl,mesh-dee-abcefb.jsonl,mesh-dee-b0c078.jsonl,mesh-dee-ba3f0e.jsonl,\
mesh-dee-cf7eae.jsonl
# 1) P03 口径的总计数（复核 §2 上半张表）
opsb replay metrics --sessions "$S" --json --no-cost
```

```python
# 2) 聚类：约 40 行，无需任何额外数据
import sys; sys.path.insert(0, 'cli')
from opsb_core.replay.loader import expand_sessions, load_agent
from opsb_core.replay import actions
import re
logs = [load_agent(p) for p in expand_sessions(S)]
for g in logs:
    for c in g.tool_calls():
        cmd = actions.shell_command(c)          # 只取 bash 的 command
        if actions.inline_python(cmd) is None:  # 672 次调用
            continue
        bucket = next((n for n, p in RULES if re.search(p, cmd, re.I)), 'other')
        # 脚本字节 = len(cmd)（含 cd 前缀），输出字节 = c.result_bytes
```

`RULES` 就是 §4.1 的表，按顺序匹配。三点注意事项：

- 单位是**命令**不是脚本体，这样 179 才能和 P03 的 `parses_report` 对上（16 条命令在 `cd .../bench-runs/...` 路径里出现 `report.json` 而不在脚本正文里）。
- 分类只看命令文本，**不看结果**；因此 `kernel.asc` 的编辑脚本即便同时 grep 了文件也会落到 `source_edit`。
- 分析脚本放在临时目录，未提交进仓库；§4.1 的规则表加上面这段代码是完整可复现的。

## 8. 局限与未覆盖

- **CLI 绕行的 53/30/14 复现不出**。四种定义、任意单一切断、任意日志子集都试过，最接近的是 P03 的 68/45/16。若主会话需要与文档数字对齐，建议直接改文档。
- **规则是词法匹配，不看语义。** 一个脚本同时做两件事（例如先读 `report.json` 再算 geomean 写回）只会被归入优先级最高的那一类。互斥桶的代价是低估复合脚本，但保证了可复现。
- **`other` 剩 20 个（3.0%）**，逐个看过，没有形成 ≥5% 的用途：`calibration.json` 形状探查 4、`~/.parhelion/bugs/index.json` 2、`results/feedback-log.jsonl` 3、`bench/profiles/*` 5、一次性算术 6。
- **只覆盖 bash 里的内联 Python。** 用 `write` 工具写 `.py` 再执行的形态在 13 个日志里只有 3 次，可忽略；`job_output` 里可能有脚本但看不到命令文本。
- **「估计可省」是模型不是测量**。它假设一次视图调用替代一组同目标脚本、视图输出在预算内。真实节省要靠 P12 实现后对 5 个视图的实测字节数复核。
- **没有量化「轮次」收益。** 每次临时脚本至少消耗一个模型回合，这大概率比字节更贵，但回放数据里没有可靠的「回合单价」，本报告不做换算。
- 本场只有 DSH 驱动；其他驱动的 worker 会不会写出不同形态的脚本，日志里没有证据。
