# P03 基线：缓存命中、工具字节、冲刺口径与压缩回放（4.2 A）

数据：2026-11-26 冲刺的 13 个新 worker（04:15:17–20 UTC 开出的 `mesh-dee-*.jsonl`）、它们在 `~/.dsh/sessions/` 下的 DSH 原生会话、UI 主控的 DSH 会话（契约根目录下 `session-7c5f9c04…`）、本场的总线库、测试登记表、门禁日志和规范回执。所有来源只读打开；本报告只含计数与汇总，不含消息原文。

窗口有两个：

- **全量**：04:15:18–05:41:17 UTC（1.43 小时，直到 worker 被停下）。
- **截至 05:25**：冲刺停止的时刻（`--until 2026-11-26T05:25`，1.16 小时）。§1 的数字看起来是在这附近截取的。

命令（只读日志，全部数字可复现）：

```bash
S=mesh-dee-07013f.jsonl,mesh-dee-0f97d5.jsonl,mesh-dee-17d6d3.jsonl,mesh-dee-2180de.jsonl,mesh-dee-345d84.jsonl,mesh-dee-43695a.jsonl,mesh-dee-754bf6.jsonl,mesh-dee-7be6aa.jsonl,mesh-dee-a07dea.jsonl,mesh-dee-abcefb.jsonl,mesh-dee-b0c078.jsonl,mesh-dee-ba3f0e.jsonl,mesh-dee-cf7eae.jsonl
opsb replay metrics --sessions "$S" --workspace ~/Competitions/mat-chem-sim-pred [--until 2026-11-26T05:25] --json
opsb replay bench   --sessions "$S" --schemes current,current_plus_ledger,full_history [--until 2026-11-26T05:25] --json
```

`replay metrics` 用时约 3.5 秒，`replay bench` 约 1.2 秒。

## 1. 结论

- **提示缓存命中率早已达标，成本的杠杆是上下文长度。** 13 个 worker 的命中率在 99.3%–99.6% 之间，主控为 99.5%，4.2 的目标是 ≥ 80%。全量窗口里 worker 共 4,241 次模型调用：缓存读取 7.35 亿 token，未命中输入 407 万，输出 464 万。每次调用都会把整段上下文从缓存里重读一遍，所以 token 总量约等于上下文长度乘以调用次数。压低 349K 的上下文峰值（C、H）才能省下 token，提高命中率省不了多少。
- **未命中只出现在可解释的边界上。** worker 共 32 次未命中：13 次 `cold_start`，19 次 `compaction_or_truncation`（每次平台压缩后的新会话各一次）。主控有 2 次：`cold_start`，以及 04:28 的一次 `system_changed`（DSH 用 `replace` 改写了系统消息，丢掉 178,682 token 的前缀）。主控会话的工具表在 04:15:08 变过一次（`request/header reason=change`，平台插件已挂载），但下一次调用的前缀照样命中，所以不算未命中。
- **现方案的种子已经写进了 SHA 和测试结果，重复劳动照样发生。** 回放基准中，现方案对 `retest` 探针的覆盖率是 100%，对 `refuted_retry` 是 91%，但切点之后仍有 11 次重测（其中 9 次是 standard 级别），已证伪的方向也被重试了 11 次。可见只靠种子里有信息挡不住重复，C 里的「由账本拦截」需要平台强制执行。种子漏得最多的是文件（44%）和想法（79%）。
- **账本摘要只多覆盖 1.4 个百分点**：0.642 [0.568, 0.716] → 0.655 [0.581, 0.723]。按 P03 口径，`current_plus_ledger` 只用测试、门禁、提交、回执、知识记录这些平台事件来重建，而这些事件的 SHA 和结论 handoff 本来就写了，文件名和多数想法却不在平台事件里。P06 的 `full_c` 方案需要补上「读过哪些文件、得出什么结论」这一类条目。
- **§1 的数字大体能复现。** 上下文峰值完全一致（中位 349,306，最高 422,444），重测次数也完全一致（11 次）。主控轮询对不上：日志里只有 195 次，§1 写的是约 760 次。起步上下文的定义也不同，见 §3。

## 2. 冲刺口径（§1、§3 同口径）

| 指标 | §1 | 全量窗口 | 截至 05:25 | 说明 |
|---|---|---|---|---|
| worker 上下文峰值 | 中位 349K，最高 422K；压缩 17 次 | 中位 349,306，最高 422,444；压缩 19 次 | 同左；压缩 18 次 | 每个 worker 日志中 `usage_update.used` 的最大值；压缩次数按 handoff 轮次计 |
| 起步到第一次平台调用的上下文 | 中位约 10K，最高 44K | 中位 30,714，最高 50,572 | 同左 | 第一次调用 `parhelion_*` 时（或之前）最后一个 `used` 读数。各 worker 的首个读数都约为 10K（系统提示 + 工具表 + brief）。§1 的「中位 10K」应当是这个首读数；RUN-LOG 写的「21–44K」与本口径接近 |
| 以 `cd` 开头的 shell | 1,766 次，4.4MB | 1,848 次，4.30MB（1,290/h） | 1,824 次，4.24MB（1,571/h） | 按 `cd` 链之后的第一个程序分组，见下表 |
| 临时 Python 脚本 | 621；读 JSON 388；解析 report.json 165 | 672；461；179（469/h） | 662；452；175（570/h） | 计 `python -c` 和 heredoc；脚本里出现 `json` 算读 JSON，出现 `report.json` 算解析 report.json |
| 绕过 MCP 的 CLI | queue list 53，history 约 30，verify-escape 14 | 68，45，16 | 68，45，16 | shell 命令里出现 `opsb queue list` 等 |
| 主控轮询 | agent_wait 约 250，test_wait 约 200，agent_send 约 200，status 约 110 | 81，15，59，40（共 195，136/h） | 同左（168/h） | 只统计 cwd 为契约根目录的 DSH 会话。worker 自己调用 test_wait 294 次、status 97 次（见 §4），不计入主控 |
| 总线消息 | result 317 条 723KB，peer 198 条 371KB，单条最长 7KB | result 368 条 869KB，peer 219 条 422KB，最长 7,046B | result 349 条 832KB，peer 212 条 408KB | 总线库只读，按 `created_at` 落在窗口内的消息计 |
| 压缩后重读压缩前的文件 | 1,036 次读中 170 次（16%） | 939 次读中 184 次（19.6%）；切点后的读取中占 55.8% | 931 中 178（19.1%）；55.3% | 读 = `read` 工具，或 shell 中 `cat`/`sed`/`head`/`tail`/`nl`/`grep` 段落里点到的文件 |
| 压缩后重测已测的 SHA 与级别 | 224 次测试中 11 次，没有一次复用已有运行 | 235 次中 11 次；其中 2 次是 reused/joined 的已有运行 | 220 中 9 | 切点后的 `parhelion_test`，其 `(sha7, level)` 在切点前请求过 |
| 已证伪方向被重试 | 未统计 | 回放探针 `refuted_retry` 11 条，涉及 7 个 worker（证伪原因：门禁低增益 6、本地 WA 3、变慢 2） | 10 条 | 见 §5 |
| DeepSeek 提示缓存命中率 | 未统计 | worker 99.3–99.6%，主控 99.5% | 同左 | `cache_read / (cache_read + input_miss + cache_write)`，DSH 的 `cacheWriteTokens` 恒为 0 |

`cd` 开头的 shell 按程序分列（全量窗口）：

| 程序 | 次数 | 返回字节 | §1 |
|---|---:|---:|---:|
| sed | 209 | 932,797 | 954KB |
| git | 414 | 855,624 | 857KB |
| python3 | 360 | 728,414 | 594KB |
| grep | 291 | 489,592 | 501KB |
| ls | 146 | 364,702 | 318KB |
| cat | 52 | 272,269 | 288KB |
| 其他（opsb、echo、for、find…） | 376 | 652,207 | — |

与 §1 的差异：多数项目比 §1 高 4%–20%，与窗口截取时刻不同相符（日志一直写到 05:41，§1 的总线条数落在本表两列之间）。python3 一栏偏高，是因为这里把 `python` 也并入了 `python3`，`timeout`、`env` 这类前缀后面跟的程序也一并识别了。主控轮询无论哪个窗口都只有 195 次，任何一份日志都凑不出 §1 的数字。最接近的组合是主控加 worker（test_wait 309、status 137），但 agent_send 和 agent_wait 只有主控会调用。§3 的「每小时约 560 次轮询」这一基线需要按本口径改为约 136–168 次每小时。

## 3. 各驱动能判定到哪一级

| 驱动 | 用量来源 | 能判定的未命中原因 |
|---|---|---|
| DSH（deepseek） | 原生会话 `assistant/message.usage`；由 mesh 日志里 `session/new` 结果中的 `sessionId` 和 cwd 定位 | 六种都能判定：`system/message`（按 seq 跟踪，支持 `replace` 区间）、`request/header` 的工具表（按名称排序归一）、非 append 的 `surfaceOp`（声明的改写）、新会话、时间间隔、其余为 `unknown` |
| Codex app-server | `thread/tokenUsage/updated` 的 `last`；`inputTokens` 已包含 `cachedInputTokens`（用本机 Codex rollout 核实：`input + output = total`，`cached ≤ input`） | `cold_start`、压缩（`thread/compact/start`、`thread/compacted`、`contextCompaction` 条目）、`ttl_gap`、`unknown`。日志里没有系统提示和工具表 |
| Claude stream-json | `assistant.message.usage`，同一 message id 只记一次（输出取最大值） | `cold_start`、`tools_changed`（只有 init 里的工具名，没有 schema）、压缩（`compact_boundary`）、`unknown`。stream-json 通常不带时间戳，所以判不出 `ttl_gap` |
| Antigravity ACP、gemini、pi | ACP 的 `usage_update` 只有 `used`/`size` | 标为 `unavailable` |

未命中事件的判据：本次调用的 `cache_read` 比上一次调用的提示长度少 `max(256, 0.5% × 上次提示)` 以上，或是该 agent 的第一次调用。实测中，13 个 worker 在同一会话内的 `cache_read − 上次提示` 落在 −163 到 +23,766 token 之间（中位 +492，DeepSeek 会连上一轮输出一起缓存），所以 256 的余量不会误报。`ttl_gap` 的阈值按驱动取：deepseek/codex 3,600 秒，claude/antigravity 300 秒，可传参覆盖。本场 worker 两次调用的间隔中位 4.6 秒，p99 78 秒，最长 50 分钟，没有出现 `ttl_gap`。

本场只有 DSH 日志，Codex 和 Claude 的解析器只在合成 fixture 上验证过。

## 4. MCP 工具字节（日志基线；实时计量从本包开始写 `metrics/mcp-calls.jsonl`）

全量窗口，13 个 worker，按返回字节排序：

| 工具 | 调用 | 每小时 | 返回字节合计 | 平均 | p95 |
|---|---:|---:|---:|---:|---:|
| bash | 2,684 | 1,873 | 5,930,164 | 2,210 | 7,399 |
| read | 265 | 185 | 1,572,293 | 5,933 | 16,417 |
| parhelion_agent_inbox | 64 | 45 | 424,244 | 6,629 | 26,608 |
| parhelion_test_wait | 294 | 205 | 404,892 | 1,377 | 2,286 |
| parhelion_submit_gate | 82 | 57 | 278,976 | 3,402 | 3,945 |
| parhelion_kb_query | 29 | 20 | 253,676 | 8,747 | 14,961 |
| parhelion_status | 97 | 68 | 213,227 | 2,198 | 7,632 |
| parhelion_kb_record | 27 | 19 | 93,690 | 3,470 | 6,427 |
| parhelion_test | 235 | 164 | 72,181 | 307 | 264 |
| parhelion_bench_diagnose | 32 | 22 | 45,744 | 1,430 | 3,408 |
| parhelion_squad | 234 | 163 | 19,893 | 85 | 258 |

`agent_inbox` 的 p95 达到 26KB，`kb_query` 平均 8.7KB，是 E（统一输出上限）最该先收的两项。实时计量写入工作区状态目录下的 `metrics/mcp-calls.jsonl`，`opsb replay metrics` 读到它时会附上按工具、按小时的汇总（次数、错误、平均与 p95 返回字节）。守护进程重启后，计量才会生效。

## 5. 压缩回放基准

19 个切点（13 个 worker 都有），每个切点都有日志里的原始种子（`Preserve this checkpoint …`）。共 148 个探针：`file_reread` 70、`idea_repeat` 61、`refuted_retry` 11、`retest` 6（按 `(sha7, level)` 去重，所以比 §2 的 11 次少）。

| 方案 | 覆盖率 | 95% CI | file_reread | idea_repeat | refuted_retry | retest | 种子长度中位（字符） |
|---|---:|---|---:|---:|---:|---:|---:|
| current | 0.642（95/148） | [0.568, 0.716] | 0.443 | 0.787 | 0.909 | 1.000 | 6,794 |
| current_plus_ledger | 0.655（97/148） | [0.581, 0.723] | 0.443 | 0.803 | 1.000 | 1.000 | 8,460 |
| full_history | 1.000 | [1.000, 1.000] | 1.000 | 1.000 | 1.000 | 1.000 | 1,118,495 |

截至 05:25 的窗口：18 个切点，140 个探针。current 0.636 [0.557, 0.714]，current_plus_ledger 0.650 [0.571, 0.729]。

按 worker 分列（探针数、两种方案的覆盖率、current 的 CI）：

| worker | 探针 | current | +ledger | current CI |
|---|---:|---:|---:|---|
| dee-07013f | 6 | 0.833 | 0.833 | [0.50, 1.00] |
| dee-0f97d5 | 15 | 0.867 | 0.867 | [0.67, 1.00] |
| dee-17d6d3 | 13 | 0.385 | 0.385 | [0.08, 0.69] |
| dee-2180de | 21 | 0.429 | 0.429 | [0.19, 0.62] |
| dee-345d84 | 13 | 0.538 | 0.538 | [0.31, 0.77] |
| dee-43695a | 8 | 0.625 | 0.750 | [0.25, 0.88] |
| dee-754bf6 | 6 | 0.833 | 0.833 | [0.50, 1.00] |
| dee-7be6aa | 11 | 0.909 | 0.909 | [0.73, 1.00] |
| dee-a07dea | 2 | 1.000 | 1.000 | [1.00, 1.00] |
| dee-abcefb | 20 | 0.650 | 0.700 | [0.45, 0.85] |
| dee-b0c078 | 9 | 0.778 | 0.778 | [0.44, 1.00] |
| dee-ba3f0e | 14 | 0.500 | 0.500 | [0.21, 0.71] |
| dee-cf7eae | 10 | 0.700 | 0.700 | [0.40, 1.00] |

探针与评分口径（确定性，不调用模型）：

- `idea_repeat`：切点后的想法（commit 信息、门禁 note、report 摘要、知识标题、todo、回复中的条目行）与切点前的某个想法共享至少 3 个信息词，且重叠系数 ≥ 0.6。在该 worker 超过四分之一的想法里都出现的词视为泛词，不计入。要点取最罕见的 3 个共享词，种子里出现其中 2 个即算覆盖。
- `refuted_retry`：切点前被证伪的 SHA（本地 WA、比基线慢 2% 以上、门禁 HOLD_LOW_GAIN 或 REJECTED_GATE、线上非 Pass），切点后又按相同方向的想法重试或重测了同一 SHA。只有种子里同一行同时出现该 SHA 和一个失败词（WA、slower、held、regress…）才算覆盖。
- `file_reread`：切点后读到的、切点前读过的文件。要点是文件名。
- `retest`：要点是 sha7，同一行还必须出现级别、`n/m` 或 pass/geomean。
- 每个 worker 都给 bootstrap 95% CI：1,000 次重采样探针，固定种子 20261126。可选的 LLM 评审默认关闭，开启会直接报错，不调用任何 API。
- P06 用 `seeds.register("full_c", builder)` 接入新方案，`builder(CutContext) -> str`。

## 6. 成本效率（调研 R9）

全量窗口。token 取自 DSH 用量（所有提示 token 加输出；「未缓存」只算未命中输入加输出），设备秒取自测试登记表中各运行 `report.json` 的 `device_batch.hold_sec`。保留改进 = 已批准、未被强制或覆盖的门禁判定，按 `(题, sha7)` 去重。官方增益 = 该回执分数高于该题此前最高官方分的部分（榜面增益，未做 tbest 同代修正）。归属：先看谁最先测过该 SHA，再看谁为它请求过门禁。

| 题目 | token（亿） | 未缓存 token | 设备秒 | 保留改进 | 判题 | 官方增益 | 每个保留改进的未缓存 token | 每个保留改进的设备秒 | 每分增益的未缓存 token | 每分增益的设备秒 | 每次判题增益 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| EdgeIndexCoalesce | 1.89 | 2,139,031 | 299.0 | 1 | 7 | 7.49 | 2,139,031 | 299.0 | 285,585 | 39.9 | 1.07 |
| FFT3DBatchComplex | 1.78 | 1,917,698 | 812.4 | 1 | 1 | 2.22 | 1,917,698 | 812.4 | 863,828 | 365.9 | 2.22 |
| SgemmFp16Cube | 1.33 | 1,888,864 | 331.9 | 2 | 2 | 1.65 | 944,432 | 166.0 | 1,144,766 | 201.2 | 0.83 |
| CGTensorProduct | 0.80 | 847,456 | 354.7 | 2 | 3 | 0.56 | 423,728 | 177.3 | 1,513,314 | 633.3 | 0.19 |
| QuantScaleCalib | 1.63 | 1,915,940 | 111.0 | 3 | 3 | 0.00 | 638,647 | 37.0 | — | — | 0.00 |
| 合计（13 个 worker 及窗口内其余判题） | 7.43 | 8,708,989 | 1,924.4 | 12 | 19 | 12.73 | 725,749 | 160.4 | 684,131 | 151.2 | 0.67 |

合计行另含 2 次无法归属的判题（RadialBasisBatch、MeshEdgeMessagePassing，由窗口外的 agent 提交），以及仍在运行的 NeighborList worker（dee-663447，不在 13 个之列）的 4 次测试。按 worker 的明细在 `--json` 输出的 `cost.per_worker` 中。

## 7. 已知局限

- 读文件只按命令文本识别，不看工作目录里文件实际是否存在；`grep -r` 把目录也算作一次读取。
- 想法匹配基于词袋。中文想法也能分词，但没有专门校准，本场的想法基本是英文。
- `current_plus_ledger` 的「判题」行来自切点前收到的提示中含判题结果的首行，没有直接查规范回执；回执与切点时间的对齐留给 P05 的账本。
- 冲刺数据里没有 Codex 和 Claude 的流水；Antigravity 判不出用量。
