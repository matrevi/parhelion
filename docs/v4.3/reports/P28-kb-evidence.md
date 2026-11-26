状态：阶段 A 完成，待 P23 合入后接第 4 项

# P28 知识库 v2：证据等级与过程知识（阶段 A）

分支 `impl/P28-kb-evidence`，工作树 `/Users/winbeau/Developer/personal/parhelion-wt/P28`。
阶段 A 覆盖子计划范围 1、2、3、5、6；范围 4（`parhelion_submit_gate` 回复附带相关知识条目）改
`mcp/handlers_bench.py`，与 P23 冲突，按子计划放在 P23 合入后 rebase 再做。

## 1. 分支与提交

```text
$ git log --oneline main..HEAD
ff00308 Add KB evidence levels, mechanism fields and process cards
```

本报告单独提交，标题为 `Document the P28 stage A knowledge evidence work`（阶段 A 不使用
`Deliver P28:` 前缀）。

## 2. 子计划逐项完成情况

| # | 范围 | 状态 | 说明 |
|---|---|---|---|
| 1 | 字段：条目级 + 证据行级 | 已完成 | 8 个新字段全部可选，旧条目读时补默认；写入路径校验；MCP/CLI 可声明证据等级 |
| 2 | 类目：`scoring-law`、`measurement`、`process` | 已完成 | 只进现有支柱，未新增支柱；过程卡默认进 community |
| 3 | 晋升规则 + 幂等迁移 | 已完成 | hardware 必须有 `paired` 或 `online_receipt`；其他支柱维持贝叶斯门控；迁移只补证据等级、不改状态 |
| 4 | 平台强制的读取（门禁回复附带条目） | 未做 | 阶段 B：等 P23 合入后 `git rebase main`，再改 `mcp/handlers_bench.py` |
| 5 | 过程卡（4–6 张，与设备无关） | 已完成 | 6 张，登记进 `templates/community/index.json`；播种非破坏式；词表测试兜底 |
| 6 | Swift 解码与详情页 | 已完成 | 2 个文件 +46 行（未超 50 行门槛）；`swift build` 通过 |

## 3. 两条全量测试末行与 Swift 构建

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
812 passed, 19 skipped, 2 subtests passed in 73.59s (0:01:13)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
551 passed in 59.70s
```

基线是 4.2.1 的 801 passed / 19 skipped 与 540 passed；新增 11 个测试（`cli/tests/test_kb_evidence.py`）。
两条全量都跑到底，`runtime/tests/test_device_broker.py` 的两条预算测试本轮没有偶发失败。
`swift build`：`Build complete! (0.39 sec)`（改动后首次全量编译 `Build complete! (10.77 sec)`）。

## 4. 真实数据验证（全部在副本上）

真实 KB 只读复制到 `/tmp/p28kb`（`hardware` / `community` / `snippets` / `breakthrough` / `bugs` 五个目录），
所有查询与迁移都在这份副本上执行，真实 `~/.parhelion` 未写。

- 条目 202 条、证据行 237 条。
- 迁移：`backfilled=196`、`entries_touched=157`、`hardware_pending_validation=39`、
  `hardware_promoted_kept=15`；第二遍 `backfilled=0, entries_touched=0`（幂等）。
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

1. **`mechanism_confirmed` 缺省是 `no`，语义是"未被证实"，不是"被证伪"。** 反例仍由 `verdict` 为
   `fail` / `anti-pattern` 的证据行表达，条目上另加 `failure_domain`。
2. **旧证据行的缺省是 `local` / `n_draws=1` / `sigma_pts=0.0` / `repro=""`。** 读取时不做任何文本推断
   （不按 note 里的 "online" 猜等级），所以现有 108 条硬件证据行全部是 `local`。这是保守选择：宁可把
   旧条目当"待验证"，也不把没有声明过的证据升格。
3. **breakthrough 支柱的合成证据类是 `online_receipt`。** `_normalize_entry` 对"有 sha、无 evidence 行"
   的突破条目会合成一条证据；那条本来就是在线上判出来的分，因此标 `online_receipt`（磁盘数据不变）。
4. **派生字段不落盘。** `record_kb_feedback()` 会把规范化后的条目写回 `index.json`；新增的
   `evidence_composition`、`evidence_strong`、`pending_validation`、`retracted` 加进
   `DERIVED_ENTRY_KEYS`，只用于读取，避免索引里出现会过期的快照。
5. **类目枚举在 `cli/opsb_core/knowledge/query.py:27` 的 `VALID_CATEGORIES`，含 `hardware-law`**
   （硬件支柱的默认类目）。写入路径现在会拒绝枚举外的类目；读路径不纠正旧值（数据只读）。
6. **证据等级词表在 `cli/opsb_core/knowledge/query.py:19` 的 `EVIDENCE_CLASSES`**
   （`("local", "paired", "online_receipt")`），强证据集合是第 20 行的 `STRONG_EVIDENCE_CLASSES`。
   这两个常量从 `opsb_core.knowledge` 与 `opsb_core.services.triad_kb_service` 两个门面再导出，P27 直接
   import 即可，不要重写拼写。
7. **MCP 加了 1 个参数，没有新工具。** `parhelion_kb_record` 与 `parhelion_kb_feedback` 各加一个
   `evidence_class`（enum，三处登记：`mcp/schemas.py` 的 schema、`mcp/handlers_kb.py` 的 handler、
   角色沿用 `mcp/tools.py` 的默认 roles）。工具表：主控 14601 → **14757 B**（+156，余量 243），
   worker 12507 → **12663 B**（+156，余量 337），上限未改。
8. **改了一处既有测试。** `cli/tests/test_v310_four_pillar_kb_and_guards.py` 原来断言"硬件条目 win +
   delta_raw 即晋升"，这正是 P28 第 3 条要废掉的旧规则；现在该用例显式给 `evidence_class="online_receipt"`。
9. **迁移不自动执行。** 查询与播种路径都不写盘（读不该触发迁移），迁移由
   `opsb kb migrate-evidence [--dry-run]` 显式调用，函数在
   `cli/opsb_core/knowledge/migration.py:271` 的 `migrate_evidence_classes()`。
10. **现有真实 KB 里已经有设备型号写进条目 id/摘要**（例如 `HW-<chip>-…`）。本包按"数据只读"没有修改
   它们，新写的过程卡、代码、测试与报告里没有设备值。发布时是否清洗由主会话决定（见「待决」）。
11. **过程卡的 `status` 是 `unverified`。** 它们没有 sha 证据行，写 `verified` 会绕过第 3 条的证据门；
    查询里它们的 `confidence=0.5`，排在已验证条目之后。
12. **Digest 只对 hardware 条目附加证据构成**（`|ev=local:2`、`|ev=local:1 待验证`），其他支柱不加，
    免得每条查询都多付上下文；完整条目（`format=full`）里所有条目都有构成字段。
13. **阶段 A 不碰门禁**：`scheduler/gate.py`、`estimator.py`、`manager.py`、`handlers_bench.py` 均为零改动，
    因此不需要新增 `GateConfig` 开关；阶段 B 的附带提示只改回复内容，不改判定。

## 6. 风险与后续

1. **工具表余量只剩 243 / 337 B。** P27 还要给广播消息加证据等级参数；如果它也需要注册 schema，很可能
   撞线，需要主会话在合入顺序上裁决（或让 P27 复用 `parhelion_squad` 的现有参数）。
2. **`paired` 证据目前没有产生器。** 本包只定义词表、门与展示；真正产出 `paired` 证据要靠 P27 的广播
   证据等级与 P23 的配对测量/回执。没有产生器时，硬件规律会停在候选。
3. **worker 契约还没教怎么声明证据等级。** 如果下一场 worker 仍按老习惯经 MCP 写硬件条目（不带
   `evidence_class`），新规则会把它们全部留在"待验证"。建议发布时在 worker 提示词或 P27 的 brief 里写
   一句：线上回执用 `online_receipt`，同批用例配对差用 `paired`，本地 profiler 只算 `local`。
4. **旧数据全部是 `local`。** 现有 39 条未晋升硬件条目会立刻显示「待验证」，15 条已晋升条目保持
   `verified` 但构成显示为 `local: n`。这是子计划第 3 条要求的"不降级"，不是 bug；如果主会话希望有
   一次性回填（例如按 note 里的 "online" 标成 `online_receipt`），需要在发布前单独决定。
5. **过程卡是软知识。** 六张卡没有 sha 证据行，`status=unverified`，在查询里不会盖过已验证条目；它们
   的价值在于被 brief/门禁回复读到（阶段 B 的附带条目只取 anti-pattern / bugs，不取过程卡）。
6. **阶段 B 的附带提示要重新测字节。** 600 字节上限与"每行一条"的格式在 `handlers_bench.py` 里实现时，
   要确认预览与入队两条路径都附带，且不影响 `approved` 判定。

## 7. 待决

1. 工具表余量（主控 243 B / worker 337 B）是否够 P27 再加消息证据等级参数；不够时由谁让位。
2. 过程卡是否该给单独的排序权重或 `status=verified`（当前 `unverified`，因为无 sha 证据行）。
3. 真实 KB 里已有的设备型号 id/摘要是否在 4.3.0 发布时统一清洗（本包按数据只读未动）。
4. 是否要一次性把旧证据行的 `online` 类回填成 `online_receipt`（当前一律 `local`，保守但信息量为零）。
5. 阶段 B 的"附带条目"要不要同时进 worker 契约/提示词，而不只是门禁回复。

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
- breakthrough 的 41 条 `online_receipt` 是读取时合成的证据行（见 NOTE 3），磁盘上仍没有 `evidence` 字段。

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

## 附录 E 门禁回复附带条目（阶段 B，未做）

范围 4 未实现，本阶段没有样例与字节数可报。阶段 B 的验收点（来自子计划）：

- `parhelion_submit_gate` 的回复附至多 2 条与本候选相关的 `anti-pattern` 或 `bugs` 条目，每条一行，
  合计 ≤ 600 字节；
- 「相关」= 本题同名条目，或与假设标题/提交说明的机制关键词重合，确定性关键词匹配、不调模型；
- 预览与入队都附带；附带只是提示，不影响 `approved` 等判定；
- 改 `mcp/handlers_bench.py`，P23 合入后 rebase 再接。

## 附录 F Swift 与工具表字节

- Swift 改动：`Sources/Parhelion/Domain/TriadKBModels.swift`（+44 / −1）与
  `Sources/Parhelion/Features/Breakthroughs/TriadKBDetailView.swift`（+2），共 +46 行，未超 50 行门槛。
  `TriadKBEvidence` 新增 `evidenceClass` / `nDraws` / `sigmaPts` / `repro`（`decodeIfPresent`，缺省
  `local` / `1` / `0` / `""`），`TriadKBEntry` 新增 6 个条目级字段（`decodeIfPresent`，缺省 `no` / `""` /
  `""` / `""` / `[]` / `nil`），详情页 metadata 区显示「证据等级」构成与「机制确认」。
- Swift 构建：`swift build` 通过（`Build complete! (0.39 sec)`）。
- 工具表（`tools/list` UTF-8 字节，未改上限）：

| 角色 | 改前 | 改后 | 上限 |
|---|---:|---:|---:|
| orchestrator | 14601 | 14757 | 15000 |
| worker | 12507 | 12663 | 13000 |

改动只来自两个 KB 工具各 1 个 `evidence_class` 参数；没有新增工具，`len(tools)` 仍是 29 / 24。
