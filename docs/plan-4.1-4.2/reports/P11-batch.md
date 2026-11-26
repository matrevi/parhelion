# P11 跨题设备批次、standard 早停与界面：真机报告

日期 2026-11-26，分支 `impl/P11-batch-ui`，测量在 08:44–08:49 UTC。真实芯片、工具链和路径只在 `~/.parhelion/config.json` 与主机上，不进仓库。

## 1. 实现要点

- **合并批次**（`device_broker._take_batch` / `serve_one`）
  - broker 取出 WSPT 队首后，如果紧随其后的作业属于另一道题，就在同一次持锁内依次执行，最多 3 个。遇到第一个不能合并的作业（同题，或其 `merge` 为 false）就停止，保证顺序仍是 WSPT。
  - 每个成员各自走自己的 segment：TLE 只续排该成员本身；排队时间（到它的 segment 开始为止）、持锁时间（只算它自己的 segment）、ledger 行和 `released`/`done` 事件都在它自己那段结束时立即发出，所以先执行的成员不会被后面成员的设备时间拖长端到端。
  - 持锁登记（`die_session.json`）和 ledger 带上 `batch` 字段，列出批次的全部成员，探针可见。开关是 `device.merge_batches`，默认 `on`，设为 `off` 即关闭；作业里对应 `merge` 字段。
- **R9 standard 早停**（`runtime/npu_runtime/early_abort.py`）
  - 仅限 standard 级别：平台随运行配置下发当前最佳官方 Pass SHA 在相同官方输入上的事件最小值。
  - 只有基线不小于 `min_us`（默认 200 µs）的用例参与判断。在这个量级以下，事件计时 5↔25 µs 的跳档会大过真实差异。
  - 前 `cases` 个参与判断的用例（默认 3 个）如果全部慢于基线的 `factor` 倍（默认 1.25），运行进程就断开 broker 连接，作业随之取消、设备释放。其余用例记为 `SKIP`，报告与 digest 标 `aborted_early`。
  - 阈值在 `config.json` 的 `scheduler.early_abort` 里配置，`{"enabled": false}` 关闭；测量时可用环境变量 `OPSB_EARLY_ABORT=off` 临时关闭。final 级别不带这项配置。
- **界面**
  - daemon 热帧新增 `test_runs`（最近 12 条测试登记，不带 digest）。digest 由 `GET /v1/test-runs/<request_id>` 按需拉取。
  - 热 mesh 新增 `suspensions`：驱动、原因、开始时间、受影响的 worker。`POST /v1/drivers/<driver>/resume` 走现有的 `clear_driver_suspension` 显式解除路径，只接受管理员 token。
  - Swift：NPU 页的服务器队列下方新增「测试登记表」，每行可展开 digest；协作页顶部和设置页的驱动诊断里都有驱动暂停横幅和「解除暂停」按钮。所有新字段都用 `decodeIfPresent`。三车道卡片原本就按运行 ID 每运行一张（`NpuLaneModel`），没有改动。

## 2. 真机验收：两题同时排队，合并与不合并对比

做法：先在设备主机上用 `flock` 占住设备锁 75 s，期间同时提交 NeighborListPbcFixedK 3f33362 和 EdgeIndexCoalesce 4f954c3 的 standard；锁一释放，两题的作业就排在一起。两种情况各跑一次，都带 `OPSB_EARLY_ABORT=off`，保证每个用例都计时；「不合并」用 `OPSB_DEVICE_MERGE_BATCHES=off`。主机重启后用例缓存是冷的，所以先跑了一次 NeighborList 预热（34.5 s，不计入对比）；两种情况的准备都命中缓存（0.09–0.10 s）。

| | 合并 | 不合并 |
|---|---|---|
| ledger `batch` 字段 | 两条都是 `[Edge, NL]` | 无 |
| 执行顺序 | Edge → NL | Edge → NL |
| Edge 持锁 | 10.025 s | 10.042 s |
| NL 持锁 | 5.029 s | 4.042 s |
| 两段之间的间隔 | 1 ms | 1 ms |
| 两题设备占用合计（首段开始到末段结束） | 15.06 s | 14.09 s |
| 会话建立次数 | 2（每题一个 sample 进程） | 2 |
| 端到端：Edge / NL | 97.5 / 94.1 s | 97.9 / 93.3 s |
| 排队：Edge / NL | 54.6 / 64.9 s | 54.8 / 64.9 s |

- **记账没有失真。** 合并后两题的持锁、排队、端到端都和不合并时一样（NL 4.0 与 5.0 s 的差异属于 P10 已测到的单题波动，其持锁本身在 4.03–5.03 s 之间变化）。后执行的 NL，其排队时间里包含了 Edge 的 10 s，这正是它真实的等待。
- **合并在这台主机上没有可测的收益。** broker 在作业之间的切换本来就只有约 1 ms（解锁、取下一个作业、加锁）。真正按作业计的成本是每题 sample 进程的启动与拆除，以及 profiler 交还设备的 2.4–3.4 s（见 P10）。这两项都是按可执行文件计的，跨题无法共享：两题是不同的可执行文件，而设备同一时间只为一个进程做 profiler 采集，所以无法提前启动下一题的进程来重叠等待。
- 结论：合并实现正确且无害，按子计划保留默认开启；它的价值目前只在于探针能看到批次成员。如果以后想真正省下按作业的成本，方向是在锁外提前完成下一题 sample 的初始化（仅限不开 profiler 的 smoke 级别）。

## 3. 未在真机上验证的部分

- **R9 早停**只有单测（`runtime/tests/test_batch_merge.py`：取消作业、释放设备、余下用例记为 `SKIP`，以及阈值规则），没有在真机上触发过；本次测量为了完整计时特意关闭了它。默认阈值（不小于 200 µs 的用例，前 3 个都慢 25%）是根据 P10 测到的事件噪声设的，建议拿几次真实的慢候选再核对一次。
- **界面截图**没有做。新数据要等 daemon 换上本分支代码才会出现，而运行中的 daemon 不能重启。合入并重启 daemon 后，用一次 smoke 运行就能看到：NPU 页的测试登记表；协作页和设置页的驱动暂停横幅（只有存在暂停时才会显示）。

## 4. 本次在真实结果目录留下的数据

本次 5 次运行都在 `results/<题>/<SHA>/bench-runs/` 下，运行 ID 为：

- 预热：`a78486ac…`
- 合并：`8587e0f7…`（NL）、`8fcb5c4d…`（Edge）
- 不合并：`86cc23c7…`（NL）、`229808d7…`（Edge）

P10 早先留下的 3 份 `NOT_RUN` 报告（`3261c0bf…`、`3dfda07b…`、`9d04d36a…`）按要求保留，没有删除。
