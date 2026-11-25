# 13 · 设备代理与热态计时（工作包 B2）

> 索引：[10-DETAILED-INDEX.md](10-DETAILED-INDEX.md) · 分支：`v4/b2-broker`

## 1. 要解决的问题

- `lanes.device_lane` 的调度方式是：每个运行进程各自写票据，每 0.2 秒检查一次自己是否排在最前；选出的队首再到内核里阻塞等 flock。这样有三个问题：
  - 内核分配锁的顺序不按 WSPT，不经过平台、直接拿 flock 的进程总能插队；
  - 已退出进程留下的票据会长期残留（设备主机上现有 23 个死票）；
  - 一个卡住的持锁者会让所有人一直等下去。
- `case_batch.execute_batch` 在某个用例 TLE 之后，把剩下的用例全部记为 NOT_RUN；这与平台约定的"回报 TLE、释放锁、为余下用例重新排票"不符。
- 计时没有 warmup：第一个样本包含内核加载等冷启动开销（约 550µs），而官方计时约 9µs。

## 2. 改动范围

新建：`runtime/npu_runtime/device_broker.py`、`runtime/tests/test_device_broker.py`（以及 `runtime/tests/__init__.py`，如果测试发现需要）。
修改：`runtime/npu_runtime/case_batch.py`、`runtime/npu_runtime/lanes.py`。

这些文件只用 Python 标准库（`case_batch.verify` 现有的 numpy 依赖除外），因为它们会被原样下发到设备主机执行。

## 3. 代理进程（`device_broker.py`）

### 3.1 位置与生命周期

- 运行目录：环境变量 `OPSB_BROKER_DIR`，默认 `~/.opsb/broker/`。目录下有 `broker.sock`（unix socket）、`broker.pid`、`broker.log`、`broker-<hash>.py`（代码副本）。
- `ensure_broker(cfg, source_text) -> socket path | None`（由 `case_batch` 调用）：
  1. 计算 `hash = sha256(source_text)[:12]`。
  2. 尝试连接 socket，发送 `{"op": "hello"}`。如果回复的 `version` 等于 hash，直接返回。
  3. 版本不同：发送 `{"op": "drain"}`。旧代理不再接受新作业，当前作业完成后退出；等它退出，最多等 `job_timeout`。
  4. 连接失败或旧代理已退出：用 `fcntl.flock` 锁住 `broker.start.lock`，再检查一次，确实没有代理时，把源码写到 `broker-<hash>.py`，然后用 `subprocess.Popen([sys.executable, '-u', path, '--dir', dir, '--lock', cfg['lock'], '--device', cfg['device']], start_new_session=True, stdout/stderr 写 broker.log)` 启动，最多等 5 秒直到 socket 可连。
  5. 仍然失败就返回 None，调用方回退到 `lanes.device_lane`。
- 代理空闲 30 分钟后自动退出，退出时删除 socket 和 pid 文件。
- 启动时清理 `/tmp/opsb-npu-tickets/` 下 pid 已经不存在的票据。

### 3.2 协议

unix socket 上收发换行分隔的 JSON，一个连接对应一个作业。

客户端发送：
```json
{"op": "submit", "job": {
  "run_id": "...", "title": "...", "sha": "...", "client_pid": 123,
  "tier": 1, "prio": 1.0, "created": 1795629521.2,
  "executable": "/abs/build-casebench/sample", "device": "0",
  "reuse_session": true, "case_timeout": 15, "job_timeout": 225,
  "cases": [{"index": 1, "id": "01", "timed": true, "warmup": 2,
             "parts": [{"warm": ["/abs/1/p0/warm-0", "/abs/1/p0/warm-1"],
                        "runs": ["/abs/1/p0/run-0", "/abs/1/p0/run-1"]}]}]
}}
```

代理回复（逐行）：

| 事件 | 字段 | 时机 |
|---|---|---|
| `queued` | `position`（从 1 开始）、`ahead`（前面的 run_id 列表，最多 5 个） | 接受作业后，以及排队位置变化时 |
| `started` | `wait_sec`、`segment` | 拿到设备锁、开始执行时 |
| `progress` | `index, id, part, parts, repeat, repeats, phase(warm|run)` | 每次执行前 |
| `case` | `index, id, status(PASS/RE/TLE), samples[µs], cold[µs], detail` | 每个用例结束时 |
| `released` | `hold_sec, segment, requeued(剩余用例数)` | 释放锁时（包括 TLE 后续排） |
| `done` | `wait_sec, hold_sec, segments` | 作业全部结束时 |
| `error` | `detail` | 协议错误或作业被拒时 |

客户端断开（运行进程被杀）时，代理把作业从队列中移除；如果正在执行，就杀掉样例进程、释放锁，并照常写账本。

### 3.3 调度

- 队列放在内存里，排序键与现有的 `lanes._priority` 一致：`(tier, prio, -created)` 最大者先执行。`prio` 按 `wspt_base + 等待秒数 / max(60, aging_tau)` 每秒更新；等待超过 1800 秒的作业 `tier` 至少提到 2。
- 一次只执行一个作业。执行时以阻塞方式 `flock(LOCK_EX)` 获取 `--lock` 指定的锁文件，这样直接拿 flock 的外部工具依然互斥。
- 为排队中的作业写票据 `/tmp/opsb-npu-tickets/<client_pid>.json`，字段与 `lanes.device_lane` 现有的写法一致：`pid=client_pid, title, sha, run_id, fast, tier, prio, created, case_progress`。出队时删除。
- 持锁期间写 `~/.opsb/die_session.json`，字段与现有格式一致：`pid=client_pid, title, sha, run_id, die=0, mode, substage="npu_exec", started_epoch, case_progress`。释放后向 `~/.opsb/die_ledger.jsonl` 追加一行，增加 `hold_sec`、`ended_epoch` 和 `"broker": true`。这样探针和 UI 不需要改。

### 3.4 执行

- 样例进程的启动和读写复用 `case_batch.SampleProcess` 的协议：stdin 写 `"{timed} {path}\n"`，从 `OPSB_BATCH_FD` 对应的 socket 读回 `"0 <us>"`。把 `SampleProcess` 从 `case_batch.py` 移到 `device_broker.py`，`case_batch` 改为从这里导入。
- 每个用例有自己的截止时间：`monotonic() + case_timeout`。对计时例，先依次执行 `warm` 路径（结果记入 `cold`），再执行 `runs`（结果记入 `samples`）。非计时例的 `warm` 为空，`runs` 只有 1 个。
- 某个部分的样本是该部分各次执行的结果；一个用例的第 r 个样本等于所有部分第 r 次结果之和（与现有 `verify` 的做法一致）。warm 样本也按同样方法相加。
- **TLE**：杀掉样例进程，本用例记为 TLE，然后立即释放锁（`released.requeued = 剩余数`），把剩余用例作为同一作业的下一段重新入队，保留原来的 `created` 和 `tier`，然后继续调度。
- **RE**：本用例记为 RE，剩余用例全部记为 NOT_RUN（`detail = "Earlier device execution failed"`），与现有行为相同，然后结束作业。
- 整个作业的累计持锁时间超过 `job_timeout` 时，剩余用例记为 TLE，结束作业。
- `reuse_session=false` 时，每次执行后关闭样例进程，与现有行为相同。

## 4. `case_batch.py` 的改动

1. `prepare()`：
   - 用例是否计时改为读取 `item.get("timed")`；没有这个字段时，回退到原来的 `timing/perf` 判断。
   - 计时例除了 `run-{r}` 目录，还要建 `warm-{k}` 目录（k 从 0 到 `cfg["warmup"]-1`），符号链接的建法和 run 目录相同。
   - `part["warm"]` 记录这些路径。
2. `execute_batch()`：
   - 先调用 `device_broker.ensure_broker(cfg, Path(__file__).with_name('device_broker.py').read_text())`。
   - 拿到 socket 时，按 §3.2 提交作业并消费事件：`case` 事件写回对应 plan 的 `samples` 和 `cold`，`progress` 事件转发给 `case_series` 的 update。返回 `{"wait_sec", "hold_sec", "acquisitions": segments, "segments", "broker": true}`。
   - 拿不到 socket 时，走原来的 `device_lane` 路径，并在返回值里加 `"broker": false`。原路径也要支持 warmup（先执行 warm 路径并丢弃结果）。TLE 的处理保持原样，并在 detail 里注明"broker unavailable"。
3. `verify()`：只检查 `runs` 的输出，不检查 warm 的输出。返回值增加 `cold_us`（按部分相加的 warm 样本）。
4. `run_series()` 发出的 `case` 事件带上 `cold_us`；`device_batch` 事件带上 `broker` 和 `segments`。

## 5. `lanes.py` 的改动

- 新增 `reap_dead_tickets(directory) -> int`：删除 pid 已经不存在的票据，返回删除的数量。`device_lane` 每次进入循环时调用它，代理启动时也调用。
- `device_lane` 的其他逻辑不变，作为回退路径。

## 6. 验收测试（`runtime/tests/test_device_broker.py`）

测试用一个假的样例程序：一个 Python 脚本，按 SampleProcess 协议从 stdin 读路径，从路径名里读出预设的耗时或故障类型，然后写回 `"0 <us>"`、错误码或者 sleep 超时。所有目录都设在 `tmp_path` 下（`OPSB_BROKER_DIR`、锁文件、票据目录用环境变量 `OPSB_TICKET_DIR` 覆盖；`lanes` 和代理都要支持这个变量，默认值仍是 `/tmp/opsb-npu-tickets`）。

1. 单个作业：返回 `queued → started → case… → released → done`，样本数等于 repeats，cold 数等于 warmup。
2. 三个作业同时提交，tier 和 prio 各不相同：执行顺序符合排序键。
3. TLE：第 2 个用例超时后，第 3 个用例在下一段执行，最终 `segments == 2`，结果分别为 PASS、TLE、PASS。
4. RE：后续用例为 NOT_RUN。
5. 客户端断开：排队中的作业被移除；执行中的作业，样例进程被杀，锁被释放。
6. 版本不同的代理会被 drain 后替换。
7. `ensure_broker` 失败（目录不可写）时返回 None，`execute_batch` 走回退路径，`broker` 为 false。
8. 死票清理：伪造一个 pid 不存在的票据，代理启动后它被删除。
9. 持锁期间 `die_session.json` 存在且字段完整，释放后账本追加了一行。

## 7. 不要做的事

- 不要修改 `scheduler/`（B1）或 `cli/`。
- 不要改变 `die_session.json`、票据和账本里现有字段的含义，只能增加字段。
