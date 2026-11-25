# 12 · 统一套件与级别（工作包 B1）

> 索引：[10-DETAILED-INDEX.md](10-DETAILED-INDEX.md) · 分支：`v4/b1-suite`

## 1. 要解决的问题

- 用例集合由每次运行临时传入的 profile / experiment / timing / repeats 决定。worker 自己拼配置，结果碎片化：今天 493 个 run 里，有的 run 只跑 1–2 个用例，各自排一次设备队。
- 计时只覆盖 `perf` 标记的用例，比例约 11%；而且 repeats 包含冷启动样本，所以报告把 `timing_score_eligible` 写死为 false，worker 拿不到本地性能信号。
- 报告里没有一份能直接回答"变快了吗、离 tbest 多远"的摘要。

## 2. 改动范围

修改：`scheduler/case_bench.py`、`scheduler/cli_bridge.py`（只改 `schedule-test` 的参数和转发）、`cli/opsb_core/commands/queue_cmd.py`（`run_test`）、`cli/opsb_core/cli_app.py`（只改 `test` 子命令）。
新建：`scheduler/suite.py`、`scheduler/tests/test_suite.py`。
补充测试：`scheduler/tests/test_case_bench.py`。

## 3. 套件文件（`scheduler/suite.py`）

### 3.1 位置与兼容

- 新文件：`workspace_bench_specs_dir(ws) / title / "suite.json"`。
- 读取顺序：先找 `suite.json`；没有时读 `case-profile.json`（旧 profile，`version: 1`），在内存里转换成套件，`suite_version` 记为 0。两者都没有时返回空套件（只有官方用例）。
- 不自动改写旧文件。`opsb bench suite migrate <Problem>`（见 §7）会显式写出 `suite.json`。

### 3.2 结构（`version: 2`）

```json
{
  "version": 2,
  "suite_version": 3,
  "updated_at": "ISO-8601",
  "strict_fp32": true,
  "warmup": 2,
  "repeats": 5,
  "case_timeout": 15,
  "reuse_session": true,
  "variants": [{"id": "local/odd-c129", "base": "03", "params": {}, "seed": 7,
                "name": "…", "perf": false, "tier": "standard"}],
  "buckets": {"small": ["01", "02", "local/odd-c129"]},
  "timed": ["05", "local/odd-c129"],
  "weights": {"01": 1.0}
}
```

- `variants`：沿用旧 profile 的 variant 规则，并新增可选字段 `tier`（`standard` 或 `final`，默认 `final`）。
- `timed`：需要计时的用例 ID，在 `perf` 标记之外额外指定。
- `weights`：可选的分数权重。官方用例默认 1，local 用例默认 0。现在只用于报告排序，以后给预计分用。
- 校验失败时抛出 `ValueError`，消息要写明是哪个字段出错。`warmup` 的范围是 0–10，`repeats` 是 1–50，`case_timeout` 是 1–600。

### 3.3 函数

- `load_suite(ws, title) -> dict`：返回规范化后的套件，所有字段都有值。
- `to_profile(suite) -> dict`：转换成 `catalog()` 能接受的旧 profile 形状（只保留 `version/variants/buckets/timing/repeats/case_timeout/strict_fp32/reuse_session`），这样 `catalog()` 的现有校验逻辑可以继续用。
- `select(cases, suite, level, ids=None) -> (selected, selection_label, formal)`：选例规则见 §4。
- `timed_ids(cases, suite) -> set`：`perf` 标记的用例 ∪ `suite["timed"]`。如果结果为空，就取全部官方用例。
- `write_suite(ws, title, suite)`：原子写入，`suite_version` 加 1，更新 `updated_at`。

## 4. 级别

| 级别 | 选例 | 计时 | 单例超时 | formal |
|---|---|---|---|---|
| `smoke` | 沿用 `adaptive_cases()`：先重测最近失败过的例；否则取 1 个官方正确性锚点 + 1 个计时例 | 选中例里的计时例，warmup 1、repeats 3 | `min(case_timeout, 8)` | 否 |
| `standard` | 全部官方例 + `tier=standard` 的 local 例 | 全部计时例，使用套件的 warmup / repeats | `case_timeout` | 是（未指定 `--case` 时） |
| `final` | 全部用例 | 全部计时例，repeats 取 `max(repeats, 7)` | `case_timeout` | 是 |

- 传了 `--case` 时，只跑指定的用例，`selection="explicit"`，不算 formal，计时规则和 standard 相同。
- worker 请求（`PARHELION_MCP_ROLE=worker`）禁用 `--profile/--experiment/--timing/--repeats/--runner/--bucket`，退出码 2，stderr 写 `Workers select a level (smoke|standard|final); the platform owns the suite`。CLI 用户和 orchestrator 仍可以使用这些参数（保留现有的实验功能），这时 `level` 记为 `custom`，不算 formal。
- `formal_evidence()` 要改：要求的用例集合等于当前套件下 `standard` 级别会选中的集合（`select(..., "standard")`）；要求这些用例全部 PASS，且每个用例的 key 一致。不再要求 `len(rows) == len(required)`，因为报告里的 rows 包含全部 catalog，未选中的记为 NOT_RUN。

## 5. `case_bench.run()` 的改动

1. 签名增加 `level="standard"`。原来的 `suite` 参数保留：`adaptive` 映射到 `smoke`，`all` 映射到 `standard`。
2. 读取套件 → `to_profile` → `catalog()` → `select()`。
3. 下发配置：`timing` 固定为 `"perf"`；但是否计时由每个用例自己的 `timed` 布尔值决定。为此给 `selected` 的每一项写入 `"timed": bool`，B2 的 `case_batch.prepare` 会读取它，代替原来的 `perf` 判断。
4. 新增配置键 `warmup`、`tier`、`job_timeout`（见索引 §3.3）。`tier`：smoke 为 2，standard 和 final 为 1。
5. modules 增加 `device_broker`，BOOTSTRAP 把它写到 `root/'device_broker.py'`。B2 交付该文件；在 B2 合入之前，如果文件不存在，就跳过这个模块，以便 B1 的测试能独立运行。
6. 去重键（`active_bench` 用的 key）要包含 `level` 和 `suite_version`。
7. 开始执行前（`_execute_run` 里写完 plan.json 之后），向 stdout 打印 `OPSB_RUN_STARTED {...}`（见索引 §3.1）。复用或加入已有运行时，在返回前打印一次，`run_id` 指向被复用的运行。
8. **计时统计**：解析 `case` 事件时读取 `device_us`（热态）和 `cold_us`，给每个计时例写入 `min_us`、`median_us`、`cold_us`（首个 warmup 样本，没有则为 null）、`samples`（热态样本数）。
9. `timing_source = "event_warm"`。满足 `warmup ≥ 1`、`repeats ≥ 3`、全部计时例都 PASS 并且都有样本时，`timing_score_eligible = true`；否则为 false，并在 `digest.hint` 里说明原因。
10. 生成 `digest`（结构见索引 §3.2）：
    - `public`：读取 `ws/results/submit-queue-outcome.jsonl` 里这道题最新一条带 `public_tbest_us` 的记录。按官方用例 ID 排序，前 `len(public_tbest_us)` 个视为公开例；某个公开例有 `min_us` 时，`ratio = tbest / min_us`（上限 1.5）；`efficiency` 取这些 ratio 的平均值。`tbest_from` 记录那条回执的 `submission_id`。没有数据时整个字段为 null。
    - `baseline`：先找基线 SHA——同一个账本里这道题得分最高的 Pass 记录的 SHA，且不能等于当前 SHA。再在 `results/<title>/<baseline>/bench-runs/*/report.json` 里找最新一份 `suite_version` 相同、`timing_score_eligible` 为 true 的报告，对共同的计时例计算 `speedup = base_min / cand_min`，取几何平均。找不到时为 null，`hint` 写"先在 <sha7> 上运行 standard 以启用基线对比"。
    - `failures`：非 PASS 且非 NOT_RUN 的用例，最多 5 条。
11. 最后一行 JSON（索引 §3.1）里带上 `digest`、`level` 和 `report`。`report.json` 本身也保存 `digest`、`level`、`suite_version`。
12. `compare_runs()` 改用 `min_us` 比较（没有时回退到 median），`timing_source` 跟随报告。

## 6. CLI

- `cli_app.py` 的 `test` 子命令增加 `--level {smoke,standard,final}`，默认不传（由 bridge 决定为 standard）。`--suite` 保留，并在 help 里标注为已废弃。
- `queue_cmd.run_test` 把 `--level` 转发给 `schedule-test`。
- `cli_bridge.py` 的 `schedule-test` 增加 `--level`，传给 `case_bench.run(level=...)`。只改参数定义和转发这两处，其他逻辑不动。

## 7. 套件管理命令（`opsb bench suite …`）

在 `cli_bridge.py` 里加一个 `suite` 动作，支持三个操作，`cli_app.py` 的 `bench` 下加子命令 `suite` 转发过去：

- `show <Problem>`：打印规范化后的套件、每个用例的级别归属和是否计时；
- `migrate <Problem>`：把旧的 `case-profile.json` 转成 `suite.json`（`suite_version` 记为 1），旧文件保留不动；
- `add-variant <Problem> --json '<variant>'`：校验通过后追加 variant，`suite_version` 加 1。

worker 不能调用这些命令（`PARHELION_MCP_ROLE=worker` 时拒绝）。

## 8. 验收测试

`scheduler/tests/test_suite.py`：

1. 旧 profile 能转换，`suite_version` 为 0；`migrate` 写出版本 1。
2. 三个级别的选例符合 §4 的表格，并覆盖 `tier` 的默认值。
3. 没有 perf 标记时，`timed_ids` 回退到全部官方例。
4. 越界的 `warmup` / `repeats` 抛出 `ValueError`。

`scheduler/tests/test_case_bench.py`（沿用现有的假 ssh / 假引擎写法）：

1. standard 运行打印出 `OPSB_RUN_STARTED`，最后一行 JSON 带有 `digest`，且 `digest` 序列化后不超过 2KB。
2. 带 warmup 的样本：`min_us` 只取热态，`timing_score_eligible` 为 true；warmup 为 0 时为 false。
3. 账本里有 `public_tbest_us` 时算出 `public.efficiency`；有基线报告时算出 `geomean_speedup`。
4. worker 环境下传 `--profile`，退出码为 2。
5. `formal_evidence` 在存在 `tier=final` 的变体时仍然成立（standard 运行可以作为正式证据）。
6. 不同 `level` 生成不同的去重键；相同请求会加入正在进行的运行。

## 9. 不要做的事

- 不要修改 `runtime/` 下的文件（B2）、`mcp/`（A）或 `agents/`（C）。
- 不要删除 profile / experiment 功能，只对 worker 禁用。
