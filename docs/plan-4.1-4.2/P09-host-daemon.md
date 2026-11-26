# P09 设备主机、总线与守护进程（4.1 #16 #17 #18 #19 #20）

分支：`impl/P09-host-daemon`。

## #16 用例缓存的全局上限与可见性

现在 `runtime/npu_runtime/case_batch.py` 的 `CaseCache` 按题设 8GB 上限，FFT 15 个用例就占 2.1GB。

- 增加全局上限，默认 24GB，取自 `config.json` 的 device 段并随环境下发（见 `device_env.py`）。超过时跨题按 LRU 淘汰，淘汰最近最少使用的整条用例，保持只读硬链接语义。
- 记录每次准备的命中与未命中，写入设备主机缓存目录下的统计文件。
- `opsb doctor` 新增 `--cases`，配合 `--remote` 显示：每题占用、全局占用与上限、最近 N 次命中率、最旧条目时间。只读，`--apply` 不清理缓存。缓存清理另设 `--purge-cases`，需要显式指定。

## #17 「新开对话」的菜单项与快捷键

本场 Token Notch 浮层挡住了按钮，只能改用等价 CLI：`opsb driver launch ALL --driver deepseek --mode gui`。

在 Swift 主菜单加菜单项，并配快捷键 ⌘⇧N，执行与按钮完全相同的动作。按钮在 `Features/Contest/` 中，定位时搜 `launchOperatorSession` 或 `driver launch`。两处要共用同一个函数，不要复制逻辑。完成后 `swift build`。

## #18 清理后总线序号重置

`agents/bus_store.py` 的 `messages` 表用 AUTOINCREMENT，清理后序号不回落。

- 全量清理（`cleanup_stopped` 的全量路径）之后，如果 `messages` 表已空，就重置 `sqlite_sequence` 中 messages 的值。
- 表里还有消息时不要重置，序号必须单调。
- 客户端的 `since_seq`：worker 收件箱、Swift 热流与 P04 的 hub 都要能处理「游标大于当前最大序号」的情况，此时按重置处理，从头读。
- 与 doctor 的清理谓词保持一致。

## #19 守护进程内的代码改动要重启才生效

本场 `ipc_bridge.py` 的卡片文案和 `dispatch_next` 只能等重启，而重启会中断 worker。

- 让守护进程热加载调度相关的纯逻辑模块，至少覆盖 `cli/opsb_core/ipc_bridge.py`，以及 `daemon/server.py` 中 `dispatch_next` 依赖的派发决策代码。做法自选：
  - 对白名单模块按 mtime 做 `importlib.reload`，在派发 tick 之间进行，失败时保留旧模块并告警；
  - 或者把派发决策移到每次新起的子进程里，守护进程只负责 I/O。
  
  选哪种都要在报告里说明理由。
- 不得重载持有状态的对象（AgentMeshManager、worker runner、SSE 流）。
- 新增 `opsb daemon reload`，以及对应的鉴权路由，用来手动触发。
- 单测：修改临时模块后 reload 生效；reload 失败时回退到旧模块。
- 不要在测试里重启真实 daemon。

## #20 与判题相同的工具链只编译检查

本场 Quant 三次 WA、Sgemm 一次 RE，本地都是零误差。

1. 先查清判题使用的 CANN 版本与编译选项。来源可能是题目模板的构建配置、比赛目录里的工具链说明、判题回执或编译日志。把证据写进报告。仓库里不写真实版本号，版本与工具链路径放进 `config.json` 的 device 段，例如 `judge_toolchain_root`、`judge_compile_flags`，未配置时这一检查跳过并给出提示。
2. 门禁前或上传前增加一步「只编译、不计时」：在设备主机上用该工具链与选项编译候选 SHA。只用 CPU 车道，不进设备锁，复用 `npu-test.py` 的 `--build-only` 路径与 `lanes.compile_lane` 预算。
3. 编译失败时门禁 S1 拒绝并给出编译错误摘要；未配置时不拒绝，只提示。
4. 真机验收：配置可用时，对一个本场 RE 的 SHA（SgemmFp16Cube 10f21fd）跑一次。只编译检查能否发现问题、耗时多少，都写进 `docs/plan-4.1-4.2/reports/P09-compile-check.md`。

## 验收

- 各条的单测见上文。
- 全量测试全绿，`swift build` 通过。
- 真机验收的 #16 `doctor --cases --remote` 输出与 #20 的报告。
