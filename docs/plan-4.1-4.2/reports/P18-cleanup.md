状态：已交付

# P18 收尾小修（4.2 §3.6）

日期 2026-11-26，分支 `impl/P18-cleanup`，基于 `main` `152fd62`。三项都已完成，无未做项。没有改 `aoci*.txt` 或 `.aoci/`，没有重启或停止守护进程，没有提交判题，没有改自动提交开关，也没有改门禁阈值或暂缓理由。

## 1. 分支与提交

```text
$ git log --oneline main..HEAD
760fb74 Make the device case cache and scratch directories configurable
0a4ed8f Run the device broker budget tests on an injected clock
ed549a6 Show the closing reserve without deducting it from free quota
```

本报告随最后一个 `Deliver P18:` 提交进入分支，因此上面三条是本报告提交前的列表。

## 2. 第 1 项：可用额度显示（已完成）

`available_normal_shots` 现在就是各账号的剩余额度，不再扣除收尾预留；收尾预留继续以单独的 `closing_reserve` 字段显示。三条产生该数字的路径都改了：

| 文件 | 位置 | 改动 |
|---|---|---|
| `cli/opsb_core/ipc_bridge.py` | `merge_bridge_enrichment` | 有活跃账号时直接用团队 `remaining`；没有活跃账号时优先用桥接的 `remaining_today`（旧桥只有 `discretionary_remaining` 时把 `closing_reserve` 加回去） |
| `cli/opsb_core/daemon/state_cache.py` | `build_hot_pipeline_and_scheduler` | 热帧 `avail_shots` 不再减 `eff_closing_reserve` |
| `cli/opsb_core/services/queue_service.py` | `compute_workspace_snapshot` | 冷快照同样改法。§3.6 没列这一处，但 `opsb queue list --json` 和 `opsb queue config` 用它，不改会让两处数字不一致 |
| `cli/opsb_core/commands/status_cmd.py` | `Gate & Quota` 行 | 文案改为 `Avail N, Reserve R shown only` |

**口径红线。** `scheduler/gate.py` 和 `compute_effective_closing_reserve` 都没有改。门禁 S4 的派发判定只读 `quota.remaining_today`；门禁算出的 `discretionary_remaining` 仍然保留，但没有被任何门禁、暂缓或排序路径使用。门禁结果与 4.1.0 相同，变的只有显示数字。测试另外断言了 `quota.discretionary_remaining == 89` 而显示的 `available_normal_shots == 99`，两者同时存在。

**Swift。** `Sources/` 里 `available_normal_shots` 只出现在 `NpuModels.swift` 和 `QueueModels.swift` 的解码枚举中，没有任何视图显示它；`SchedulerBarView` 的「收官预留额度」滑块显示的是有效预留值并写回配置，与本项无关。因此没有改 Swift。按要求仍跑了 `swift build`，结果见 §4。

**单测。**
- 更新 `cli/tests/test_queue_quota_consistency.py`：多账号场景下，冷快照、MCP `parhelion_status`、守护进程热帧都断言 `available_normal_shots == 99`、`closing_reserve == 10`，并保留 `quota.discretionary_remaining == 89`。
- 新增 `test_shown_free_shots_keep_the_closing_reserve_separate`：覆盖有活跃账号、无活跃账号的桥接回退、以及只报告预留后数字的旧桥接三种形态。

## 3. 第 2 项：设备代理测试加固（已完成）

`runtime/tests/test_device_broker.py` 的 `test_job_budget_turns_remaining_cases_into_tle` 与 `test_case_budget_limits_each_execution_not_the_repeats` 不再 sleep，也不再看墙钟：

- 两个用例改为在测试进程内驱动真实的 `device_broker.Broker`（`serve_one`），把 `device_broker.time` 换成虚拟单调时钟，把 `SampleProcess` 换成按路径里 `cost-<秒>` 记账的同进程样本。没有 unix socket、没有子进程，也没有真实的 0.4–2 s 等待。
- 虚拟样本仍然按传给它的 deadline 判定 TLE，因此测的仍是 broker 的预算逻辑：逐次执行 deadline（`test_case_budget…`）与整个 job 的预算把剩余用例转成 TLE（`test_job_budget…`）。逐次预算若被误写成「整个用例共用一个 deadline」，前者会在第 3–4 次执行失败。
- unix socket / 样本协议路径仍由同文件的 `test_case_timeout_releases_lock_and_requeues_the_rest`、`test_seconds_long_case_stops_after_one_warmup_and_two_runs` 等端到端用例覆盖。

**20 次连跑（验收）。** 在工作树内执行，负载是同时跑另一条全量测试（`~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider`）加 10 个 `yes > /dev/null` 占满 CPU；12 核机器上 load average 从 12.37 升到 16.90。

```bash
for i in $(seq 1 20); do
  PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider \
    runtime/tests/test_device_broker.py \
    -k "job_budget_turns_remaining_cases_into_tle or case_budget_limits_each_execution_not_the_repeats"
done
```

结果：**20/20 通过，0 失败**。第 1 次 0.40 s（进程冷启动与 import），第 2–20 次每次 `2 passed` 用时 0.02–0.05 s。并发的全量测试在同一负载下也跑完并通过（`691 passed, 19 skipped`，见 §4）。

对照：一次误在 4.1.0 主检出的旧测试上做的同样负载循环，20 次失败 2 次（第一个 0.6 s 用例被记成 TLE），正是本项要消除的现象；主检出没有被改动。

## 4. 第 3 项：用例缓存目录可配置（已完成，评估后成本可控）

§3.6 把它列为可选。评估结果是改动集中、`case_cache._link_or_copy` 本来就会在 `os.link` 失败时退回复制，不需要重写硬链接解析，因此做完：

- `cli/opsb_core/device_env.py` 的 `DEFAULTS` 新增 `case_cache_dir`、`case_tmp_dir`，默认都是空串（保持现状）。部署值只存在于 `~/.parhelion/config.json` 的 `device` 段，仓库里没有真实路径。
- `scheduler/case_bench.py` 把两项放进设备运行配置（`cfg`，随 bootstrap 经 stdin JSON 发到设备主机，不放到 SSH 命令行，避免 `ps` 可见），bootstrap 用 `case_tmp_dir` 建 `TemporaryDirectory` 并在需要时先 `mkdir -p`。
- `runtime/npu_runtime/case_cache.py`：`CaseCache.top` 的优先级为 `OPSB_CASE_CACHE_DIR` > `cfg.case_cache_dir` > 现有默认值 `repo.parent.parent/opsb-case-cache`。因为 `case_tmp_dir` 移动的是 bootstrap 根目录，`repo.parent.parent` 会跟着移动，所以只设 `case_tmp_dir` 时缓存与 scratch 仍在同一文件系统。
- 同文件系统检查：新增 `case_cache.same_filesystem`；`run_series` 在显式配置了任一目录且两者不在同一文件系统时向 stderr 打印一次明确提示，随后由 `_link_or_copy` 自动退回复制。没有静默假设。
- `opsb doctor --cases --remote`：`doctor_cases` 把 `device.case_cache_dir` 作为 `CONFIGURED_DIR` 嵌进发往设备主机的脚本（仍走 stdin），`find_root(configured)` 会优先采用它；未配置时退回原来的 `/tmp/opsb-case-cache` 查找逻辑。本地 `--cases` 不采用该设备路径。
- 新增测试：`scheduler/tests/test_case_cache.py` 覆盖配置目录、`find_root`、`same_filesystem`；`scheduler/tests/test_case_bench.py` 断言两项进入 `settings`、空值时保持 `None`；`cli/tests/test_doctor_cases.py` 断言远端报告使用配置目录。

## 5. 验证

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
691 passed, 19 skipped, 2 subtests passed in 70.30s (0:01:10)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
444 passed in 54.85s
```

4.1.0 基线是 686 passed / 19 skipped 与 442 passed；新增 5 条（全量根目录）与 2 条（第二条命令范围）测试。

`swift build`：通过，末行 `Build complete! (10.01 sec)`。没有改 Swift 源码；按要求重新构建了一次。

第 2 项的 20 次连跑输出摘要见 §3，20/20 通过。

## 6. NOTE（发现的非显然约束）

- `queue_service.compute_workspace_snapshot` 是第三个扣预留的地方（§3.6 只列了 `ipc_bridge` 与 `state_cache` 两处）。不一起改，`opsb queue list --json` 会仍显示旧数字。
- `scheduler/gate.py` 的 `discretionary_remaining` 仍是「剩余额度减预留」，它只是 `QuotaStatus` 的字段；门禁 S4 只读 `remaining_today`，所以保留它不会把额度带进门禁。旧桥接兼容分支也靠「`discretionary_remaining` + `closing_reserve`」重建剩余额度。
- Swift 解码了 `available_normal_shots`，但没有任何视图使用；不要因为改了 JSON 数字就去找 Swift 的显示位置。
- `case_tmp_dir` 移动 bootstrap 根目录，`CaseCache` 的默认缓存根 `repo.parent.parent/opsb-case-cache` 会自动跟着走；只有显式 `case_cache_dir` 才可能造成两者跨文件系统，这时 `run_series` 会提示并退回复制。
- `device.case_cache_dir` 只对 `doctor --cases --remote` 有意义；本地 `--cases` 看的是本机缓存，不应套用设备主机的路径。
- 两个预算测试现在 patch 的是 `device_broker` 模块的 `time` 属性；如果以后把 broker 改成 `from time import monotonic`，需要同步改注入方式，否则测试会直接失败而不是静默变绿。
- 那两条测试不再覆盖 unix socket 与样本协议，这是有意为之；协议覆盖留在同文件的其他端到端用例。

## 7. 已知风险与建议后续

- **显示语义变化。** 运维看到的 `Avail` 会变大，含义是「账号剩余额度」，收尾预留另行显示。建议主会话在 `RUNBOOK.md` §2 与 4.2 发布说明里写清楚，避免被读成「预留已经花掉」。
- **配置项默认不生效。** `case_cache_dir`、`case_tmp_dir` 为空时行为与 4.1.0 完全一致。要在容器重启后保住缓存，需要在设备主机的 `config.json` 里把两项设到同一个持久盘目录；只设一项可能跨文件系统，运行时会给提示并复制。
- **跨文件系统复制成本。** 大用例跨盘复制会明显变慢；本包选择「提示 + 复制」而不是拒绝运行，后续若觉得太慢可以改成显式失败或禁止该组合。
- **两项目录还没在真机验证。** 本包没有登录设备主机，也没有重启容器；`case_tmp_dir` 的 `mkdir -p` 与 bootstrap 传递只有单测。建议设备可用时在主机上设两个同一文件系统的目录，跑一次 `opsb doctor --cases --remote`，再做一次容器重启后的缓存命中检查。
- **负载验收的可迁移性。** 20 次连跑是在本机 12 核、load average 12–17 的条件下完成的；CI 核数不同不影响确定性（虚拟时钟与真实负载无关），但报告里的负载数字只代表本机。
- **主会话收尾。** `RUNBOOK.md` §2 的两条已知遗留可以删掉；4.2 发布说明需要补 `device.case_cache_dir` / `device.case_tmp_dir` 与新的 `available_normal_shots` 口径；`ipc_bridge.py`、`state_cache.py`、`case_cache.py`、`case_bench.py` 的 aoci 条目需要按新职责更新。
