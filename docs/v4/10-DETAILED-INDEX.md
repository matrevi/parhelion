# Parhelion 4.0 详细设计索引

> 上游：[00-OVERVIEW.md](00-OVERVIEW.md) · 集成分支：`release/4.0` · 目标版本：4.0.0

## 1. 子文档

| 编号 | 文档 | 工作包 | 一句话 |
|---|---|---|---|
| 11 | [11-mcp-async-tests.md](11-mcp-async-tests.md) | A | MCP 多线程分发、异步测试登记表、工具输出上限 |
| 12 | [12-unified-suite.md](12-unified-suite.md) | B1 | 统一套件、三个级别、热态计时报告与摘要 |
| 13 | [13-device-broker.md](13-device-broker.md) | B2 | 设备主机常驻代理、warmup 计时、TLE 续排 |
| 14 | [14-orchestration.md](14-orchestration.md) | C | 默认单 worker、供应商熔断、token 压缩、状态卡、squad 寻址 |
| 15 | [15-hygiene.md](15-hygiene.md) | D | 测试隔离、网关日志节流、清理命令 |
| 16 | [16-e2e-release.md](16-e2e-release.md) | E | 集成顺序、端到端测试脚本、4.0 发布清单 |

## 2. 文件归属矩阵

每个文件只归一个工作包修改。表里没列的文件不得改动；确实需要改时，在汇报里说明原因，由集成方处理。

| 文件 | 归属 |
|---|---|
| `cli/opsb_core/mcp/server.py`、`tools.py`、`schemas.py`、`handlers_bench.py`、`handlers_kb.py` | A |
| `cli/opsb_core/services/test_runs.py`（新建） | A |
| `cli/tests/test_mcp_server.py`、`cli/tests/test_test_runs.py`（新建） | A |
| `scheduler/case_bench.py`、`scheduler/suite.py`（新建） | B1 |
| `scheduler/cli_bridge.py`（只改 `schedule-test` 参数和转发部分） | B1 |
| `cli/opsb_core/commands/queue_cmd.py`（`run_test`）、`cli/opsb_core/cli_app.py`（只改 `test` 子命令） | B1 |
| `scheduler/tests/test_case_bench.py`、`scheduler/tests/test_suite.py`（新建） | B1 |
| `runtime/npu_runtime/device_broker.py`（新建）、`case_batch.py`、`lanes.py` | B2 |
| `runtime/tests/test_device_broker.py`（新建，目录也新建） | B2 |
| `cli/opsb_core/agents/*`、`cli/opsb_core/mcp/handlers_agents.py`、`cli/opsb_core/drivers/brief.py`、`cli/opsb_core/services/templates_md.py`、`cli/opsb_core/services/state_card.py`（新建） | C |
| `cli/opsb_core/daemon/server.py`（只增加一次 `deliver_test_results` 调用） | C |
| `cli/tests/test_agent_mesh_mcp.py`、`cli/tests/test_orchestration_v4.py`（新建） | C |
| `conftest.py`、`scheduler/gate.py`（只改 `evaluate` 的日志）、`scheduler/feedback_log.py`、`cli/opsb_core/commands/doctor_cmd.py`（新建） | D |
| `scheduler/tests/test_feedback_log.py`、`scheduler/tests/test_gate_log_verdicts.py`、`cli/tests/test_doctor.py`（新建） | D |
| `cli/opsb_core/__init__.py`、`scripts/bundle_app.sh`、`README.md`、`AGENTS.md`、`docs/v4/RELEASE-NOTES.md`、`cli_app.py` 中 doctor 的注册行 | E（集成方） |

## 3. 跨包接口契约

下面这些契约是各工作包之间唯一的耦合点。实现时必须逐字遵守字段名。

### 3.1 `opsb test` 命令行（B1 提供，A 调用）

```
opsb test <Problem> --workspace <ws> [--sha <sha>] [--level smoke|standard|final]
          [--case <id> ...] [--remeasure] [--build-only] [--dry-run]
```

- `--level` 默认 `standard`。旧参数 `--suite adaptive` 等价于 `--level smoke`，`--suite all` 等价于 `--level standard`。
- 环境变量 `PARHELION_MCP_ROLE=worker` 表示请求来自 worker。这时传入 `--profile` / `--experiment` / `--timing` / `--repeats` / `--runner` / `--bucket` 中任意一个，都以退出码 2 拒绝，并在 stderr 写一行原因。
- stdout 协议：
  1. 开始执行时打印一行 `OPSB_RUN_STARTED {"run_id": "...", "dir": "<bench-runs/run_id 的绝对路径>", "level": "...", "selected": <int>}`。复用或加入已有运行时，`run_id` 指向被复用的运行。
  2. 中间日志行不限格式。
  3. 最后一行是一个 JSON 对象，至少包含 `run_id`、`exit_code`、`level`、`formal`、`summary`、`digest`、`report`（report.json 的绝对路径），按情况包含 `reused`、`joined`、`deferred`、`reason`。
- 进程退出码：0 表示通过；2 表示已构建但有用例失败；1 表示构建或环境失败；75 表示被延后（deferred）。

### 3.2 `digest` 结构（B1 产出，A/C 消费）

只有一层嵌套，序列化后不超过 2KB：

```json
{
  "level": "standard", "suite_version": 3, "formal": true,
  "summary": {"total": 15, "passed": 15, "counts": {"PASS": 15}},
  "failures": [{"id": "07", "status": "WA", "detail": "≤200 字符"}],
  "timing": {"eligible": true, "source": "event_warm", "warmup": 2, "repeats": 5,
             "cases": {"01": {"min_us": 9.2, "median_us": 9.4, "cold_us": 558.8}}},
  "public": {"efficiency": 0.83, "efficiency_raw": 0.25, "offset_us": 2.6, "offset_from": "<submission_id>",
             "cases": {"01": 0.96}, "tbest_from": "<submission_id>"},
  "baseline": {"sha": "abc1234", "run_id": "...", "geomean_speedup": 1.07,
               "cases": {"01": 1.12}} ,
  "hint": "≤200 字符的下一步提示，可为空"
}
```

- `failures` 最多 5 条，`timing.cases` 最多 20 条（按 min_us 从大到小），`public.cases` 最多 10 条，`baseline.cases` 最多 10 条（按偏离 1 的程度排序）。
- 没有数据的字段写 `null`，不得省略键。
- `offset_us` 是本地事件计时器的固定附加开销。如果官方测过同一个 SHA，取公开例上"本地热态最小值减官方时间"的中位数，写入 `results/.timing-offset.json` 缓存；之后测过其他 SHA 时沿用缓存值。`efficiency` 用扣除开销后的时间计算，`efficiency_raw` 是未校正值。真机实测：同一 SHA 上，10µs 量级的用例与官方相差约 2%，2–3µs 的用例约差 2.6µs。

### 3.3 远端运行配置（B1 写入、B2 读取）

case_bench 发往设备主机的 cfg 在现有键之外增加：

| 键 | 类型 | 含义 |
|---|---|---|
| `warmup` | int ≥0 | 每个计时部分先跑并丢弃的次数 |
| `repeats` | int ≥1 | 计入结果的次数（现有键，语义不变） |
| `tier` | int | 0 补测 · 1 普通 · 2 快速；数值大者优先 |
| `job_timeout` | int 秒 | 整个作业在设备上的总预算，等于 `timeout × 选中用例数`，上限 1800 |

B1 必须把 `device_broker` 模块和 `lanes`、`case_batch` 一起放进下发的 modules 字典（键名 `device_broker`）。BOOTSTRAP 要相应地把它写到 `root/'device_broker.py'`。

B2 输出的 `case` 事件在现有字段之外增加 `cold_us`（list[float]，warmup 样本）；`device_us` 只含 repeats 次热态样本。`device_batch` 事件增加 `broker`（bool）和 `segments`（int，TLE 续排后共占用设备的次数）。

### 3.4 测试运行登记表（A 提供，C 消费）

- 位置：`resolve_workspace_state_dir(ws) / "test_runs" / "<request_id>.json"`，`request_id` 为 32 位十六进制。
- 字段：`request_id, problem, sha, level, cases, remeasure, agent_id, role, created_at, pid, log, status(queued|running|finished|failed|cancelled), run_id, exit_code, digest, report, finished_at, observed(bool), delivered(bool)`。
- A 在 `services/test_runs.py` 暴露以下函数，C 只通过它们访问登记表：
  - `list_undelivered(ws) -> list[dict]`：已结束、`delivered` 为 false、`agent_id` 非空的记录；
  - `mark_delivered(ws, request_id) -> None`；
  - `compact_line(record) -> str`：不超过 600 字符的一行摘要。

### 3.5 MCP 工具参数（A 定义 schema，C 实现其中编排类的处理逻辑）

- `parhelion_sprint_start` 增加 `mode`：`solo`（默认）、`dual`、`full`。
- `parhelion_squad` 的 `recipient` 支持 agent_id、角色名（axis 或 role_label，不区分大小写）和 `all`。

## 4. 集成顺序

1. B2，然后 B1（B1 的测试会用到 B2 的 modules 键名）。
2. A（调用 B1 的命令行）。
3. C（读取 A 的登记表）。
4. D。
5. E：全量测试、真机端到端、发布。

每个工作包都在独立的工作树和分支上完成（`v4/<包名>`），通过自己的测试后交回。集成方按上面的顺序合入 `release/4.0`，处理冲突，跑全量测试。

## 5. 通用要求（所有工作包）

- 词汇保持中性：使用 accel / device / SDK / profiler / judge，不写厂商、产品或芯片名称。设备相关的取值一律通过 `cli/opsb_core/device_env.py` 或 case_bench 里的 `_dev()` 读取。
- 只用 Python 标准库；设备主机侧代码必须能在只有 python3 和 numpy 的环境下运行。
- 每个新函数都要有针对性的测试；不改动与本包无关的测试的断言。
- 提交信息用英文祈使句，结尾带 `Co-Authored-By` 行（见下）。
- 完成后在分支上提交，不要合并，不要 push。

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
