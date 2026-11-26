# P12 用平台视图取代临时脚本（4.2 D）

分支：`impl/P12-report-views`。在 P01 与 P06 合入后的 `main` 上开发。

针对：本场有 621 个临时脚本，其中 388 个读 JSON、165 个直接解析 `report.json` 求逐例时间；约 100 次绕过 MCP 的 CLI 调用（`opsb queue list` 53 次、`opsb history` 约 30 次、`opsb bench verify-escape` 14 次）；`sed` 与 `python3` 的输出合计约 1.5MB。

## 1. 先聚类

用 P03 的 `replay/loader.py` 从本场流水中抽出全部临时 Python 脚本与 CLI 绕行调用，按用途聚类。初步看用途有：逐例对比、两个 SHA 的差异、判题历史、队列、榜单与 tbest。可用读取的文件和打印的字段做简单的确定性聚类。

结果写入 `docs/plan-4.1-4.2/reports/P12-script-clusters.md`。如果出现上面没列出的用途，并且占比 ≥5%，就为它增加视图。

## 2. `parhelion_report`

新模块 `cli/opsb_core/mcp/handlers_report.py`，不超过 350 行，复用现有服务，不重写计算逻辑。新工具：

`parhelion_report(view=per_case|diff|history|queue|board, problem?, sha?, base?, limit?)`

各视图：
- `per_case`：固定列为用例、本方 µs、tbest µs、差距 %、噪声带，另带判题映射标记（P01）与最近一次运行 ID。
  - `sha` 缺省时取最新正式运行；
  - `base` 缺省时取同代基线，即榜上 SHA。
- `diff`：两个 SHA 的逐例比与置信区间。有 P10 的配对运行时优先使用，否则用分别运行的最小值。另附 `git diff --stat`。
- `history`：与 `opsb history` 等价，包括判题历史、逐例比与回执状态，使用 P01 的规范字段。
- `queue`：与 `opsb queue list` 等价，含候选、暂缓、在途与已完成，以及门禁判定（P07）。
- `board`：榜单分数、本方排名、逐例 tbest 及其观测时间、frontier。数据取自平台缓存（`services/board_refresh_service.py` 与 `ranking_service.py` 写入的缓存），不触发远端请求。这是 P15 要给 worker 的数据通道，worker 不需要接触任何凭据。

角色规则：
- worker 只能查自己负责的题；`board` 视图 worker 也可以用。
- 主控可以查全部题。

在 schema、handler 与角色三处同步登记。

### 调研补充

- **R6 `view=bottleneck`**：返回紧凑诊断，包括瓶颈类别、受限资源占峰值的百分比、前 3 个指标值、知识库里的候选修复族，以及原始剖析数据的路径。依据 `docs/v4.2/research/agent-optimization-practice.md` R6。
- **R3 三态判定**：`per_case` 视图对每个用例给出 improved / indistinguishable / regressed 三态，取代原始 µs 差值；判定逻辑见 P17。

## 3. 覆盖核对

- `opsb queue list --json` 与 `opsb history --json` 的每个字段，在对应视图中都能拿到，或者有明确的去处。报告附对照表。
- 工具描述里写明：用 `parhelion_report` 代替读取 `report.json` 或 `outcome.jsonl` 的临时脚本。

## 验收

- 单测：
  - 每个视图使用 fixture 比赛目录，形状裁剪自真实数据并去敏；
  - 固定列；
  - 角色限制；
  - `board` 不触发网络请求，用 mock 断言。
- 对真实比赛只读执行一次 5 个视图，记录输出字节数，写进报告。输出上限由 P13 统一处理，本包先保证 `per_case` ≤4KB。
