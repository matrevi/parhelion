状态：已交付

# P28 知识库 v2：证据等级与过程知识

分支 `impl/P28-kb-evidence`，工作树 `/Users/winbeau/Developer/personal/parhelion-wt/P28`。
子计划范围 1、2、3、5、6 在阶段 A 完成；范围 4（`parhelion_submit_gate` 回复附带相关知识条目）
在 P23 合入后完成。最终基线是含 P21/P22/P23/P24/P26 的 `main`（`31cd89e`）。

## 1. 分支与提交

```text
$ git log --oneline main..HEAD
364be42 Document the P28 stage A knowledge evidence work
da3d23d Add KB evidence levels, mechanism fields and process cards
```

本报告与范围 4 的实现一起放在最后一个提交里，标题 `Deliver P28: ...`（阶段 A 两个提交的内容没变，rebase 只换了父提交）。

基线说明：阶段 A 开工时的 `main` 是 `c070ea1`；阶段 B 按主会话要求先 rebase 到 `integ/base-P23`（`8c5a414`，
与当时的 `main` 树相同），做完范围 4 时 `main` 已推进到 `31cd89e`（合入 P23、P24），于是再 rebase 到它。
两次 rebase 都没有冲突（P24 也改了 `mcp/handlers_bench.py`，但改的是 gate 回复的其它段落，见 NOTE 2）。
最终分支只含本包三个提交，对 `main` 的 diff 只有本包文件。

## 2. 子计划逐项完成情况

| # | 范围 | 状态 | 说明 |
|---|---|---|---|
| 1 | 字段：条目级 + 证据行级 | 已完成 | 8 个新字段全部可选，旧条目读时补默认；写入路径校验；MCP/CLI 可声明证据等级 |
| 2 | 类目：`scoring-law`、`measurement`、`process` | 已完成 | 只进现有支柱，未新增支柱；过程卡默认进 community |
| 3 | 晋升规则 + 幂等迁移 | 已完成 | hardware 必须有 `paired` 或 `online_receipt`；其他支柱维持贝叶斯门控；迁移只补证据等级、不改状态 |
| 4 | 平台强制的读取（门禁回复附带条目） | 已完成 | 新模块 `knowledge/hints.py`，`handlers_bench.py` 只加薄接入点；预览与入队都附带；不影响判定（见附录 E） |
| 5 | 过程卡（4–6 张，与设备无关） | 已完成 | 6 张，登记进 `templates/community/index.json`；播种非破坏式；词表测试兜底 |
| 6 | Swift 解码与详情页 | 已完成 | 2 个文件 +46 行（未超 50 行门槛）；`swift build` 通过 |

## 3. 两条全量测试末行与 Swift 构建

在最终交付的树上（rebase 到含 P23 的 `main` 之后）：

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
945 passed, 19 skipped, 2 subtests passed in 88.64s (0:01:28)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
624 passed in 79.52s (0:01:19)
```

阶段 A 在旧基线上是 812 / 551；rebase 后增量里含已合入 `main` 的 P21/P22/P23/P24/P26 用例。本包新增
**20 个用例**（阶段 A 11 个在 `cli/tests/test_kb_evidence.py`，阶段 B 9 个在
`cli/tests/test_kb_gate_hints.py` 与同文件的两条守卫）。`runtime/tests/test_device_broker.py` 的两条预算
测试两轮都通过，没有偶发失败。

Swift 没有阶段 B 改动，仍按阶段 A 的解码改动计：`swift build` → `Build complete! (2.25 sec，增量)`。

## 4. 真实数据验证（全部在副本上）

真实 KB 只读复制到 `/tmp/p28kb`（`hardware` / `community` / `snippets` / `breakthrough` / `bugs` 五个目录），
所有查询与迁移都在这份副本上执行，真实 `~/.parhelion` 未写。

- 条目 202 条、证据行 237 条。
- 迁移：`backfilled=196`、`entries_touched=157`、`hardware_pending_validation=39`、
  `hardware_promoted_kept=15`；第二遍 `backfilled=0, entries_touched=0`（幂等）。
- 门禁附带：在真实 KB 副本上 `select_gate_hints(problem="CTCLossV2", subject="avoid k=2 eo step fusion")`
  返回 2 条 bugs 条目、合计 280 字节；单次耗时 2.6–4.6 ms（热/冷）。真实条目原文含历史分数，按仓库
  惯例不在报告里引用。
- 模板播种副本（22 条、27 条证据行）：`backfilled=27`、`hardware_pending_validation=0`、
  `hardware_promoted_kept=2`；第二遍 0/0。

`parhelion_kb_query` 在副本上的 brief 输出样例（1004 字节；为不把真实设备型号带进仓库，样例关掉了
anti-pattern 列表并选了一条不含设备值的条目）：

```json
{
 "result_count": 1,
 "anti_pattern_count": 0,
 "results": [
  {
   "id": "HW-256-3-fft-case-05-tle-per-tile-o-n-2-sca",
   "pillar": "hardware",
   "title": "256^3 FFT case-05 TLE: per-tile O(N^2) scalar twiddle rebuild dominated 7.7 s; vector recurrence + N-dependent reset/chunk = 2.5x",
   "confidence": 0.667,
   "tags": [],
   "summary": "A per-tile O(N^2) scalar twiddle-row rebuild was 7.7 s of a 256^3 FFT run, not bandwidth (the 6.4 GB of traffic is ~4 ms). A vector recurrence with an N-dependent exact-reset period plus a widened merge chunk cut it to ~3 s/run (2.5x). The 15 s local case budget covers 2 cold + 5 warm reps, so a green validation needs <=2.14 s/run; the best kernel in the workspace measures 2.78 s/run and therefore only validates under a long-budget experiment profile.",
   "evidence": {
    "local": 1,
    "paired": 0,
    "online_receipt": 0
   },
   "validation": "pending"
  }
 ],
 "anti_patterns": [],
 "full_hint": "full=true returns complete entries",
 "page": {
  "offset": 0,
  "limit": 1,
  "total": 2
 },
 "next_cursor": "o1"
}
```

同一次查询的 digest 末行（`|ev=local:1 待验证`）：

```text
- `[hardware|unverified|conf=0.67|portable]` `HW-256-3-fft-case-05-tle-per-tile-o-n-2-sca` — **…**: … | doc: `entries/HW-256-3-fft-case-05-tle-per-tile-o-n-2-sca.md` |ev=local:1 待验证
```

## 5. NOTE（非显然约束与源码事实）

1. **两次 rebase 都没有冲突，包括预期中的 `mcp/schemas.py`。** P23 的 `intent` 在
   `parhelion_submit_gate` 块（`schemas.py:251`），阶段 A 的 `evidence_class` 在 `parhelion_kb_record` /
   `parhelion_kb_feedback` 块（`schemas.py:145/163`），两处是不同的 hunk，三方合并成功；第二次 rebase
   到含 P24 的 `main` 也是干净的。已经用测试断言两个 enum 同时存在、参数名逐字一致，没有丢掉任何一边。
2. **`handlers_bench.py` 与 P23、P24 没有语义重叠。** P23 改的是 handler 顶部（`intent` 归一化与
   `--intent` 传参，`handlers_bench.py:450-457`）和 ledger link 的 intent（`:494`）；P24 改的是同一
   handler 里的 S1 体检与逃逸锁字段；范围 4 只在 `return result` 之前追加 7 行（局部 import + 一次
   `gate_hints()` + 条件挂载）。匹配、裁剪、排序全部在 `cli/opsb_core/knowledge/hints.py` 里，handler 里
   没有任何判定逻辑。
3. **证据等级词表在本分支只有一份定义。** `cli/opsb_core/knowledge/query.py:19` 的 `EVIDENCE_CLASSES`
   是唯一来源；`mcp/handlers_kb.py` 原来那份字面量已改成从它 import（`is` 相同的对象有一行守卫测试）。
   **但 P27 已在 `cli/opsb_core/evidence.py::CLASSES` 另外定义了一份同样的三值**（P27 已交付、未合入，
   不在本分支基线里，无法 import）。两条分支的取值逐字相同 `("local", "paired", "online_receipt")`，
   守卫测试 `test_evidence_class_vocabulary_is_pinned_for_p27` 把三个字面量与两个 MCP schema 的 enum
   一起钉死。合并后需要消重（见「待决」）。
4. **`mechanism_confirmed` 缺省是 `no`，语义是"未被证实"，不是"被证伪"。** 反例仍由 `verdict` 为
   `fail` / `anti-pattern` 的证据行表达，条目上另加 `failure_domain`。
5. **旧证据行的缺省是 `local` / `n_draws=1` / `sigma_pts=0.0` / `repro=""`。** 读取时不做任何文本推断
   （不按 note 里的 "online" 猜等级），所以现有 108 条硬件证据行全部是 `local`。这是保守选择：宁可把
   旧条目当"待验证"，也不把没有声明过的证据升格。
6. **breakthrough 支柱的合成证据类是 `online_receipt`。** `_normalize_entry` 对"有 sha、无 evidence 行"
   的突破条目会合成一条证据；那条本来就是在线上判出来的分，因此标 `online_receipt`（磁盘数据不变）。
7. **派生字段不落盘。** `record_kb_feedback()` 会把规范化后的条目写回 `index.json`；新增的
   `evidence_composition`、`evidence_strong`、`pending_validation`、`retracted` 加进
   `DERIVED_ENTRY_KEYS`，只用于读取，避免索引里出现会过期的快照。
8. **类目枚举在 `cli/opsb_core/knowledge/query.py:27` 的 `VALID_CATEGORIES`，含 `hardware-law`**
   （硬件支柱的默认类目）。写入路径现在会拒绝枚举外的类目；读路径不纠正旧值（数据只读）。
9. **MCP 加了 1 个参数（阶段 A），范围 4 没有加参数。** `parhelion_kb_record` 与
   `parhelion_kb_feedback` 各有一个 `evidence_class`（schema + handler 两处登记，角色沿用 `tools.py`
   的默认 roles）。**为了让工具表停在 ≤14800 B，`parhelion_submit_gate` 的 description/schema 没有加
   "回复会附带 hints" 的说明**，只靠回复里的 `knowledge_hints` 字段自解释。工具表：主控
   **14716 B**（余量 284）、worker **12528 B**（余量 472），上限未改。
10. **`knowledge_hints` 是纯附加字段。** 只有匹配到条目时才出现；它追加在 `stages`、`gates`、
    `approved`、`priority_score`、`estimate`、`next_step` 之后，不参与任何 stage、优先级或审批计算。
    预览与入队走同一个 `result` 对象，所以两条路径都带。
11. **附带逻辑没有副作用。** `select_gate_hints()` 只读 `~/.parhelion/<pillar>/index.json`，不做
    `ensure_triad_kb_seeded`（缺失或损坏的 KB 返回空列表），不写盘；`gate_hints()` 读不到提交标题或 KB
    时返回 `[]`，绝不抛出。真实 KB 上单次 2.6–4.6 ms。
12. **改了一处既有测试。** `cli/tests/test_v310_four_pillar_kb_and_guards.py` 原来断言"硬件条目 win +
    delta_raw 即晋升"，这正是 P28 第 3 条要废掉的旧规则；现在该用例显式给 `evidence_class="online_receipt"`。
13. **迁移不自动执行。** 查询与播种路径都不写盘（读不该触发迁移），迁移由
    `opsb kb migrate-evidence [--dry-run]` 显式调用，函数在
    `cli/opsb_core/knowledge/migration.py:271` 的 `migrate_evidence_classes()`。
14. **现有真实 KB 里已经有设备型号写进条目 id/摘要**（例如 `HW-<chip>-…`）。本包按"数据只读"没有修改
    它们，新写的过程卡、代码、测试与报告里没有设备值。发布时是否清洗由主会话决定（见「待决」）。
15. **过程卡的 `status` 是 `unverified`。** 它们没有 sha 证据行，写 `verified` 会绕过第 3 条的证据门；
    查询里它们的 `confidence=0.5`，排在已验证条目之后。
16. **Digest 只对 hardware 条目附加证据构成**（`|ev=local:2`、`|ev=local:1 待验证`），其他支柱不加，
    免得每条查询都多付上下文；完整条目（`format=full`）里所有条目都有构成字段。

## 6. 风险与后续

1. **证据等级词表有两份定义（P27 与本包）。** 合并后在同一个 `main` 上会出现两个独立的常量；守卫测试
   只能钉住本包这一侧与 MCP schema，钉不住另一个文件。后合入的一方应改成对另一方的别名（见「待决」）。
2. **工具表余量只剩 284 / 472 B。** 阶段 B 没有加 schema 字节（14716 / 12528）；P27 已交付、未合入的
   参数大约还要 240 B，按当前数字仍在 15000 以内，但只剩约 40 B 余量。若 P27 再多加描述就会撞线。
3. **`paired` 证据目前没有产生器。** 本包只定义词表、门与展示；真正产出 `paired` 证据要靠 P27 的广播
   证据等级与 P23 的配对测量/回执。没有产生器时，硬件规律会停在候选。
4. **worker 契约还没教怎么声明证据等级。** 如果下一场 worker 仍按老习惯经 MCP 写硬件条目（不带
   `evidence_class`），新规则会把它们全部留在"待验证"。建议发布时在 worker 提示词或 P27 的 brief 里写
   一句：线上回执用 `online_receipt`，同批用例配对差用 `paired`，本地 profiler 只算 `local`。
5. **旧数据全部是 `local`。** 现有 39 条未晋升硬件条目会立刻显示「待验证」，15 条已晋升条目保持
   `verified` 但构成显示为 `local: n`。这是子计划第 3 条要求的"不降级"，不是 bug。
6. **过程卡是软知识。** 六张卡没有 sha 证据行，`status=unverified`，在查询里不会盖过已验证条目；门禁
   附带的候选只取 anti-pattern 与 bugs，不取过程卡。
7. **门禁附带依赖关键词重合。** 中文提交说明没有 ASCII 机制关键词时只能命中"本题同名条目"；如果下一
   场 worker 用中文写 note，附带的命中率会下降。要提升需要给中文分词或让 brief 规定英文机制关键词，
   两者都不在本包范围。
8. **附带条目的排序是固定的。** 同题优先、再按关键词命中数、再按 id；没有按"最近一次"或"失败类型"
   加权。如果主控觉得同题旧 bug 太多，可以再加一条时间/类别过滤（需要新参数或新字段）。

## 7. 待决

1. **词表重复（P27 与本包）。** 建议后合入的一方改成别名：若 P27 先合入，`knowledge/query.py` 改成
   `from ..evidence import CLASSES as EVIDENCE_CLASSES`（`evidence.py` 更底层、不依赖 KB）；若本包先合入，
   `evidence.py::CLASSES` 从 `knowledge.query` 再导出。谁是唯一来源由主会话决定。
2. **`parhelion_submit_gate` 的 description 是否补一句"回复附带 ≤2 条知识提示"。** 当前不写是为了不再
   吃工具表字节（14716 / 目标 ≤14800）；若要写，需要先确认 P27 合入后的余量。
3. 真实 KB 里已有的设备型号 id/摘要是否在 4.3.0 发布时统一清洗（本包按数据只读未动）。
4. 是否要一次性把旧证据行的 `online` 类回填成 `online_receipt`（当前一律 `local`，保守但信息量为零）。
5. 门禁附带是否要在 worker 契约/提示词里说明（当前只在回复里出现 `knowledge_hints` 字段）。

## 附录 A 字段与类目表

条目级（全部可选，缺省在 `_normalize_entry` 补）：

| 字段 | 取值 / 含义 | 缺省 | 位置 |
|---|---|---|---|
| `mechanism_confirmed` | `yes` / `no` / `confounded` | `no`（未被证实） | `query.py:142` |
| `trigger` | 可测触发条件，文本 | `""` | 同上 |
| `cost` | 代价与风险，文本 | `""` | 同上 |
| `failure_domain` | 在什么条件下不成立 | `""` | 同上 |
| `applicable_problems[]` | 适用题 | `[]` | 同上 |
| `retracted_by` | 取代它的条目 id | `""` | 同上 |

证据行级（`_normalize_evidence_line`，`query.py:45`）：

| 字段 | 取值 / 含义 | 缺省 |
|---|---|---|
| `evidence_class` | `local` / `paired` / `online_receipt` | `local` |
| `n_draws` | 判了几次 | `1` |
| `sigma_pts` | 噪声大小 | `0.0` |
| `repro` | 运行 ID 或命令 | `""` |

反例：沿用现有 `verdict` 为 `fail` 或 `anti-pattern` 的证据行，条目上另加 `failure_domain`。

类目（`VALID_CATEGORIES`，`query.py:27`）：原有 `tiling-regime`、`async-pipeline`、`simd-transpose`、
`dma-coalescing`、`separable-folding`、`deterministic-sync`、`vectorization`、`hardware-law`；
新增 `scoring-law`、`measurement`、`process`。支柱仍是 5 个（`breakthrough` / `snippets` / `hardware` /
`community` / `bugs`），未新增，Swift 的 `TriadKBPillar` 不需要改。

写入路径：`evolution.validate_evidence()`（校验证据等级与三个数值字段）、
`evolution._entry_model_fields()`（`evolution.py:162`，校验条目级字段与类目）、
`mcp/handlers_kb.py:_validate_evidence()`（MCP 证据门同步）、
`opsb kb record / feedback` 新增 `--category --evidence-class --n-draws --sigma-pts --repro
--mechanism-confirmed --trigger --cost --failure-domain --retracted-by --applicable-problem`。

## 附录 B 晋升规则的改动

`cli/opsb_core/knowledge/evolution.py`：

- **创建**（`record_kb_entry`，`evolution.py:218`）：hardware 支柱若算出的状态是 `verified` 但没有
  `paired` / `online_receipt` 证据，降回 `unverified` 并返回
  `promotion_blocked="hardware promotion requires paired or online_receipt evidence"`。
- **反馈**（`record_kb_feedback`，`evolution.py:344`）：晋升分支同样先查强证据；不满足时状态不动、
  `transition="held-local-evidence"`、结果与进化日志事件里带 `promotion_blocked`。
- **其他支柱**：`_meets_promote()` 与贝叶斯后验保持 4.2.1 原样（新增测试
  `test_other_pillars_keep_the_bayesian_gate` 钉住 community 仍按 `delta_raw >= 1.5` 晋升）。
- **迁移**（`migration.py:271`）：`migrate_evidence_classes()` 只把缺失的 `evidence_class` 补成
  `local`，从不改 `status`；第二遍没有可补的就不写索引（文件字节不变，测试钉住），因此幂等且不降级。
- **查询展示**：hardware 条目在 digest 里带构成与「待验证」；MCP brief 里带
  `evidence: {local, paired, online_receipt}` 与 `validation: pending|strong`；已晋升的旧条目只多一个
  构成，不改 `status`。

## 附录 C 现有条目的证据构成

真实 KB 副本（`/tmp/p28kb`）上的统计，`evidence_class` 为读取后的值：

| 支柱 | 条目 | 证据行 | local | paired | online_receipt | 标为待验证 |
|---|---:|---:|---:|---:|---:|---:|
| hardware | 87 | 108 | 108 | 0 | 0 | 39 |
| snippets | 16 | 29 | 29 | 0 | 0 | 0 |
| community | 13 | 13 | 13 | 0 | 0 | 0 |
| bugs | 45 | 46 | 46 | 0 | 0 | 0 |
| breakthrough | 41 | 41 | 0 | 0 | 41 | 0 |
| **合计** | **202** | **237** | **196** | **0** | **41** | **39** |

- 硬件 87 条里：15 条已晋升（全部证据为 `local`，迁移后保持 `verified`，只显示构成）、39 条未晋升
  候选（标「待验证」）、33 条 `anti-pattern`（不计入待验证）。
- 模板播种副本：22 条 / 27 条证据行，`hardware_pending_validation=0`、`hardware_promoted_kept=2`。
- breakthrough 的 41 条 `online_receipt` 是读取时合成的证据行（见 NOTE 6），磁盘上仍没有 `evidence` 字段。

## 附录 D 过程卡清单

`templates/community/entries/`，均已登记进 `templates/community/index.json`：

| id | 类目 | 规则 | 可测触发条件 | 来源 |
|---|---|---|---|---|
| `COM-process-latest-state-change` | `scoring-law` | latest 榜型下一次失败提交会把整题榜面归零；提交前先备好 undo（在榜且线上 Pass 过的 SHA） | 规则 `ranking_submission_mode=latest`，或榜面在无新回执时归零 | 00-SUMMARY §4.1、§5.1；R4 §7 C11 |
| `COM-process-noise-is-not-a-breakthrough` | `measurement` | `abs(Δ) < σ̂` 只记为候选；证实机制用同 SHA 多判或同批用例配对，同源重投期望略负 | 只有一条回执且 `abs(Δ) < σ̂`；或同 SHA 重判落在 σ̂ 内 | R4 §2、§7 C1/C4；00-SUMMARY §4.9 补充、§5.2 |
| `COM-process-restore-before-experiment` | `process` | 归零后先 restore 到历史最好且本地全绿的线上 Pass SHA，再开新实验 | 在榜分为 0 或最近一发硬失败，且存在更高分的线上 Pass SHA | 00-SUMMARY §4.2、§4.10、§5.1；R4 §7 C5 |
| `COM-process-judge-time-shared-budget` | `measurement` | 判题时限是编译加评测共享的总时限；看模板实例化数、编译耗时、内核体积防 TLE | 编译耗时接近时限 80%，或实例化数/内核体积环比 > 1.5 倍，或本题出现过 TLE | 00-SUMMARY §4.5、§2 末；R4 §4、§7 C9/C10 |
| `COM-process-correctness-before-speed` | `process` | 一次迭代只做一件事：先修正确性与确定性，再做性能结构 | 逐例比对有 mismatch，或一发同时改正确性路径与性能结构 | 00-SUMMARY §2、§4.5；R4 §9 |
| `COM-process-local-profiler-needs-pairing` | `measurement` | 本地 profiler 只有 `local` 证据，要有 `paired` 或 `online_receipt` 才能升格为硬件规律 | 结论只有一次本地测量，或本地与线上方向相反，或无回执就跨题推广 | 00-SUMMARY §4.8、§5.8；R4 §7 C9 |

每张卡正文含「规则 / 可测触发条件 / 反例与失效域 / 来源」四节；播种沿用 `ensure_triad_kb_seeded` 的
`_copy_if_missing`（已存在的条目文件不覆盖，测试 `test_process_cards_seed_idempotently_and_keep_edited_files`
钉住）。

## 附录 E 门禁回复附带的知识条目（范围 4）

### 实现

- 新模块 `cli/opsb_core/knowledge/hints.py`（151 行，未超 350 行上限）：
  - `select_gate_hints(*, problem, subject, note, parhelion_home, limit=2, max_bytes=600)`：确定性匹配与裁剪；
  - `gate_hints(workspace, problem, sha, note)`：读提交标题作为"假设标题"，任何异常都返回 `[]`。
- `cli/opsb_core/mcp/handlers_bench.py`：`tool_parhelion_submit_gate` 末尾 +7 行（局部 import、一次调用、
  非空才挂 `result["knowledge_hints"]`）。没有新的工具、没有新的 schema 参数、没有新的 `GateConfig` 开关。
- 候选集：任何支柱里 `status` 或 `portability` 为 `anti-pattern` 的条目，加上全部 `bugs` 条目。
- 相关性（确定性，不调模型）：
  - 本题同名：`bugs` 条目的 `problem` 与请求的题名逐字相同（大小写不敏感），或 `applicable_problems`
    含该题名；
  - 机制关键词：提交标题 + 提交说明切成 ASCII 词（长度 ≥3、去停用词与纯数字）后，与条目
    id/标题/摘要/lesson/fix/category/problem/tags/bottlenecks 的词集求交，**至少 2 个**才算相关。
- 排序：同题优先 → 关键词命中数降序 → 条目 id；同样的输入永远同样的输出（有测试）。
- 裁剪：每条一行，单行上限 `(600-1)//2 = 299` UTF-8 字节，两行连换行合计 ≤600 字节。
- 格式：`!<id> [bugs|<WA/TLE/RE/score-drop>]: <题名> — <lesson>` 或
  `!<id> [anti-pattern|<category>]: <title> — <summary>`。

### 样例原文与字节数

同一次 handler 调用（预览路径，`problem=TestOp`，提交标题 `protect the noinline spill`，合成 KB），
回复里的 `knowledge_hints`（两行，合计 **278 字节**，回复片段 **306 字节**）：

```json
{"knowledge_hints": ["!BUG-testop-1a2b [bugs|WA]: TestOp · WA — Judge case 15 has a 4-ulp precision miss on the tail tensor.", "!HW-noinline-hot-loop [anti-pattern|hardware-law]: NEVER call noinline inside hot loops — Noinline inside the hot loop spills 12 scalars to HBM and loses the latency win."]}
```

| 行 | 字节 |
|---|---:|
| `!BUG-testop-1a2b [bugs\|WA]: … tail tensor.` | 105 |
| `!HW-noinline-hot-loop [anti-pattern\|hardware-law]: … latency win.` | 172 |
| 两行 + 换行 | 278 |
| 整个 `"knowledge_hints": [...]` 片段 | 306 |

入队路径用同一段代码与同一个 `result` 对象，测试
`test_enqueue_also_carries_the_hints` 在 `enqueue=True` 下断言 `knowledge_hints` 存在且
`enqueued=True / dry_run=False`。

### 「不影响判定」的证据

`test_gate_reply_appends_hints_without_touching_the_verdict` 用同一个 stub bridge 跑两次（一次附带、
一次不附带），断言：

- 15 个判定字段（`stages`、`gates`、`approved`、`badge`、`next_step`、`estimated_score`、`current_score`、
  `delta_expected`、`wspt_priority`、`reasons`、`enqueued`、`dry_run`、`status_tag`、`priority_score`、
  `intent`）完全相同；
- 两次传给 bridge 的参数列表逐字相同（`seen[0] == seen[1]`）；
- 没有匹配时回复里**没有** `knowledge_hints` 键，不是空列表。

### 范围 4 的测试（`cli/tests/test_kb_gate_hints.py`，7 个）

1. 正例：同题 bugs 条目排第一，关键词命中的 anti-pattern 排第二；
2. 反例：无关题名 + 无关说明 → `[]`；只命中 1 个关键词也不算相关；
3. 提交说明与提交标题一样参与匹配；
4. 两行上限与 600 字节上限（长摘要裁剪、单行 ≤299 B、`max_bytes=220` 时同样不超）；
5. 确定性：同输入两次结果相同；
6. 没有 KB 时不抛异常（`gate_hints` 返回 `[]`）；
7. 预览不带/带提示的判定字段与 bridge 参数一致，入队路径同样附带。

## 附录 F Swift 改动与工具表字节

- Swift 改动（阶段 A，阶段 B 未再改）：`Sources/Parhelion/Domain/TriadKBModels.swift`（+44 / −1）与
  `Sources/Parhelion/Features/Breakthroughs/TriadKBDetailView.swift`（+2），共 +46 行，未超 50 行门槛。
  `TriadKBEvidence` 新增 `evidenceClass` / `nDraws` / `sigmaPts` / `repro`（`decodeIfPresent`，缺省
  `local` / `1` / `0` / `""`），`TriadKBEntry` 新增 6 个条目级字段（`decodeIfPresent`，缺省 `no` / `""` /
  `""` / `""` / `[]` / `nil`），详情页 metadata 区显示「证据等级」构成与「机制确认」。
- Swift 构建：`swift build` 通过（`Build complete! (2.25 sec，增量)`）。
- 工具表（`tools/list` UTF-8 字节，未改上限；阶段 B 没有加 schema 字节）：

| 角色 | 4.2.1 基线 | 本包最终 | 上限 |
|---|---:|---:|---:|
| orchestrator | 14601 | 14716 | 15000 |
| worker | 12507 | 12528 | 13000 |

（`integ/base-P23` 的基线是 14560 / 12372；本包的 `evidence_class` 参数带来 +156 B，rebase 后为
14716 / 12528。范围 4 只加回复字段，不加工具表字节。）
