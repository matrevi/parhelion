# P28 知识库 v2：证据等级与过程知识

分支：`impl/P28-kb-evidence`。基于 4.2.1 的 `main`。无依赖。

第 4 项要改 `mcp/handlers_bench.py`，与 P23 冲突：先做其余部分，第 4 项放在最后，P23 合入后 rebase 再接。

## 为什么

R4 把上一场的经验文档逐条对照回执，发现现有模板装不下一半的有效经验（R4 §6–§7）：

- **字段太少。** 缺这些：
  - 机制是否被证实；
  - 反例与失效域；
  - 判了几次、噪声多大；
  - 可测的触发条件；
  - 代价与风险；
  - 适用题与复用记录；
  - 复现方法；
  - 作废指针。
- **类目缺计分律与度量律。** 能改变「这一发该不该提」的头部经验，一半是计分律与度量律：latest 归零、同源重投噪声、公开与隐藏剪刀差。12 张卡片草稿里有 9 张只能塞进 community。
- **证据不分等级。** 本地 profiler 得出的「硬件规律」被当作事实广播，两次造成线上大跌（00-SUMMARY §4.8、§5.8）。

R5 的教训 5：知识库的价值不在写而在读，读必须由平台强制。上一场赛后一次性写，次日读取 0 次。Parhelion 自己的冲刺边打边写，却仍重试了 11 次已证伪的方向。

## 范围

### 1. 字段

在 `cli/opsb_core/knowledge/query.py` 的 `_normalize_entry` 与写入路径上增加以下字段，全部可选，旧条目读时补默认值。

- **条目级：**

  | 字段 | 取值或含义 |
  |---|---|
  | `mechanism_confirmed` | `yes`、`no`、`confounded` |
  | `trigger` | 可测的触发条件，文本 |
  | `cost` | 代价与风险，文本 |
  | `applicable_problems[]` | 适用题 |
  | `retracted_by` | 作废指针，指向取代它的条目 id |

- **证据行级：**

  | 字段 | 取值或含义 |
  |---|---|
  | `evidence_class` | `local`、`paired`、`online_receipt`；与 P27 的消息证据等级同一组取值 |
  | `n_draws` | 判了几次 |
  | `sigma_pts` | 噪声大小 |
  | `repro` | 运行 ID 或命令 |

- **反例**：用现有 verdict 为 fail 或 anti-pattern 的证据行表达。条目上另加 `failure_domain` 文本，说明在什么条件下不成立。

### 2. 类目

- 类目枚举增加 `scoring-law`、`measurement`、`process`。
- **不新增支柱。** 支柱是前后端契约（`Domain/TriadKBModels.swift` 的 S），新增要同步 Swift 与 seeding。新类目放在现有支柱里，过程类默认进 community。

### 3. 晋升规则

在 `knowledge/evolution.py` 中：
- **hardware 支柱**：条目只有在至少一条证据为 `paired` 或 `online_receipt` 时才能晋升。只有 `local` 证据的条目保持候选，查询结果里标「待验证」。
- **其他支柱**：维持现有的贝叶斯门控。
- **迁移**：迁移必须幂等。已晋升的旧条目不降级，只在查询结果里标出证据等级的构成。

### 4. 平台强制的读取

- `parhelion_submit_gate` 的回复里附上至多 2 条与本候选相关的 anti-pattern 或 bugs 条目，每条一行，合计不超过 600 字节。
- 「相关」怎么判：本题的同名条目，或与假设标题、提交说明的机制关键词重合的条目。
- 用确定性的关键词匹配，不调模型。
- 预览与入队都附带。附带只是提示，不影响门禁判定。

### 5. 过程卡

- 在 `templates/community/entries/` 增加 4–6 张与设备无关的过程卡，并登记进 `templates/community/index.json`。
- 候选题目：
  - latest 榜型下，提交是状态变更，先备好 undo；
  - 判题噪声：σ̂ 以内的差不算突破，同源重投的期望略负；
  - 归零后先 restore 再实验；
  - 判题时限：编译加评测共享一个总时限，要看实例化数与编译量；
  - 先修正确性再求快；
  - 本地 profiler 的结论要先经配对或线上验证，再推广。
- 每张卡写清楚：规则、可测触发条件、反例或失效域，以及来源（指向 `docs/v4.3/research/antigravity/` 的章节）。
- **不写比赛分数，不写设备型号，也不写厂商专有术语。**
- 播种沿用 `knowledge/seeding.py` 的非破坏式规则：已存在的条目文件不覆盖。

### 6. Swift

`TriadKBModels` 用 decodeIfPresent 解码新字段，详情页显示证据等级的构成与 `mechanism_confirmed`。改动超过 50 行就只做后端，列为后续。

## 复用

- `knowledge/evolution.py`：`compute_confidence`、`validate_evidence`、`_meets_promote`；
- `knowledge/query.py`：`format_prompt_cache_digest`，以及 kb_query 的 brief 格式（每条 600 字符）；
- `knowledge/migration.py` 的幂等做法。

## 测试

- 新字段的规范化与默认值；
- 晋升规则在只有 local 证据时被拒；
- 迁移幂等；
- 门禁回复中附带条目的相关性与字节上限；
- 过程卡播种幂等，且不覆盖已有文件；
- 过程卡里不出现设备值与厂商术语：用一个词表测试兜底，词表放在测试里。

## 验收

- 用 `parhelion_kb_query` 在当前工作区查询，把带证据等级的 brief 输出样例写进报告。
- 统计现有条目的证据等级构成，以及迁移后有几条被标为「待验证」。

## 真机

不需要。

## 报告（`docs/v4.3/reports/P28-kb-evidence.md`）

- 字段与类目表；
- 晋升规则的改动；
- 现有条目的证据构成；
- 过程卡清单；
- 门禁回复附带的样例与字节数；
- Swift 是否改动。
