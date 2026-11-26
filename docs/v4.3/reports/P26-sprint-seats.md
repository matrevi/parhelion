状态：已交付

# P26 席位与冲刺 v2 · 实施报告

分支 `impl/P26-sprint-seats`，基于 4.2.1 的 `main`。子计划：[P26-sprint-seats.md](../P26-sprint-seats.md)。

## 1. 分支与提交

交付前的 `git log --oneline main..HEAD` 快照（本报告提交之后、交付提交之前）：

```text
$ git log --oneline main..HEAD
<this-commit> Report P26: seat identity, race/pipeline modes and the remote checkout finding
6caeb20 Test seat identity, race and pipeline seats, and seat replacement
49248d1 Give a sprint seat an identity, a mode and a replacement path
```

快照不含它自身这一笔之后的交付提交：分支最后一笔的标题以 `Deliver P26:` 开头（`git log` 末行）。
工作树 `/Users/winbeau/Developer/personal/parhelion-wt/P26`；`main` 未合入、未推送、未 rebase。

## 2. 子计划逐项完成情况

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 1 | 席位身份 | 已完成 | 总线 `agents` 表加 `seat` 列（沿用现有 ALTER 迁移）；席位按（题，席位名）识别；`.worktrees/sprint-<axis>` 旧布局继续识别；重试复用仍在跑的席位 |
| 2 | 模式 | 已完成 | `solo`/`dual`/`full` 不变；新增 `race`（N 条同目标赛道，默认 2）与 `pipeline`（recon + impl-1..K + integrator + harness，K 默认 2） |
| 3 | 角色权限 | 已完成 | `mcp/tools.py` 增 `recon`、`knowledge` 两个 worker 子角色；`recon` 无测试工具、门禁仅可预览；`knowledge` 无测试、无门禁；两表更短且有字节测试 |
| 4 | 按题设模式 | 已完成 | `problems` 接受字符串列表与 `[{title, mode, seats}]`；未给 mode 的题用全局 mode；`role=knowledge` 知识席可不绑定题目 |
| 5 | 冲刺中调整 | 已完成 | `adjust=true` 只给列出的题增开席位；`stop=[席位名]` 走现有 `stop_agent`；`replace=[title]` 先发交接指令再开正交新席，启动包带 failures 视图与最近交接叙述，按 stagnation 事件幂等 |
| 6 | stagnation 执行器 | 已完成 | `judge_feedback` 的 stagnation 消息附 `parhelion_sprint_start(replace=["<题>"])`；配置项 `agents.auto_replace_on_stagnation`，默认 false；打开后平台自己执行，仍按事件幂等 |
| 7 | 同题远端检出隔离 | 部分完成 | 真机核实结论是**不互相覆盖**（见 §6），子计划「会覆盖才隔离」的判据未触发，因此未启用 `<root>/<Title>@<席位>`；写死的远端根目录已移入 `device_env`，仓库内不再出现该路径（本包涉及的那一处） |
| 8 | brief | 已完成 | `drivers/brief.py` 增 race / recon / integrator / knowledge 四段角色契约，各 ≤1500 B，末行保留数据通道说明 |

`部分完成` 的只有第 7 项的隔离半边；判据、证据与改法在 §6，剩余硬编码路径在 §11 NOTE 3。

四段角色契约的字节（上限 1500，末行都是数据通道说明）：

| 契约 | 字节 |
|---|---:|
| race | 1233 |
| recon | 1047 |
| integrator | 953 |
| knowledge | 1002 |

## 3. 全量测试

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
817 passed, 19 skipped, 2 subtests passed in 76.74s

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
556 passed in 63.16s
```

基线 4.2.1 为 801 passed / 19 skipped 与 540 passed；增量是 `cli/tests/test_sprint_seats_p26.py`
的 16 条测试（工具表字节测试在原文件内扩写）。

**Swift：未构建，也不需要。** 本包没有改 `scheduler/cli_bridge.py`，也没有给
`build_hot_mesh` 的 JSON 加字段（`agents` 记录多一个 `seat` 键，属性式解码忽略多余键）。
`runtime/tests/test_device_broker.py` 的两条预算测试本轮没有出现偶发失败。

## 4. 验收：当前工作区的 preview 席位表

在 `/Users/winbeau/Competitions/mat-chem-sim-pred` 上，用**工作树代码**调
`parhelion_sprint_start(preview=true)`，只预览、不派发：

- 预览进程用临时 `PARHELION_HOME`（`/tmp/…`）启动，总线库建在临时目录里；
  **线上总线库没有被迁移**（核验：`~/.parhelion/workspaces/mat-chem-sim-pred-*/state/agent_bus.db`
  的 `agents` 表仍是 28 列、无 `seat` 列）。
- 比赛目录只读：预演没有写任何文件。

`mode=pipeline`（前两题，`seats=2`，10 个席位）：

| 题 | 席位 | 角色 | 分支/工作树 | 状态 |
|---|---|---|---|---|
| NeighborListPbcFixedK | recon | recon | `sprint-recon` | planned |
| NeighborListPbcFixedK | impl-1 | worker | `sprint-impl-1` | planned |
| NeighborListPbcFixedK | impl-2 | worker | `sprint-impl-2` | planned |
| NeighborListPbcFixedK | integrator | integrator | `sprint-integrator` | planned |
| NeighborListPbcFixedK | harness | bench-author | `sprint-harness` | planned |
| MeshEdgeMessagePassing | recon | recon | `sprint-recon` | planned |
| MeshEdgeMessagePassing | impl-1 | worker | `sprint-impl-1` | planned |
| MeshEdgeMessagePassing | impl-2 | worker | `sprint-impl-2` | planned |
| MeshEdgeMessagePassing | integrator | integrator | `sprint-integrator` | planned |
| MeshEdgeMessagePassing | harness | bench-author | `sprint-harness` | planned |

`mode=race`（前两题，`seats=2`，4 个席位）：

| 题 | 席位 | 角色 | 分支/工作树 | 状态 |
|---|---|---|---|---|
| NeighborListPbcFixedK | race-1 | race | `sprint-race-1` | planned |
| NeighborListPbcFixedK | race-2 | race | `sprint-race-2` | planned |
| MeshEdgeMessagePassing | race-1 | race | `sprint-race-1` | planned |
| MeshEdgeMessagePassing | race-2 | race | `sprint-race-2` | planned |

两种模式都是 `ok=true`，全部 `planned`（`preview` 不派发、不写总线、不碰设备）。

## 5. 各模式的展开表

| mode | 每题席位数 | 席位名（角色） | collab_mode | 工作树/分支 |
|---|---|---|---|---|
| `solo`（默认） | 1 | `solo`（worker） | solo | `.worktrees/sprint-solo` / `sprint-solo` |
| `dual` | 2 | `algorithm` `dataflow`（worker） | orthogonal | `sprint-<席位>` |
| `full` | 3 | `+ calibration`（bench-author） | orthogonal | `sprint-<席位>` |
| `race` | N=2 | `race-1..N`（race） | orthogonal | `sprint-race-<n>` |
| `pipeline` | K+3（K=2 → 5） | `recon`（recon）`impl-1..K`（worker）`integrator`（integrator）`harness`（bench-author） | pipeline | `sprint-recon` / `sprint-impl-<n>` / `sprint-integrator` / `sprint-harness` |

`seats` 参数只影响 `race`（N）与 `pipeline`（K），取值 1..8；固定模式忽略它。
race 的胜负不另设机制：各赛道都经门禁提交，候选由门禁与 `scheduler/candidate_archive.py` 的
top-3 决定（本包未改这两处）。

## 6. 席位身份的迁移、远端隔离的核实与改法

### 6.1 席位身份

- **新列**：`agents` 表加 `seat TEXT NOT NULL DEFAULT ''`，走 `AgentBusStore._init_db` 里既有的
  `PRAGMA table_info` + `ALTER TABLE ... ADD COLUMN` 迁移；旧库打开时自动补列，旧记录为空串。
- **写入**：`spawn_agent(..., seat=...)` 写入；冲刺席位由 `sprint.py` 显式传席位名，普通
  `parhelion_agent_spawn` 不传（保持空，不被误认成席位）。
- **读取**：`seat_plan.seat_of(worker)` = 列值，否则回落到 4.2 布局
  `.worktrees/sprint-<axis>`（axis 属于 solo/algorithm/dataflow/calibration 才认）。
  `sprint_seats()` 的键从「（题，工作树目录名）」改为「（题，席位名）」，
  `manager.build_hot_mesh` 同步改用 `seat_of()`；两处行为对 4.2 记录完全等价。
- **兼容**：4.2 的 `.worktrees/sprint-solo` 等目录仍然识别为同名的 `solo` 等席位，
  重试启动仍是「复用仍在跑的席位」；旧记录与新记录可以在同一题上共存。
- **不在 PATH 依赖**：没有改 `cli_bridge.py` 的 JSON，协作页字段不变。

### 6.2 远端检出：核实结论是「不互相覆盖」

`opsb test` 在这套工作区（官方模板 + `reference.py`）走**第三条**路径。三条远端路径的实际行为：

| 路径 | 触发条件 | 远端检出 | 同题并发 |
|---|---|---|---|
| A 参考模板 `scheduler/case_bench.py` | 仓库有 `main.asc`+`reference.py`，且非 `--fast/--build-only/--sync-only` | 把**调用方自己的**工作树按请求 SHA 打包，在设备主机解到**每次运行独立的** `opsb-case-*` 临时树 | 各自独立，不共享任何文件（代码注释与实测都如此） |
| B `REMOTE_TEMPLATE`（`runtime/npu-test.py`） | 官方模板的构建/编译检查（`--build-only`、judge 编译检查） | 共享 `<远端根>/<contest>/<Title>`，先取 `<Title>.lock` 的同题 flock | 串行排队，不覆盖 |
| C `REMOTE_3PHASE`（legacy `build.sh` 布局） | 其余 | 共享 `<work_root>/<work_prefix>/<Title>`，取 `<work_prefix>-repo-<Title>.lock` | 串行排队，不覆盖 |

所以「同题两席同时测不同 SHA 会不会互相覆盖」的答案是：**不会**。A 路径根本没有共享检出；
B、C 路径有按题的 flock，第二个运行在整个「检出+编译+测量」区间排队。按子计划的判据
（「会覆盖，就让非默认席位用 `<root>/<Title>@<席位名>`」），**不启用席位级检出隔离**，
默认席位与非默认席位都保持原路径，旧缓存不失效。

真机证据（见 §7）：同题、两个不同 SHA、同时启动的两次 smoke 各自在设备主机的不同
`opsb-case-*` 临时树里编译，结果各自对应自己的 SHA；共享检出
`/…/<contest>/<Title>` 的 HEAD 在前后两次查询里都是同一个旧 SHA（未被本次运行触碰）；
该目录下的 `<Title>.lock` 文件确实存在（B 路径的按题 flock）。另外一次故意选的坏 SHA
（历史提交，模板实例化对不上）只在自己那棵树里编译失败，没有影响并发的另一发。

**用例缓存键**：`runtime/npu_runtime/case_cache.py` 的 `CaseCache.key` =
`sha256([CACHE_FORMAT, python 版本, numpy 版本, _oracle_digest(repo), sha256(reference_cases.py),
sha256(schema.json)] + json(item))`；`_oracle_digest` 遍历检出但**跳过** kernel 源
（`.asc/.h/...`）、`CMakeLists.txt` 与 `.pyc`；缓存目录是 `<top>/<title>`。
即：**键只取 oracle 侧内容，与检出路径、与 kernel SHA 无关**。实测旁证：两个不同 SHA 的并发
运行里，其中一个命中了 1 个缓存条目（`"cached": 1`），另一个为 0——键不随 SHA/路径变化。

**写死的远端根目录**：`device_env.DEFAULTS` 增 `contest_work_root`（中性默认 `""`，回落
`work_root`），新函数 `contest_work_root()`；`queue_cmd._remote_problem_dir` 与
`runtime/npu-test.py::_remote_root_for` 都改用
`<contest_work_root 或 work_root>/<contest_id>/<Title>`。已配置的主机行为不变
（本机实测仍是 `<work_root>/<contest>/<Title>`），未配置时是中性 `/srv/work/<contest>/<Title>`。
`cli/opsb_core/commands/queue_cmd.py` 里不再有该主机路径字面量。

## 7. 真机验证

设备主机可达（`ssh` 探测一次通过，32 核）。为不写比赛目录，把**一题**（
`NeighborListPbcFixedK`）连 `.git` 复制到 `/tmp/p26-verify/ws`，用工作树代码
（`cli/opsb test … --workspace /tmp/p26-verify/ws`）在副本里跑，只测不提交。

1. 单发 smoke（`14ee90e…`）：`exit 0`，2/2 PASS，`built=true`。
2. 并发两发（同题、不同 SHA，在同一条 shell 命令里同时启动，`--remeasure` 强制新跑）：

```text
start A 17:29:30   A: sha 3f333621…  exit 0  PASS 2/2  built=true   结束 17:30:01
start B 17:29:30   B: sha d70506fd…  exit 0  PASS 2/2  built=true   结束 17:29:57
```

- 两发在设备主机上各自编译于 `/tmp/opsb-case-2l4zj_1_/repo` 与 `/tmp/opsb-case-_zraxmhs/repo`
  （互不相同的临时树），重叠约 27 秒。
- 结果分别落在 `results/NeighborListPbcFixedK/<自己的 SHA>/bench-runs/…`，报告的 `sha` 字段
  分别等于各自的请求 SHA——**结果各自对应自己的 SHA**。
- 共享检出 HEAD 前后一致（同一旧 SHA），未被触碰。
- 缓存命中 1/0（见 §6.2）。
- 结论：同题两席并发 smoke 不互相覆盖；设备侧串行（单卡）不变。**只测不提交，没有入队、没有判题。**

预演与真机的具体设备值（芯片、驱动、工具链路径）留在 `config.json` 的 `device` 段与本机环境，
本报告不写。

## 8. 角色工具表的字节

`tools/list` 序列化后的 UTF-8 字节（同一测试口径）：

| 角色 | 工具数 | 改前 | 改后 | 上限 | 余量 |
|---|---:|---:|---:|---:|---:|
| orchestrator | 29 | 14601 | **14852** | 15000 | 148 |
| worker | 24 | 12507 | 12507（未变） | 13000 | 493 |
| recon（新） | 18 | — | **9711** | 13000（沿用 worker 上限） | 3289 |
| knowledge（新） | 14 | — | **8415** | 13000（沿用 worker 上限） | 4585 |

- 主控表 +251 B：`parhelion_sprint_start` 增 `seats` / `role` / `adjust` / `stop` / `replace`，
  `problems` 的 items 变 `anyOf[string, object]`，`mode` 的 enum 增加两项；同时压缩了该工具的
  description 与 mode 说明来腾空间。**上限没动**。
- worker 表 0 变化：sprint_start 仍是主控专用，subrole 只做减法。
- 两个子角色的减法（工具表 = worker 表 − 禁用集）：`recon` 去掉 4 个测试工具、
  `parhelion_escape_rebind`、`parhelion_escape_reproducer`（保留 `parhelion_submit_gate` 但只许预览）；
  `knowledge` 再去掉 `parhelion_submit_gate`、`parhelion_bench_diagnose`、`parhelion_pmu_card`、
  `parhelion_elf_cold3_check`。
- 权限落点：`mcp/tools.py` 的 `tool_allowed` 同时用于 `list_tool_schemas` 与 `call_tool`；
  worker 的 MCP 角色由总线记录的 `role_label` 解析（`worker_subrole`），因此
  `recon`/`knowledge` 席位不需要改驱动层就能拿到短表。

## 9. 换席的幂等口径

- **触发身份**：该题在总线上**最新一条 stagnation 战略消息**的 `event_key`（即
  `strategy:<submission_id>:stagnation`）；没有 stagnation 事件时为字面量 `manual`。
- **换席回执**：换席成功后写一条 `seat_replace` 总线消息，`event_key` 为
  `seat-replace:<题小写>:<触发>`；若上一次换席的席位已经停/错，则退避到 `…:retry-<n>`。
- **重放**：同一批回执重放不会产生新的 stagnation 消息（`event_key` 唯一），触发身份不变，
  于是第二次 `replace` 直接返回 `state=existing`，不新开席位（测试
  `test_replace_hands_over…` 与 `test_replace_is_keyed_by_the_stagnation_event` 钉住）。
- **自动换席**：`agents.auto_replace_on_stagnation`（默认 false）打开后，`deliver_judgements`
  只在**新写入** stagnation 消息时调用同一个 `start_sprint(replace=[…])`，所以自动与手动共用一套
  幂等口径。
- **新席命名**：每题单调 `replace-1`、`replace-2`…（不复用已停席位的名字），分支
  `sprint-replace-<n>`，方向取该题在跑席位未覆盖的正交方向（dataflow → algorithm → numerical）。
- **不立刻 kill 旧席**：只发「写交接并停在边界」的指令（子计划要求），旧席继续跑完当前回合；
  要立刻停用 `stop=[…]`。

## 10. AGENTS.md 需要改的段落（建议清单，本包未改 AGENTS.md）

1. **末段（「收到整场启动指令后…」）**：模式补 `race` 与 `pipeline`，说明「一题可用对象形式单独
   指定 mode/seats」「冲刺中可 `adjust` 增席、`stop` 停席、`replace` 换席」；把「每方向独立工作树/
   分支/worker 身份」改为按「（题，席位名）」表述，并写明旧 `.worktrees/sprint-<axis>` 布局继续识别。
2. **第 6 条（六发无增益换人）**：补「stagnation 消息已附可执行的
   `parhelion_sprint_start(replace=["<题>"])`；平台开关 `agents.auto_replace_on_stagnation` 默认关，
   打开后由平台执行，仍按判题回执事件幂等」。
3. **第 5 条（单台设备按整题调度）**：补一句「同题多席不共用远端检出：参考模板路径每次运行在设备
   主机的独立临时树里编译；构建/编译检查路径按题 flock 串行」——不写任何具体设备路径。
4. **第 9 条或角色段落（平台接口）**：补 worker 子角色边界——`recon` 只读、不能跑测试、门禁只能
   预览；`knowledge` 不跑测试、不入队，交付 KB 条目与文档路径。
5. **第 1 条或新段（平台管工具）**：说明知识席的交付不是分数（不占题、不参与门禁排序）。

## 11. NOTE：非显然的约束与与子计划不一致的源码事实

1. **00-PLAN §3 与子计划措辞不同**：00-PLAN 写「按（题，分支）识别席位」，子计划写「按（题，席位名）」。
   本包按子计划实现：身份是席位名，分支名由 `sprint-<席位>` 派生，二者一一对应。
2. **三条远端路径**（§6.2）：当前工作区的 smoke/standard 走的是参考模板路径，压根不用共享检出；
   只有构建类运行（B）与 legacy 布局（C）用共享检出并按题串行。子计划描述的是 B/C 的旧行为。
3. **仓库里仍有其它主机路径字面量**：`scheduler/backfill.py`（die lock 根）、
   `scheduler/npu_scheduler.py`（dry-run 计划里展示的 `PYTHONPATH`）、`runtime/npu-test.py` 顶部注释、
   `docs/SCHEDULER_DESIGN.md`。这些不在 P26 的文件集里，且 die lock 的根要与设备侧其它进程一致，
   改动需要单独设计，未动（见「待决」）。
4. **`queue_cmd._remote_problem_dir` 目前是不可达路径**：它只在 bridge 不提供 `--build-only`
   时才被直连 ssh 的 CPU 构建分支调用；当前 bridge 提供该参数，所以不会走到。它也是唯一没有
   取按题 flock 的远端路径——保留在报告里作为残余风险，不扩大改动。
5. **`seats` 对固定模式无效**：`solo/dual/full` 忽略 `seats`（不报错），只有 `race`（N）与
   `pipeline`（K）使用。
6. **`enable_auto_submit` 语义未变**：普通启动仍是「显式 true 才翻开关、且要求该工作区是当前
   工作区」；`adjust`/`stop`/`replace` 不会翻自动提交开关（这三种动作不涉及「开整场」）。
7. **知识席与题绑定**：`role=knowledge` 且给了题时，每题一个 `knowledge` 席位（`problem` 有值，
   题内报告/账本可用）；不给题时开一个不绑定题的席位，总线记录的 `problem` 是工作区名，
   于是 `parhelion_report`/`parhelion_note` 的「自己的题」作用域对它是空题——这类席位应走
   `parhelion_kb_record`（显式给 `problem`）与文档路径，或改用「带题的知识席」。未改
   `problem_of` 的既有作用域规则。
8. **换席不 kill 旧席**（见 §9），因此换席后同一题会短暂有两套席位在跑；旧席按指令停在边界，
   要立刻回收用 `stop`。
9. **`recon` 席位仍有独立工作树**：计划里写「只读侦察」，权限边界限制的是平台工具（不能测试/入队），
   没有做文件系统只读沙箱；`recon` 的契约要求它只提交 blueprint。
10. **工具表余量只剩 148 B**（主控）：P27 要加 schema 时需先删字（00-PLAN 已把余量列为冲突热点）。
11. **`judge_feedback` 的 stagnation 文案带题名时才附可执行调用**：回执行没有 `title` 时只给原来的
    文案（测试用的最小回执行就是这样），不影响既有事件语义。
12. **总线迁移只在 4.3 进程打开库时发生**：本次预演用临时 `PARHELION_HOME`，线上总线没被迁移；
    合入后由守护进程第一次打开时补列（`ALTER ... DEFAULT ''`，旧代码读到多余列无影响）。

## 12. 风险与后续

- **编译 CPU 争用**：race/pipeline 把同题并发从 1 提高到 N+K 级别；R3 的结论是设备不是瓶颈、
  编译才是。B/C 路径的同题运行会按题串行（不覆盖但排队），A 路径可真正并行。下一场要先看
  `opsb status` 的编译队列与实测分/判，再决定 race 的 N。
- **知识席产出质量**：知识席交付不是分数，靠契约约束；建议下一场结束后回看 `parhelion_kb_record`
  的条目是否有证据等级（P28 落地后再收紧）。
- **自动换席默认关**：D4 的默认值保持；打开后由平台在判题回执循环里换席，可能在被替换席位仍
  在写交接时启动新席——两条分支可能同时改同一题的同一方向，正交方向选择只按席位名规避，
  不保证语义正交。
- **远端隔离的真正缺口**：如果将来有工作区走 B/C 路径且同题并发高，按题 flock 会退化成串行；
  那时再启用席位级 `<root>/<Title>@<席位>`（`_remote_problem_dir` 与 `_remote_root_for` 已集中到
  一个 device 设置，加参数即可）。
- **follow-up**：P27 需要工具表余量，建议先合并 P26 再按其计划压缩或调预算（由主会话决定）。

## 13. 待决

1. **工具表上限**：主控表余量 148 B，P27 是否提高上限（15000 → 15300？）或在 P27 内压缩既有描述，
   需主会话决定。本包按 10-DSH §2 未改上限。
2. **剩余主机路径字面量**（NOTE 3）：`scheduler/backfill.py` 的 die lock 根、`npu_scheduler.py` 的
   dry-run 展示、`runtime/npu-test.py` 注释、`docs/SCHEDULER_DESIGN.md`。建议 4.4 单独一包，
   与设备侧常驻进程的锁根一起迁移；本包不越界。
3. **不绑定题的知识席**：是否给一个工作区级（不受「自己的题」限制）的只读报告视图，属于权限边界
   决定，未自行放开。
4. **协作页是否展示席位名**：需要给 `build_hot_mesh` 加字段并同步 Swift 解码；本包故意不加。
5. **race 默认 N 与 pipeline 默认 K**：本包按子计划默认 2；是否按下一场实测调整，留给发布前的
   数据决定。
