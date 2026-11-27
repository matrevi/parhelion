# 4.4.8 修复计划：重启与供应商故障后的恢复

状态：A–D 已实现，见第 7 节。

来源：2026-11-27 端到端测试（`RUN-LOG-2026-11-27-e2e.md`）里 03:39 之后的四次事故。这几次事故都要靠管理员手工收拾：杀进程、逐个发跟进、重新拉起主控。验收单位仍是 AGENTS.md 规定的完整流程：从 worker 交付候选，到收到官方反馈。

## 1. 事故与证据

| # | 现象 | 证据 |
|---|------|------|
| A | 三个测试运行卡住 35–64 分钟。FFT solo 被「An experiment is already admitted for this problem」挡住，什么都测不了。 | `677d920e`（FFT b7a650a standard）与 `12ad1d12`（Sgemm e13ca77 standard）停在 `prepared` 之后，没有 `device_batch`。`82f27045`（EdgeIndex 5d8124b final）在设备批次后的逐例校验里停在 case 02。三个都创建于 03:39–03:40，正好是三个守护进程并存的窗口（问题 11）。设备主机上的 broker 只有一个版本（`9bd4790ce609`），没有版本来回替换，后来的运行都能正常拿到设备。管理员杀掉进程后，注册表才把它们记成 failed，准入才放开。 |
| B | 03:49 DeepSeek 余额耗尽后，主控再也没有回来。worker 的升级请求（Sgemm 逃逸锁）没人读，直到 04:17 管理员手工重新拉起主控。 | 主控会话日志最后一行是 `Insufficient Balance`，`driver_sessions.json` 里状态是 `failed`。`POST /v1/drivers/deepseek/resume` 只删掉暂停记录，被暂停的席位和主控都没有恢复。暂停通知要主控「恢复后重新派发」，可主控自己就在同一个驱动上，也停了。 |
| C | 用 `--resume <native id>` 重新拉起主控时，DSH 返回 `session/load` Method not found，平台悄悄开了新会话。新主控只拿到标准 brief 和管理员手写的目标，不知道之前的冲刺状态。 | 新会话日志第一条是 `-32601 session/load`。`open_session` 返回的「是否续上」标志被丢掉，会话记录里看不出这次是续上还是新开。 |
| D | 03:50 同时跑着三个守护进程（18910、18911、18912），都在同一个工作区跑 tick。 | `run_daemon_server` 先做 `recover_workers` 和缓存预热，然后才写 `daemon.json`。`start_daemon` 只等 6 秒，并且只在这 6 秒里持有启动锁。下一个启动者（应用的自动拉起）看不到状态文件，于是再拉起一个守护进程；新进程发现端口被占，就换到下一个端口。这一次启动正好也报了「failed to become healthy within 6s」。 |

## 2. 根因

**A. 运行没有整体时限，卡住的运行一直占着本题的实验名额。**运行以独立进程组启动（`start_new_session=True`），日志写进文件。这是为了让运行熬过守护进程重启，是有意的设计。可是平台只在进程退出时结算；进程活着但不再前进时，没有任何机制处理它。`case_bench.active_bench` 用 `.worker-experiment.lock` 保证每题同时只有一个实验，这把 flock 由运行进程持有，进程不死，本题就一直被挡着。三个运行为什么会卡住，现有日志不足以确定（设备主机上已经找不到当时的进程）。时间上它们都落在多守护进程窗口里，D 修好后应该很少再出现。但平台不能靠「不会再卡」来保证进度，必须有兜底。

**B. 供应商故障只恢复了一半。**mesh 里的 worker 遇到供应商故障时会触发 `suspend_driver`。后台主控不是 mesh runner，它的失败只在 `_run_worker_job` 里记一个 `failed`：不触发暂停，也不发告警。管理员解除暂停时，`clear_driver_suspension` 只改暂停文件，被暂停的席位（`error = suspended: provider fault` 或原始的供应商错误）和失败的主控都要人手工唤醒。

**C. 续会话失败没有降级方案。**`AcpSessionClient.open_session` 在 `session/load` 失败时改用 `session/new`，这个降级本身合理。问题在于降级之后，平台没有给新会话补上下文，也没有记下这次续会话失败了。

**D. 守护进程没有单实例保证。**启动锁只覆盖启动者的 6 秒等待，不覆盖守护进程本身的生命周期。状态文件要等预热完才写。端口被占时会自动换端口，于是多出来的实例不会失败退出，而是悄悄并行运行。

## 3. 修复

### A. 卡住的运行由平台结算（`services/test_runs.py`）

- 新增 `reap_stalled(ws, now)`：找出状态为 `running`、进程还活着、但很久没有动静的运行。动静取运行日志和本次运行 `events.jsonl` 两者里较新的修改时间。超过阈值后，按 `cancel` 的方式对进程组先发 SIGTERM，5 秒后发 SIGKILL，然后记为 `failed`，`error` 写「stalled: no output for N min; the platform stopped it — resubmit」。进程一死，它持有的 `.worker-experiment.lock` 就释放了。
- 阈值默认 45 分钟，可用配置 `test_runs.stall_minutes` 调整。依据：broker 单个作业的上限是 `job_timeout ≤ 1800 s`，排队等待超过 30 分钟的作业会提升优先级（`STARVATION_SEC = 1800`）。正常运行不会超过 45 分钟一行输出都没有；这次卡住的三个运行在 35–64 分钟之间，其中两个会被这条规则结算（第三个由管理员在 37 分钟时杀掉）。
- 调用点：`list_undelivered` 已经负责结算进程已死的运行，就在这里先调用 `reap_stalled`。这样守护进程每次 tick 时都会检查，重启后的第一次 tick 也会。结算后的运行和其他运行一样送到原 worker 的收件箱，worker 看到原因后自己决定是否重新提交。

### B. 供应商故障恢复后自动续上（`drivers/registry.py`、`agents/manager.py`）

1. 后台主控的回合因供应商故障失败时（错误文本匹配 `PROVIDER_FAULT`），`_run_worker_job` 在会话记录里写下 `error` 和 `fault = "provider"`，并调用 `suspend_driver`。这样横幅和战略消息照常出现，同一驱动的其他派发也会暂停。
2. `clear_driver_suspension` 解除暂停后调用 `resume_after_outage(ws, driver)`：
   - 给该驱动上 `status = error`、且错误是 `SUSPENDED_ERROR` 或匹配 `PROVIDER_FAULT` 的席位，各发一条跟进：「供应商已恢复，从中断处继续当前实验」。发跟进就是唤醒 error 席位的正规途径，管理员手工停掉的席位是 `stopped`，不受影响。
   - 如果该驱动最近一个主控会话的状态是 `failed`、`fault = "provider"`，而且工作区里没有正在运行的主控会话，就用 `--resume <native id>` 以后台方式重新拉起，目标里写明「上一回合因供应商故障中断，冲刺仍在进行，只有管理员能结束」。每次解除暂停最多拉起一次，按会话 id 记录，避免重复。
3. `resume` 接口的返回值增加 `resumed_seats` 与 `orchestrator`，应用和 CLI 都能看到实际唤醒了什么。

### C. 续会话失败时补交接（`drivers/antigravity_acp.py`、`drivers/registry.py`）

- `run_acp_headless_turn` 接收一个可选的 `handoff` 回调。传了 `resume_id`、而 `open_session` 返回「没续上」时，平台把 `handoff()` 的文本放在 brief 前面，并在会话记录里写 `resume = "fresh"`；续上了就写 `resume = "loaded"`。
- 主控的交接文本由 `drivers/brief.py` 的 `orchestrator_handoff(ws)` 生成，上限 1.2 KB，内容是：
  - 「上一会话无法载入，这是新会话」；
  - 各题在线席位的数目与状态；
  - 未确认的 worker 问题条数，以及提问的席位；
  - 最近一条管理员指令的前 200 字节。
  不复制账本或日志正文，具体内容由主控用 `parhelion_agent_messages` 自己去读。

### D. 守护进程单实例（`daemon/server.py`）

- `run_daemon_server` 在绑定端口之前，以非阻塞方式拿实例锁 `daemon.instance.lock`，并持有到退出。拿不到锁说明已有实例在运行，直接退出，返回码 0，日志写「another daemon owns this home」。不会绑定端口，也不会跑 tick。
- 端口绑定成功后立刻写 `daemon.json`（带 `starting: true`），预热完成后再写一次，把它去掉。
- `start_daemon` 发现状态文件里的 pid 还活着、健康检查还没通过时，认为守护进程正在启动，继续等，不再拉起新的。健康等待从 6 秒放宽到 30 秒。
- `stop_daemon` 发 SIGTERM 后的等待从 3 秒放宽到 15 秒。16 个席位时 `stop_all` 要逐个停 runner、写状态，3 秒后就 SIGKILL 会让一部分席位停在中间状态。

## 4. 不在本次范围

- 三个运行具体卡在哪里（设备排队还是 CPU 校验），留待下次复现时在设备主机上抓现场。A 的兜底不依赖这个结论。
- 主控停掉有活跃假设的席位前要求说明理由（问题 6），另行处理。

## 5. 测试

每条都要先在未修复的代码上失败：

- A：伪造一个运行记录，用一个活着但不输出的子进程，日志 mtime 设成 50 分钟前。`list_undelivered` 之后，该运行为 `failed`、`error` 含 stalled、进程已结束。最近有输出的运行不受影响。
- B：
  - 主控作业抛出 `Insufficient Balance` 后，会话记录有 `fault=provider`，暂停文件里出现该驱动；
  - `clear_driver_suspension` 之后，`suspended` 席位收到跟进，`stopped` 席位没有；
  - 失败的主控被拉起一次，用打桩的 `launch_problem_session` 验证 `resume_id` 和目标；
  - 第二次解除暂停不会再拉起。
- C：打桩一个 `session/load` 返回错误的 ACP 客户端，发出的第一条 prompt 以交接文本开头，会话记录 `resume=fresh`；load 成功时不加交接。
- D：
  - 第一个实例持有实例锁时，第二个 `run_daemon_server` 直接返回，没有绑定端口；
  - `start_daemon` 遇到 pid 活着、健康检查还没通过的状态时，不会再拉起新进程。

## 6. 发布

版本 4.4.8，需要重启守护进程。重启时运行的还是旧代码，所以 D 的单实例保证从这次重启之后才生效：重启前先退出应用。

## 7. 实现记录

- A：`services/test_runs.py`：
  - `reap_stalled` 与 `_kill_group`（`cancel` 也改用它）；
  - 由 `list_undelivered` 调用，每个工作区每分钟最多扫描一次，因为每次扫描要读所有在跑运行的日志；
  - 阈值为 `test_runs.stall_minutes`，默认 45 分钟。
- B：
  - `drivers/registry.py` `_record_failure`：失败原因写进会话记录，供应商故障时调用 `suspend_driver`；
  - `agents/manager.py` `resume_after_outage`，由 `clear_driver_suspension` 调用，结果存在 `last_outage_resume`；
  - `resume` 接口返回 `resumed_seats` 与 `orchestrator`；
  - 会话记录新增 `error`、`fault`、`resume` 三个字段（`drivers/base.py`、`session_store.update_session`）。
- C：
  - `run_acp_headless_turn(handoff=)`，DSH 与 Antigravity 两个 ACP 驱动都传 `self.resume_handoff`；
  - `drivers/brief.py` `orchestrator_handoff` 只给计数和 agent id。
- D：`daemon/server.py`：
  - 实例锁 `daemon.instance.lock`；
  - 绑定端口后先写 `starting` 状态；
  - 判断「正在启动」时还要求实例锁被持有，防止状态文件里的 pid 已被别的进程复用；
  - 健康等待改为 30 秒，停止等待改为 15 秒。
- 测试：
  - `test_outage_recovery.py`（6 个）、`test_daemon_single_instance.py`（2 个）、`test_test_runs.py::test_reap_stalled_*`；
  - 新测试在未修复的代码上都失败；
  - 涉及模块的 16 个测试文件共 145 个用例通过。
