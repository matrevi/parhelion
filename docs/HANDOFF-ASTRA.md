# HANDOFF → Astra：Parhelion × mat-chem 比赛提交链路

> 交接人：上一轮 agent（DeepSeek Harness）。用户已明确：**先停止改动**，由 Astra 接手。
> 本文件是唯一交接依据；下文所有路径、提交号、状态均为 2026-11-24 实测。

---

## 0. 请先读原文（原始对话记录）

用户在 DSH 桌面端与本 agent 的完整对话保存在：

```
~/.dsh/sessions/--Users-winbeau-Competitions-mat-chem-sim-pred--/<session-id>/session.v4.jsonl.zstd
```

- 最新一条（就是这次对话，session id `87dd6bd0-f406-4e4a-8268-228f78ccaf73`）：
  `~/.dsh/sessions/--Users-winbeau-Competitions-mat-chem-sim-pred--/87dd6bd0-f406-4e4a-8268-228f78ccaf73/session.v4.jsonl.zstd`
- 读法（zstd 帧、每行一个 JSON 事件）：

```bash
SESSION=~/.dsh/sessions/--Users-winbeau-Competitions-mat-chem-sim-pred--/87dd6bd0-f406-4e4a-8268-228f78ccaf73/session.v4.jsonl.zstd
zstd -dc "$SESSION" | jq -c 'select(.type|test("message|user|assistant")) | {type, text: (.text // .content // "")}' | head -200
# 或全量导出
zstd -dc "$SESSION" > /tmp/session.jsonl
```

- 另有会话缓存（摘要视图）：`~/.dsh/storages/session_projcache/sessions/87dd6bd0-*.json`
- 比赛工作区（所有产物、日志、队列、历史）：`/Users/winbeau/Competitions/mat-chem-sim-pred`
- Parhelion 仓库：`/Users/winbeau/Developer/personal/parhelion`

**注意**：Astra 只需要读原文 + 本文件，不必重读整个代码库；下文的文件清单就是入口。

---

## 1. 用户这段对话到底要什么（按时间顺序）

1. 修 DSH 新开对话不能创建 workspace / 进目录 / 填提示词的问题；后来扩展到「14 题并行做内核」。
2. 给比赛写「主控提示词」和「单题 worker 提示词」，并让新开会话自动带上。
3. 每次改动都要 **commit → push → 发版 → 重装 app → 重启**（历史版本：3.2.1 … 3.2.8）。
4. 放开提交门禁：**每题前 3 发任意提交；3 发后仍没 AC 的题目继续保持任意提交**（只要编译过）。
5. 修「多题在队列里互相抢提交」的并发问题；**只认官方冷却**（官方没冷却就交，有冷却就等）。
6. 历史/突破/额度/排名数据要真实，不能串上一届比赛；**初始化时要一并清理**。
7. 日志要详细记录，因为「未来没有端到端测试，只能对着日志开发」。
8. 最新一条：让我停止改动，整理交接（本文件）。

用户态度：可以浪费几发提交；**不能接受**可见历史被删、数据自相矛盾、被含糊解释。

---

## 2. 已完成的改动（提交均在 origin/main，按时间倒序）

```
c03bf17 fix(submit): classify judge compliance 400s and attach the child trace to failures
f75cb03 chore(submit): trace every submit-validated step to /tmp/opsb-submit-trace.log
ec02f2f chore(release): bump Parhelion to v3.2.8
3aa97c0 fix(quota): report the judge's daily usage, not local receipts
6279ec7 feat(reconcile): backfill judge-scored rows into the submission history
ba6a007 fix(submit): diagnose an empty submit receipt instead of 'Expecting value'
2360151 feat(submit): reconcile the board against the judge's own testcase data
b5dc5e1 test(workspace): assert the archived board lives under scoring-evidence/
4882d65 fix(workspace): a re-inited folder starts with a clean contest board
819e9ad feat(submit): operator freeze switch for queue/board reconciliation
c23eeb9 fix(submit): gate the spawn path, not only the worker
6c21f38 fix(submit): one dispatch slot per workspace
772e1de fix(submit): let the judge alone decide submission pacing
099438e fix(submit): keep cooldown out of the submission history
5fd935b fix(submit): stop inventing a 95s cooldown after each send
02b0c0b feat(brief): stage-0 bench-bucket author for every problem
d3f0ef5 fix(submit): stop cooling-queue growth and accept the template verifier
c6a87fc fix(submit): one judge-wide cooldown instead of per-problem racing
a35ce31 feat(gate): 3 bootstrap probes, then keep probing until the problem is accepted
b6e362b fix(submit): honor judge cooldown without burning the 90s timeout
2f7783f feat(observability): durable feedback log for gate, submit and runtime events
```

更早的（本次对话前半段）：模板布局运行时 `runtime/npu-test.py REMOTE_TEMPLATE`、`load_problem_manifest`、3.2.3–3.2.7 各版本、DSH 20 并发上限、Claude 派发策略。详见 `git log --oneline -60`。

### 关键实现点

| 主题 | 文件 | 要点 |
|---|---|---|
| 提交单发闸 | `cli/opsb_core/services/submit_runner.py` | `results/.submit-dispatch.lock`，spawn 与 worker 两级；拿不到锁返回 2（静默、条目保持 pending） |
| 冷却 | 同上 + `runtime/submit-validated.py` | 只认判题器 429 的 `retry_after`；429 立即 `print({"rate_limited":…})` + `exit 75`，不再 sleep |
| 额度 | `cli/opsb_core/services/judge_quota.py` | `results/.judge-quota.json`，由判题器历史/429 写入；`compute_account_slots` 取 `max(本地, 判题器)`，仅同北京日期有效 |
| 历史/对账 | `cli/opsb_core/services/submit_reconcile.py` | 拉判题器历史 → 按 `result[].time/best_time` 反算分数（2 分/用例）→ 回填 outcome → 按 `submission_id` 去重 |
| 门禁 | `scheduler/gate.py` | `probe_submissions_per_problem=3`、`probe_while_unsolved=True`；未 AC 题目无限放行，只要求 `exit_code∈{0,2}` 或日志有 `Built target` |
| 初始化清理 | `cli/opsb_core/services/contest_state_service.py` + `commands/admin_cmd.py` | contest_id 变化时归档旧 score/board/queue/state 到 `results/_archive-*`、`state/_archive-*` |
| 反馈日志 | `scheduler/feedback_log.py` | `results/feedback-log.jsonl`：gate/submit/submit_error/rate_limited/reconcile |
| 冻结开关 | `submit_runner.submissions_frozen()` | `results/.submit-freeze` 存在即全停 |
| 模板运行时 | `runtime/npu-test.py` | `REMOTE_TEMPLATE`：clone → cmake → sample.py generate → 设备锁内 `./sample` + `profiler` + `sample.py verify`；旧 `REMOTE_3PHASE` 不动 |
| 提示词 | `cli/opsb_core/drivers/brief.py` | `compose_bench_author_brief`（阶段 0 bench 桶作者）、模板布局专用单题/全题 brief、派发策略（原生优先/Claude 需管理员放行） |

### 版本与进程现状

- 最新发布：**v3.2.8**（build 18），DMG + sha256 已在 GitHub release；`/Applications/Parhelion.app` 已装 3.2.8。
- Daemon：`~/.local/bin/opsb daemon start --workspace /Users/winbeau/Competitions/mat-chem-sim-pred --host npu --port 18910`，当前 PID 会变，用 `opsb daemon status` 查。
- **注意**：daemon 跑的是仓库代码（不是 app 包内代码），所以仓库改动对 daemon 立即生效；app 包需重新发版才一致。

---

## 3. 现在的真实数据（工作区）

| 数据 | 值 | 来源 |
|---|---|---|
| 待提交队列 | 14 行（每题一个 HEAD sha） | `results/submit-queue.jsonl` |
| 历史 | 12 条 judge-backed（Pass×5、WA×4、RE×2）+ 最近新增的失败行 | `results/submit-queue-outcome.jsonl` |
| 判题器当日额度 | 15/50（剩余 35） | `results/.judge-quota.json` |
| 冻结 | 已解除 | `results/.submit-freeze` 不存在 |
| 真实成绩（反算） | BucketPackMask 12.19、PatchEmbedOverlap 13.90、BodySliceExtract 10.14/10.19、CGTensorProduct 9.45/9.46、SphericalHarmonicsYlm 9.38、QuantScaleCalib 5.36、FFT 1.38 | `results/scoring-evidence/board-reconciled.json` |

**7 题判题历史为空**（MeshEdge/RadialBasis/RevINNorm/PatchEmbed 之前出现过空记录、SlidingWindow/Sgemm/EdgeIndex），本地任何分数只能是估算——不要当成成绩展示。

---

## 4. 仍未解决的三个 bug（Astra 的主要任务）

### BUG-1：`submit-validated.py exited 0 without a JSON receipt (stdout='')`
- 现象：SgemmFp16Cube、MeshEdgeMessagePassing 提交即「本地拦截」，detail 为空回执。
- 已知：`/tmp/opsb-submit-trace.log` 里**没有这两个进程的任何 trace 行**（连 `start` 都没有），说明子进程可能根本没执行到脚本。
- 已加诊断：`submit_runner` 会把 trace 末尾 6 行附进失败 detail；trace 每步记录 `start/problem/session_loaded/prepared/posting/posted/exit`。
- 下一步建议：在启动前把 `runner`、`py_exe`、`os.path.exists(runner)`、`os.access(runner, X_OK)` 写进 trace 第一行；再复现一次即可定位是「路径不对」还是「exec 失败」。

### BUG-2：判题器 `HTTP 400 提交代码中存在不合规内容，请检查并删除后提交。如需Debug请在本地进行。`
- 证据：RadialBasisBatch 提交时判题器原文返回（`runtime/judge/ict_cli.py:65`）。
- 已知矛盾：同样的源码稍后又能被接受 → 怀疑是**同一份代码重复提交/短时间重复**触发的合规检查，而非内容违规。
- 下一步建议：本地先做「同 sha 已提交过则跳过」，并在 400 后对该题打 `blocked_compliance`、停止重试，避免继续消耗额度。

### BUG-3：历史里仍会出现 `rejected` 行
- 当前 `submit_runner` 把大部分异常写成 `rejected`；Astra 接手时应保证：
  - `rejected` 只用于「我们自己的基建失败」，且在详情里附 trace；
  - 判题器 400 → `blocked_compliance`；429 → 不写历史（保持 pending）；
  - 历史**只追加**，不要按状态批量删。

---

## 5. 硬性约束（用户明确要求，违反会被视为倒退）

1. **历史只追加，永不删除**；清理只能动 `submit-queue.jsonl` 的待提交行，且先备份、打印 before/after。
2. **不消耗额度的原则**：429 不写历史、不重试；400 不重试；只有真正送达到判题器的才进历史。
3. **一次只发一条**（`results/.submit-dispatch.lock`），不要恢复并发提交。
4. **冷却只认判题器**：`retry_after_seconds` 是唯一节奏来源；Parhelion 不再自设 190s/95s。
5. **不要动** `main.asc` / `sample.py` / `reference.py` / `CMakeLists.txt` / `run.sh`（官方模板只读）。
6. **不要跑 `opsb submit` 之外的提交路径**；提交只走 `run_worker_submit_one`。
7. 任何改动后：`pytest cli/tests -q` 与 `pytest scheduler/tests -q` 必须全绿再重启 daemon。
8. AOCI 治理欠账：本轮改了 10+ 受管文件，尚未 `aoci_maintain` + `aoci_update_entry`；Astra 完成功能修复后需补（见仓库 `AGENTS.md`）。

---

## 6. 复现与验证命令

```bash
# 状态
~/.local/bin/opsb daemon status
wc -l ~/Competitions/mat-chem-sim-pred/results/submit-queue.jsonl \
      ~/Competitions/mat-chem-sim-pred/results/submit-queue-outcome.jsonl
cat ~/Competitions/mat-chem-sim-pred/results/.judge-quota.json

# 对账（拉判题器历史、回填分数、压缩队列）
cd /Users/winbeau/Developer/personal/parhelion
PYTHONPATH=cli ~/.parhelion/venv/bin/python -m opsb_core.services.submit_reconcile \
  --workspace ~/Competitions/mat-chem-sim-pred

# 提交 trace
tail -50 /tmp/opsb-submit-trace.log

# 反馈日志
tail -50 ~/Competitions/mat-chem-sim-pred/results/feedback-log.jsonl

# 测试
cd /Users/winbeau/Developer/personal/parhelion
PYTHONPATH=cli ~/.parhelion/venv/bin/python -m pytest cli/tests -q
PYTHONPATH=.   ~/.parhelion/venv/bin/python -m pytest scheduler/tests -q

# 发版（用户要求时才做）
./scripts/build_dmg.sh && gh release create vX.Y.Z dist/Parhelion-vX.Y.Z-arm64.dmg \
  dist/Parhelion-vX.Y.Z-arm64.dmg.sha256 --notes-file /tmp/notes.md
```

---

## 7. 建议的接手顺序

1. 读原文（第 0 节）+ 本文件；跑一遍 `opsb daemon status`、对账命令、`tail` trace，确认现场。
2. 修 BUG-1（启动前校验 runner/py_exe 并写入 trace），复现一次，把真实原因写进 `feedback-log`。
3. 修 BUG-2（同 sha 去重 + 400 → `blocked_compliance`，不重试）。
4. 收尾：历史只保留 judge-backed + 带 trace 的基建失败；额度/冷却/队列三处口径一致。
5. 跑两套测试 → 重启 daemon → 观察一轮真实提交是否入账（判题器历史出现新 `submission_id`）。
6. 补 AOCI maintain/author，再按用户要求发版（当前 3.2.8，勿跳版本）。
