# P26 席位与冲刺 v2

分支：`impl/P26-sprint-seats`。基于 4.2.1 的 `main`。无依赖。P27 在它合入后开工，因为两者都改 `drivers/brief.py`。

## 为什么

### 上一场最有效的分工，4.2.1 表达不出来

11-23 的 Group A 按工序拆分：
- `recon`：只读侦察，产出 blueprint；
- 两个按瓶颈拆开的实现席；
- `int`：负责合并。

Group B 是同一目标的两条赛道（`b-track1`/`b-track2`），互相赛马。20 个席位全程零 kill；worker 之间互投 599 条消息，主控只发了 76 条（R2 §1.1、§3.3）。

11-11 一整天没有提交，专门建知识库，后面 9 天都在复用（R6 §7）。

### 当前 `cli/opsb_core/agents/sprint.py` 的限制

- **模式少。** 只有 `solo` / `dual(algorithm,dataflow)` / `full(+calibration)`。
- **整场同一模式。** `start_sprint` 对全部题目套同一个 mode，不能「3 题 dual、2 题 pipeline」。
- **识别不了实战布局。** `sprint_seats()` 只认 `.worktrees/sprint-<axis>` 这种目录名，`<Title>-expB`、`b-track1` 都认不出。
- **冲刺中不能调整席位**，熔断或停滞之后也没有换席的执行器。
  - `agents/judge_feedback.py` 在六发无增益时，会给主控发一条 stagnation 事件，内容是「总结最近六发并派新 worker 从正交方向接手」。
  - 但接下来要主控自己拼 spawn 调用。
  - 上一场熔断触发 5 次，从没换过人（R2 §2.5）。
- **同题多席共用一个远端检出目录。**
  - `commands/queue_cmd.py` 的 `_remote_problem_dir` 是 `<root>/<Title>`；`runtime/npu-test.py` 在远端对这个目录做 `checkout --detach --force <sha>`。
  - 同题两席同时测不同的 SHA，可能互相覆盖检出与构建目录。上一场为此专门定了一条「同题 opsb test 串行」规则（R2 §1.3）。
  - 现有的 dual 模式已经有这个风险。
  - 同一函数在清单没有 `npu_remote_root` 时，会回落到一个写死的远端根目录。这是设备侧的值，应该移进 device 配置。

## 范围

1. **席位身份**
   - 席位按「题 + 席位名」识别。席位名写进总线 agent 记录：在 `agents/bus_store.py` 增加一列，沿用现有的 ALTER 迁移方式。
   - 旧的 `.worktrees/sprint-<axis>` 布局继续识别。
   - 重试启动时仍复用在跑的席位（AGENTS.md 末段的约定）。
2. **模式**
   - `solo`、`dual`、`full` 不变。
   - `race`：每题 N 条同目标赛道，默认 N=2，席位名 `race-1..N`。
     - 每条赛道的 brief 写明「你是 N 条赛道之一，对手在做同一目标」。
     - 胜负不另设机制：各赛道都经门禁提交，由门禁与 `scheduler/candidate_archive.py` 的 top-3 决定。
   - `pipeline`：
     - `recon`：只读侦察，交付一份 blueprint，经 `parhelion_squad` 发给指定的下游席位；
     - `impl-1..K`：默认 K=2，各自负责 recon 或主控划分的一组瓶颈；
     - `integrator`：负责合并各实现席的分支，并提交候选；
     - `harness`：沿用现有的 calibration/bench-author，经平台接口增补用例与分桶。**不能**写平台套件之外的测量脚本，AGENTS.md 第 5 条不变。
3. **角色的权限**
   - 在 `cli/opsb_core/mcp/tools.py` 的角色过滤里增加 `recon` 与 `knowledge` 两个 worker 子角色。这是唯一的权限边界。
     - `recon` 不能调用 `parhelion_test`，也不能用 `parhelion_submit_gate` 入队；预览可以。
     - `knowledge` 同样不能测试与提交，交付物是 `parhelion_kb_record` 与文档路径。
   - 两个子角色的工具表都更短。工具表字节测试要覆盖它们。
4. **按题设模式**
   - `parhelion_sprint_start(problems=…)` 接受字符串列表（现有写法），也接受对象列表 `[{title, mode, seats}]`。
   - 未给 mode 的题用全局 mode。
   - 增加 `role=knowledge` 的「知识席」：可以不绑定题目，交付不是分数。
5. **冲刺中调整**
   - `parhelion_sprint_start(adjust=true, problems=[…])` 为列出的题增开席位，已在跑的席位不动。
   - `stop=[席位名]` 用现有的 `stop_agent` 停掉指定席位。
   - `replace=[title]` 换席：
     - 先给该题现有席位发一条指令，要求写交接并停在边界上；
     - 再新开一个正交方向的席位。新席位的启动包要带上：
       - 该题账本的 failures 视图（`ledger/view.py`）；
       - 被替换 worker 最近一次的交接叙述（`state/handoffs/<agent>.jsonl`）。
     - 换席按触发它的 stagnation 事件幂等：同一批回执重放不会重复换席。
6. **stagnation 的执行器**
   - `judge_feedback` 产生的 stagnation 战略消息里，附上可以直接执行的 `parhelion_sprint_start(replace=["<题>"])` 调用。
   - 配置项 `agents.auto_replace_on_stagnation`，默认 false（00-PLAN D4）。打开后由平台自己执行换席，仍然按事件幂等。
7. **同题远端检出隔离**
   - 先在真机上核实：同题两席同时测不同 SHA，会不会互相覆盖。
   - 会覆盖，就让非默认席位使用 `<root>/<Title>@<席位名>` 的远端检出。默认席位（solo、dual 的第一席）保持原路径，旧缓存不失效。
   - 涉及 `queue_cmd._remote_problem_dir`、`runtime/npu-test.py` 的远端目录参数，以及用例缓存键。缓存键只取 oracle 侧，与检出路径无关，核实后在报告里写明。
   - 写死的远端根目录移进 `device_env`，用中性默认值；仓库里不再出现这个路径。
8. **brief**
   - 在 `drivers/brief.py` 为 race、recon、integrator、knowledge 各写一段角色契约。
   - 每段都在 1.5KB 上限内，不要挤掉末行的数据通道说明。
   - 契约里写明这个角色的交付物与收件人：R2 §1.2 的必含字段，即独占目录与分支、命令、交付、收件人、停手条件。

## 约束

- `start_sprint` 的 `enable_auto_submit` 参数，语义与默认值都不变。
- 冲刺默认仍是每题一个 solo 席位。新模式必须显式开启。
- 新增的是参数，不是新工具。工具表字节在报告里写明改前改后。

## 复用

- `agents/manager.py` 的 spawn：已经有 `collab_mode` 的 pipeline 取值，以及 `track` 参数；
- `drivers/startup_pack.py` 的段落结构；
- `agents/peer.py` 的 squad 信封。

## 测试

- 新旧两种席位识别；
- 各模式的席位展开；
- 对象形式的 problems 与未知题名报错；
- adjust 与 stop；
- replace 的幂等与启动包内容；
- recon 与 knowledge 的工具过滤；
- 远端目录的推导。

## 验收

- 用 `parhelion_sprint_start(preview=true)` 在当前工作区展开 `pipeline` 与 `race`，把席位表写进报告。preview 不派发 worker。
- 真机：见下。

## 真机

- 在当前工作区任选一题，从工作树用平台命令起两个同题、不同 SHA 的 smoke 测试，同时运行，确认两者的结果各自对应自己的 SHA。只测不提交。
- 如果需要两个真实 worker 席位，**不要**派发。可以直接对两个 SHA 调 `opsb test`，模拟两席并发。
- 设备不通时标为待补。

## 报告（`docs/v4.3/reports/P26-sprint-seats.md`）

- 席位身份的迁移；
- 各模式的展开表；
- 角色工具表的字节；
- 远端隔离的核实与改法；
- 换席的幂等口径；
- AGENTS.md 需要改的段落：由主会话在发布时改写，你只列出建议。
