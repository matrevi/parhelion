# Parhelion 发布说明

> 设计：[00-OVERVIEW.md](00-OVERVIEW.md) · 详细设计：[10-DETAILED-INDEX.md](10-DETAILED-INDEX.md)

## 4.2.0 — 上下文经济（第二部分）与 worker 边界

本版完成 4.2 的 D、E、F、G、K 与 J 的两项（含 4.1 #15），以及 4.1 遗留的小修。H（遮蔽旧工具输出）与 I（预算护栏）经 P16 测量后不做。各包的计划、报告、审查与合入提交见 `docs/plan-4.1-4.2/00-MASTER.md` §4、`reports/` 与 `reviews/`。为 4.3 做的调研（上一场比赛的涨分归因）在 `docs/v4.3/research/antigravity/`。

### 平台视图与输出上限（D、E；P12、P13）
- **`parhelion_report`。** 七个视图：`per_case`（逐例差距、三态判定、映射、正确性、复现标记）、`diff`（两个 SHA 的逐例比与区间，优先读已有的配对运行）、`history`、`queue`、`board`（只读缓存，不联网）、`bottleneck`、`env`；`view="help"` 返回各列含义。用来代替读 `report.json` 与回执日志的临时脚本。
- **统一输出上限。** 每个 MCP 工具回复不超过 `mcp.output_cap_bytes`（默认 5000 字节）。列表类工具先分页（`offset`/`limit` 或 `next_cursor`），仍超限时把全文写到 `<workspace state>/spill/`，回复只带开头、结尾与路径，用 `parhelion_spill_read` 读其余部分。`parhelion_status`、`parhelion_kb_query`、`parhelion_test_wait` 与测试结果消息支持 `format=brief|full`。按 11-26 冲刺回放，平台 MCP 回复字节减少 59%。
- `parhelion_kb_query` 的回复不再重复携带与 `results` 相同的 `entries` 列表（原占简要回复的 48%）。

### 同伴消息（F；P14）
- `parhelion_squad` 改为先写结论的信封：claim（≤280 字）、账本引用、证据路径；正文超过 1200 字时转为附件，消息只带路径。同一收件方的多条消息在它的回合结束时合并成一次投递；管理员指令与判题反馈仍立即投递。按回放，同伴消息字节减少 69%，阶段结果字节减少 81%。

### worker 启动包与 DSH 插件（G、4.1 #15；P08）
- worker 开局拿到由代码生成的启动包（约 4.5–4.9KB）：任务契约、工作树绝对路径与「用 `workdir`、别用 `cd && …`」的规则、要改的文件与行号段、本题账本摘要与逐例证据。仓库地图按调研结论默认关闭（`startup_pack.repo_map`）。
- 新工具 `parhelion_agent_peek`（仅主控）：查看 worker 工作树的文件、会话流水尾部与 `git diff --stat`。
- DSH 插件按 Agent 类型给角色：比赛根目录的主对话是主控，`.worktrees/*` 下的对话与原生子代理是 worker，识别不了的一律按 worker。

### worker 边界（K；P15）
- worker 进程的环境去掉平台私有变量与指向平台源码的 `PYTHONPATH`；在 macOS 沙箱里禁读判题账号、会话文件、守护进程 token 与远端判题缓存。
- worker 读取平台内部状态、导入平台源码、打开判题凭据或调用全队会话时，给主控与管理员各发一条告警（同一 worker 同一类只发一次），在协作页标记该 worker，并记一条账本约束；不会自动停止 worker。按回放扫描，上一场 13 个 worker 中有 11 个共越界 130 次。

### 工具表瘦身（J；P20）
- 平台工具表（每次模型调用都会带上）：主控 25,007 → 14,601 字节，worker 22,066 → 12,507 字节，测试钉住上限 15,000 / 13,000。

### 小修（P18）
- 显示的 `available_normal_shots` 不再扣除收尾预留，预留单独列在 `closing_reserve`；门禁不变。
- 设备代理的两条预算测试改用虚拟时钟，不再在机器高负载时偶发失败。
- 新配置 `device.case_cache_dir`、`device.case_tmp_dir`：把用例缓存放到持久盘，两者不在同一文件系统时会提示并改为复制。

### 升级
1. 重启守护进程并重新安装应用，MCP 改动随守护进程生效。DSH 插件的角色改动要等 Parhelion 下一次经驱动启动 DSH、改写 `~/.dsh` 下的插件块时才生效。
2. 新增配置项都有默认值：`mcp.output_cap_bytes` / `spill_keep_days` / `spill_max_mb`、`startup_pack.tokens` / `repo_map`、`device.case_cache_dir` / `case_tmp_dir`。
3. 凭据放在 `cj_accounts.json`，不要写进 `config.json`。`config.json` 一旦出现凭据形状的键，就会对 worker 整体禁读。
4. `opsb doctor clean` 会列出过期的 spill 与同伴附件，`--purge-retention --apply` 才删除。

### 已知限制
- 4.2 的目标（worker 起步上下文 ≤15K、上下文峰值中位 ≤150K）目前只有回放估算，要在下一场冲刺实测。
- 原生子代理拿到 worker 角色但没有绑定的 worker 身份，多数 worker 工具会拒绝它们。
- Codex 的权限配置块没有在真实 codex 上验证；分离式启动的会话读不到远端判题缓存。
- worker 契约离 1.5KB 上限只剩约 35 字节，更长的题名会截掉最后一行。
- `device.case_cache_dir` / `case_tmp_dir` 没有在设备主机上验证。

## 4.1.0 — 判题一致性、可信的本地测量与上下文经济（第一部分）

本版完成 4.1 路线 1–4 与「4.1 待办」#5–#14、#16–#20，并提前带上 4.2 的工作包 A、B、C（计量、变化驱动的等待、实验账本）。#15 随 4.2 的 P08 交付。各包的计划、报告与合入提交见 `docs/plan-4.1-4.2/00-MASTER.md` §4 与 `reports/`。

### 判题一致性（#5–#8，P01、P02）
- **回执归一。** `scheduler/receipts.py` 是回执的唯一读口：规范化官方分（未刷新最高分的 `timing_score.score` 也读得到），按 `submission_id` 去重，并给出判题计分公式。`opsb bench receipts backfill` 只用磁盘已有数据补写历史行。
- **用例映射。** `scheduler/case_map.py` 把本地 official 用例对应到判题用例，并标出规模不同的用例（如 FFT 05，是不同的工作量，标 `scale_mismatch`）。`opsb bench case-map` 查看或重建。
- **按回执逐例预测。** `scheduler/receipt_predict.py` 用已判 SHA 的官方逐例时间和本地配对比，按判题公式与最新 tbest 逐例计分。留一回测 MAE 从 2.75 降到 1.32。
- **WA 复现。** WA/RE 回执到达时，`scheduler/wa_repro.py` 为失败用例生成多 seed 变体和精确舍入平局变体，作为逃逸锁的复现入口（`opsb bench wa-repro`）。真机上 QuantScaleCalib a2717b5 在平局变体上复现 WA（24/8320 个元素错），榜上 SHA 全部通过。配对改按 official 输入的 `pairing_key`，追加 final 变体不再让旧证据失配。

### 本地测量可信度（路线 1–2，P10、P17）
- **profiler 任务时长。** `standard` 与 `final` 默认在 profiler 下计时（`device.profiler_levels`）；持锁期间只采集，导出和解析在锁外完成。同一 SHA 重复运行的计时 CV 从 61% 降到 0.9%。
- **A/B 交替配对。** `opsb test --compare <base|board>` 与 `parhelion_test(compare=…)` 在同一个设备作业里交替运行候选和基线，S2 优先使用配对证据。
- **计时归属与未公开变体。** 改动模板主程序、`sample.py` 或 `reference.py` 的运行只供诊断，不算正式证据；每次 `final` 运行派生只存在于本次运行的 seed、scale、shape 变体，不写入 `suite.json`。
- **物理合理性。** 低于带宽下限（`device.peak_bandwidth_gbps`）、比同输入此前最好快 3 倍以上，或超过历史最大跃升 2 倍的读数标为 implausible，在冷会话复测确认前不进入门禁证据。
- **校准表与三态判定。** 逐例记录本地预测与判题变化，给出 trusted / noisy / divergent 标签；门禁估分带区间，divergent 用例按未知处理。逐例判定为 improved / indistinguishable / regressed，CV 超过 3% 的用例只供查看，不作增益结论。

### 门禁与队列（#9–#12，P07、P07b）
- 队列灯、badge、理由和下一步只从门禁判定读取，不再本地重算；没有判定时显示 NOT EVALUATED。
- S4 只决定派发时机（账号槽与冷却），不再拒绝或暂缓候选；收尾预留只用于显示。
- `opsb queue approve` 如实报告入队结果；拒绝卡只显示判题返回的消息，完整堆栈写到 `logs/submit-errors/`。
- 新 MCP 工具 `parhelion_escape_status`、`parhelion_escape_rebind`、`parhelion_escape_reproducer`；每题保留前 3 名不同思路的候选档案。

### 编排与上下文（#13 #14，4.2 A B C；P03–P06）
- **计量与回放。** 按驱动解析提示缓存命中与未命中原因，记录每次 MCP 工具调用的字节与耗时；`opsb replay metrics` 与 `opsb replay bench` 只读地重算冲刺指标、跑压缩回放基准。本场缓存命中率已是 99.3–99.6%，成本杠杆在上下文大小。
- **变化驱动的等待。** `parhelion_wait` 阻塞到可见事件再返回，事件到返回 p50 262ms；`parhelion_status` 支持 `if_rev`，未变化时返回 `not_modified`。按本场回放，主控调用从每小时 136 次降到 67–91 次，单次返回中位数从 6.7KB 降到约 200B。
- **实验账本。** 测试终态、门禁请求和判题回执由平台自动写入只追加账本；worker 用 `parhelion_note`、`parhelion_ledger`、`parhelion_recall` 记录与查询。重测同 SHA 同级别、或与近期实验近似重复的改动，会先返回已有结果和账本 id，`force_rerun` 加书面理由才重测。同一失败指纹或瓶颈类达 3 次、或连续 5 次实验没有改进时给出警告（不阻断）。
- **压缩。** 交接叙述按五个固定标题校验，失败时回退到只用账本的种子；低于硬上限时只在测试终态、判题回执或阶段报告等边界上压缩，工具调用进行中不压缩。代码拼装的种子 `agents.seed_scheme=ledger_full` 已实现，但回放中与现方案打平，默认仍为 `ledger_core`。
- **扩展状态卡。** `parhelion_status(detail=true)` 返回逐例同代加速比与置信度、与 tbest 的差距、最近回执和期望收益最大的目标，与门禁读同一套数据。

### 设备主机、守护进程与界面（#16–#20，路线 3–4；P09、P11）
- 用例缓存加全局 LRU 上限（默认 24GiB，`device.case_cache_bytes`）和 7 天过期；`opsb doctor --cases [--remote]` 显示占用与命中率，`opsb doctor clean --purge-cases` 清除。
- `standard` 运行在前几个计时用例都明显慢于基线时提前停止，`final` 始终完整运行。不同题已就绪的作业可合并在一次持锁内（`device.merge_batches`）。
- 守护进程在派发间隙热加载 `ipc_bridge` 与 `daemon.dispatch`，也可手动 `opsb daemon reload`，不必重启、不中断 worker。总线清空后消息序号从 1 重新开始。
- 上传前可用判题版本的工具链做一次只编译检查（`device.judge_toolchain_root` 等），失败时 S1 以编译器错误行拒绝。
- 界面：测试登记表（可展开 digest）、驱动暂停横幅与解除、队列卡的门禁 badge 与下一步、协作页缓存命中率、菜单项「New Contest Chat」（⌘⇧N）。

### 升级
1. 重启守护进程并重新安装应用；新的设备代理、profiler 采集与用例缓存模块会在第一次测试时自动部署到设备主机。
2. 新增配置项都有中性默认值：`device` 段的 `profiler_levels`、`merge_batches`、`case_cache_bytes`、`judge_toolchain_root` / `judge_toolchain_env` / `judge_compile_flags`、`peak_bandwidth_gbps`；`agents` 段的 `seed_scheme`、`seed_budget_chars`、`compact_floor_percent`。
3. 历史回执需要规范字段时，先运行 `opsb bench receipts backfill` 查看，再加 `--apply` 补写。

### 已知限制
- 设备主机上没有判题版本的工具链，只编译检查默认跳过。本场线上失败都发生在编译通过之后（RE、WA、TLE），这项检查拦不住它们。
- 跨题合批在这台主机上没有可测的节省，`standard` 早停只有单测。
- 校准数据很薄：14 题中只有 4 题有可配对回执，两题达到 trusted。
- 设备主机是容器，重启会清空 `/tmp` 里的用例缓存，首轮准备会变慢。
- 显示用的 `available_normal_shots` 仍扣除收尾预留，数字偏低（不影响门禁）。
- `runtime/tests/test_device_broker.py` 两条基于 sleep 的预算测试在机器高负载时偶发失败，单独运行通过。
- 4.2 的 D（平台视图）、E（统一输出上限）、F（同伴消息协议）、G（启动包，含 #15）、K（worker 边界）尚未完成，H、I、J 待测量后决定。

## 4.0.4 — 修复 NPU 遥测断流

4.0.1–4.0.3 之后 `opsb npu status` 正常，但 daemon 的常驻遥测流一直连不上，界面显示 NPU 离线（`"error": "SSH stream closed (rc=None)"`）。两个缺陷叠加：

- **远端探针版本标记过期。** daemon 用 `OPSB_STREAM_PROBE_V5` 标记判断远端副本是否可用，旧副本没有该标记，远端命令直接 `exit 42`。自动重部署分支因此不可达，因为它依赖退出码 42：读线程先看到 stdout EOF，主线程随即 `poll()` 拿到 `None`，42 被丢掉。现在改为先 `wait(timeout=2.0)` 收割真实退出码，超时才回退 `poll()`。
- **设备事实没有随命令下发。** 探针是独立副本，读不到本机 `~/.parhelion/config.json`，`driver_root`、`dcmi_lib`、`driver_libs` 全为空，`libdcmi` 预载失败后退化成 `npu-smi`，SoC 变成占位值。现在 `device_env.device_env_exports()` 把所有标量与结构化设备设置渲染成 `OPSB_DEVICE_<KEY>` 前缀，随 ssh 命令下发；字典与列表走 JSON，探针侧的独立解析器同步支持 JSON 覆盖。仓库仍然零厂商字面量，设备值只存在于 `config.json` 与运行时环境。

修复后 `probe_backend=dcmi`、`soc_name` 正确、`stream_connected=true`，采样约 40ms，热帧构建约 5ms。新增 `cli/tests/test_device_env_exports.py` 覆盖结构化设置的导出与探针侧解析。

## 4.0.3 — 修复清理误伤与索引校正

### 修复
- **厂商名清理的误伤。** 那次替换把 "cannot" 里的子串也换掉了，留下 28 处 `sdkot`，分布在 18 个文件的报错文本、注释、文档和测试里，现已还原。同一次清理还把 `fast_profiler.py` 里 profiler 的运行时追踪参数改成了 profiler 不认的名字，并把程序名写死为占位符。现在两者都和 `npu-test.py` 一样，从 `OPSB_DEVICE_PROFILER_BIN` 和 `OPSB_DEVICE_PROFILER_ACL_FLAG` 读取，也可以用新参数 `--profiler-bin` / `--acl-flag` 指定。
- **`opsb doctor clean --agents` 不再回收 `error` 状态 worker 的沙箱。** 这类席位还能通过显式 followup 续跑，Codex 续跑还要用沙箱里的 `codex_home`。现在它们默认只列出（显示为 `agent error (resumable)`），加 `--include-errored` 才会回收。
- **`AGENTS.md` 的冲刺启动段落与 4.0 默认值对齐。** 默认每题一个 solo 席位，`dual` / `full` 需要显式开启；没有校准席位时，官方回执投给提交该 SHA 的 worker。

### 仓库认知索引
- `services/test_runs.py` 是 `parhelion_test` / `parhelion_test_wait` 背后的登记表，但因为文件名以 `test_` 开头被归入了观察层。现在通过项目 scope 规则把它纳入索引，并补写了条目。
- 修正了 4 条描述与源码不符的条目：
  - `status_cmd`：`--compact` 只去掉缩进，2KB 上限在 MCP 那边；
  - `__init__`：`bundle_app.sh` 的版本号写死在脚本里，需要手工同步；
  - `bundle_app.sh`：默认只在仓库外构建，加 `--install` 才安装；
  - `fast_profiler`：目前是手动工具，平台代码不调用。
- 索引共 248 条；`check` 0 个问题，`guide` 为 aligned / complete。

## 4.0.2 — 重建仓库认知索引

4.0.0 主动删除了旧的 AOCI 索引以便重建。本次按 AOCI 的正式流程从零建立仓库级认知层：`aoci init` → `aoci scan`（294 个受管文件，其中 247 个需要条目）→ 分批创作 → 对齐。

- 索引共 **247 条**条目，覆盖 `Sources/`（Swift 前端）、`cli/opsb_core/`（CLI、daemon、MCP、驱动）、`scheduler/`、`runtime/`、`docs/`、`templates/`、`scripts/` 与根文件。
- 每条按紧凑标签 `A+B+C+E` 与 F/R/A/S 语义书写；S 只记不可推断且能防错的约束（如：inflight 文件是账号提交的唯一锁、`cleanup_stopped` 的判定谓词被 doctor 复用、stdout 只留给 JSON-RPC 协议流）。
- 测试文件（`cli/tests/**`，44 个）保持在观察层，不单独建条目，与既有 scope 策略一致。
- 终局验证：`aoci verify` 结构与治理均对齐；`aoci check` ok、0 findings；`aoci guide` 为 `stage=aligned, complete=true`。
- `aoci init` 会重写 `AGENTS.md` 的托管块并留下 `*.backup.*` 副本，后者已加入 `.gitignore`。

## 4.0.1 — 卫生修复

4.0 的清理只覆盖了"测试污染的工作区目录"和"远端死票"，两条平台自己写入的路径因此无界增长：每个 worker 的 `workspaces/<id>/agents/<agent_id>/`（各带一份 ~100MB 的 `codex_home`，实测 175 个共 1.0GB）与驱动会话流水（125MB，其中 90% 来自单日）。本次补齐回收，并修正一处权限漂移。

### `opsb doctor clean`

- `--agents`：回收已结束 agent 的沙箱。判定与 `AgentBusStore.cleanup_stopped` 完全一致（`stopped`/`error` 且 `pid`、`pgid` 均已清空），数据库里无记录的孤儿沙箱一并回收。登记工作区的**状态目录本身仍不移动**，只回收其 `agents/` 下已被证明结束的沙箱；数据库不可读时只列出、不移动。`--apply` 移入 `trash/<UTC>/agents/<workspace>/<agent_id>`。
- `--logs DAYS`（默认 14 天）与 `--logs-max-mb MB`（默认 100，`0` 关闭）：驱动会话流水的时间窗 + 总量封顶，按 mtime 从新到旧装满预算，装不下的更旧文件回收。这类流水属平台诊断 telemetry，与远端死票同类，`--apply` 直接删除；用户数据仍只移动。
- `--purge-trash`：唯一会删除 trash 的操作，需显式指定，用于真正释放前两步腾出的空间。
- 默认仍是只列出不动手；`--json` 在带对应开关时追加 `agents` / `logs` / `trash_purge`。

### 配置权限

- `ConfigRepository.save()` 在原子替换前 `chmod 0600`：迁移表承诺 `config.json` 为 0600，但运行期保存会掉回 umask。

### 实测回收

- `workspaces/` 1.0GB → 5.2MB，`logs/` 125MB → 100MB，`~/.parhelion` 约 1.2GB → 144MB。

## 4.0.0

### 测试平台
- 每题一个平台持有的版本化套件 `suite.json`；用例是最小单元，桶只是标签。
- 三个级别 `smoke` / `standard` / `final`。standard 运行可以作为提交网关的正式证据。
- 默认对全部官方例计时：丢弃 warmup 样本，报告热态最小值和中位数；套件可以用 `timing_scope=perf` 缩小计时范围。
- 每次运行生成不超过 2KB 的 digest，包括：
  - 逐例判定和失败摘要；
  - 计时结果，以及本次运行相对该 SHA 最好成绩的漂移和慢档标记；
  - 公开例效率，分为扣除计时开销后的值和原始值；
  - 相对最佳官方 SHA 的几何平均提速。
- 计时开销校准：用官方对同一 SHA 的逐例时间，标定本地事件计时的附加开销，并缓存到 `results/.timing-offset.json`。
- worker 不能再传 profile / experiment / timing / repeats / runner / bucket。

### 设备主机
- 常驻设备代理按 WSPT 顺序串行执行作业并独占设备锁；TLE 后释放锁，把剩余用例续排；代理启动时清理死票。代理不可用时回退到 3.6.2 的车道逻辑。
- 探针把代理识别为持锁者，探针版本标记升到 `V5`，设备主机会自动重新部署。

### 控制面与 MCP
- 新工具 `parhelion_test`、`parhelion_test_wait`、`parhelion_test_cancel`。`parhelion_test_plan` 保留为兼容别名。
- 测试运行登记表放在 `state/test_runs/`：相同请求会加入正在进行的运行，或复用 30 分钟内的结果；结束后作为 `test_result` 消息投递到请求者的收件箱。
- stdio MCP 服务改为多线程分发，回复可以乱序返回，长调用不再挡住其他请求。
- 输出上限：`status` 默认不超过 2KB；`kb_query` 默认返回 3 条、每条摘要不超过 600 字符；`bench_diagnose` 精简。传错 SHA 时，报错里列出最近 5 个提交。

### 编排
- 冲刺默认每题 1 个 worker（`mode=solo`），`dual` 和 `full` 需要显式开启。
- 供应商故障熔断：余额、配额、401/402/429 错误会暂停该驱动，只发一次告警，并停掉同驱动的 worker；管理员显式派发时自动解除暂停。
- 压缩阈值随模型窗口自适应，取窗口的 30%，限制在 12 万到 40 万 token（`agents.compact_tokens` 可以覆盖）；不再要求先有阶段结果。
- 不超过 2KB 的状态卡用于 brief 和压缩检查点；worker brief 不超过 1500 字节。
- squad 支持按 agent_id、角色名或 `all` 寻址；报错时附上队友列表。
- 不再唤醒 error / stopped / 已暂停驱动的 worker。
- 修复：`allow_claude_workers` 配置项从未被读取。

### 卫生
- 测试使用临时的 `PARHELION_HOME`，跑前跑后真实目录数不变（293 → 293）。
- 网关判定只在变化时写日志；`feedback-log.jsonl` 超过 8MB 自动轮转。
- 工作树路径的 `workspace_id` 归并到所属比赛工作区，不再为每个工作树新建一个数据目录。
- 新命令 `opsb doctor clean [--remote] [--apply] [--json]`，把测试残留移到 `~/.parhelion/trash/`，并清理设备主机上的死票。

## 迁移
1. 可选：`opsb bench suite migrate <Problem>`，把旧的 `case-profile.json` 显式转成 `suite.json`。不转也能运行，旧 profile 会在内存里自动转换。
2. `opsb doctor clean` 先看一遍列表，再用 `--apply` 清理。
3. 已安装的 3.x 应用在重新安装之前，仍然使用旧的运行时。新的设备代理会在第一次 4.0 测试时自动部署到设备主机。

## 端到端实测（2026-11-25，真机）

| 用例 | 结果 |
|---|---|
| E1 全量测试 | 378 通过 / 18 跳过；测试前后 workspaces 目录数不变；Swift 编译通过 |
| E3 smoke（RevINNorm best） | 端到端 22s，代理已启用，等待 0.001s，持锁 3.9s |
| E4 standard | 15/15 通过，端到端 31–35s，持锁 2.9–3.9s，formal 证据成立 |
| E5 复用 | 相同请求 0.4s 返回 `reused` |
| E6 三题并发 | 端到端 31/38/70s，排队 ≤3.3s，串行执行，零残留票据 |
| E7 MCP 全流程 | `parhelion_test` 0.02s 返回；运行期间 `status` 1.07s 返回（1.8KB）；wait 33s 得到 15/15；网关预览 0.3s |
| E8 清理 | 本地 290 个残留移入回收目录；远端死票 0（代理启动时已清理 23 个） |

## 已知限制
- **事件计时带有主机侧启动延迟。** 同一 SHA，小内核在不同运行之间、甚至同一次运行的不同用例之间，读数会在约 5µs 和约 25µs 之间跳档；10µs 以上的内核与官方相差约 2%。4.0 的应对办法：比较时使用该 SHA 的最好成绩，扣除标定出的开销（实测约 2.4µs），并标记慢档运行。真正的修复是 profiler 任务时长（见 4.1）。
- 编排流程只用假 adapter 测过，没有调用真实模型（DeepSeek 余额已耗尽）。ACP / Codex 的压缩路径也没有实机验证。
- 加入同一运行的第二个 worker，能从调用返回值里拿到结果，但收件箱只投递给最初提交的 worker。

## 4.1 路线
1. 把 profiler 任务时长接入 reference 引擎，替换事件计时（最高优先级）。
2. A/B 交替配对，以及多轮轮转计时（把各用例的样本分散到不同时刻）。
3. 跨题合并设备批次。
4. Swift UI 显示测试登记表、digest 和驱动暂停状态。

## 4.2 路线

上下文经济与压缩记忆：变化驱动的等待取代轮询、实验账本与由代码构建的压缩种子、平台视图取代临时脚本、统一工具输出上限、同伴消息协议、worker 启动包与边界。计划、目标与验收方法见 `docs/v4.2/00-PLAN.md`。

## 2026-11-26 冲刺暴露的问题

来源：`mat-chem-sim-pred` 六题冲刺（DSH 主控，14 个 worker），完整过程见 `docs/v4/RUN-LOG-2026-11-26.md`。加权总分从 1047.13 升到约 1071.65。

### 已在 4.0.x 修复（随 4.1 一起发布）

| 问题 | 修复 |
|---|---|
| DSH 桌面对话没有 `parhelion_*` 工具：登记工作区不会挂载 MCP，主控第一步就停住 | 1192ce3：桌面配置安装按 Agent 挂载的插件，比赛目录内的对话自动挂上 `opsb mcp serve`；DSH 热加载，无需重启 |
| 用例准备每次都重算输入和期望输出，每个进程按 32 线程起 OpenBLAS，两题并发即 64 线程挤 32 核；FFT 一次准备约 50s | b2a7d17：按 oracle 与用例摘要缓存在设备主机，只读硬链接给每次运行；未命中的用例并行生成并限制 BLAS 线程。FFT 准备 46s → 0.09s |
| S2 门禁读不到 case-bench 证据，模板题退回 commit 说明估分（sigma 5.68、trust 0.25），并以「保留额度」为由暂缓；被暂缓的一发实得 +5.78 | 599d8cc：改用与榜上 SHA 的逐例配对，按官方回执校准偏差和 sigma；按期望提升 E[max(0, 增益)] 放行，上涨概率不低于 20%；额度不参与门槛 |
| 没刷新最高分的回执只在 `timing_score` 里有分数，同 SHA 历史和校准都看不到，判过的 SHA 会被反复推荐 | 599d8cc：估分器读取 `timing_score.score` |
| 逃逸复核绑定后不能改绑，即使绑定的 SHA 从未提交；worker 只能停下等人删文件 | db67e56：绑定的修复未提交、未排队时允许改绑一次 |
| 设备预算按整个用例（预热加全部重复）计时；FFT 用例 05 单次 7.4s，七次执行必然超过 15s，所有 FFT 候选本地 TLE、S1 全部拒绝 | 77941cb：预算按单次执行计，与判题一致；作业预算按执行次数放大 |
| 秒级用例按默认 2+5 次执行，FFT 一次测试独占设备 66s | 77941cb：单次 ≥2s 的用例只做 1 次预热、2 次计时 |
| 暂缓卡片对所有 S1 拦截都显示「缺少精度记录」，逃逸锁和本地用例失败无法区分 | 77941cb：区分逃逸硬锁、本地用例未通过（理由里列出失败用例）、缺少测试记录。**需要重新打包应用才能看到** |
| 每次 WA 开一把新逃逸锁、每把锁给一次免复现复核，复核可以无限串联（QuantScaleCalib 连续三次 WA） | 77941cb：复核放行的 SHA 再次线上失败后，下一个修复必须先在本地复现 |
| 门禁全部通过的候选若没有 worker 发起提交请求，就一直停在「待提交 · READY」；worker 又在等主控决定，主控忙于调度没有回复（Sgemm 10f21fd 预计 +8.88，空等约 6 分钟） | 自动提交开启时，平台对全部四关通过、且验证完成超过宽限期（默认 120s）仍无人请求的候选代为请求，再由守护进程照常复核门禁后提交；同题有排队或判题中的提交、或回执不到 1 分钟时不代为请求 |

### 4.1 待办

> 状态（4.1.0）：#5–#14、#16–#20 已完成，见本文件顶部「4.1.0」；#15 随 4.2 的 P08 交付。

**判题一致性（影响最大）**

5. **核对本地官方用例与判题用例的对应关系。** FFT 本地用例 05（8×256³）单次执行 7.4s，而判题对榜上 SHA 的公开用例最长只有 8.46ms，相差约 870 倍。要确认本地「official」用例是否就是判题用例，以及 `device_us` 是否把文件读写或 H2D 也算进去。这会直接影响 case-bench 预测和设备占用。
6. **让本地数据能复现数据相关的 WA。** QuantScaleCalib 三次线上 WA，本地所有用例都是零误差。WA 发生在 2048×2048、模式 0 这类舍入平局敏感的形状，而本地每个用例只用一个 seed。需要按判题回执给出的失败用例（testcase id 与形状）自动生成多 seed、富含平局的变体，作为逃逸锁的复现入口。
7. **用回执里的逐例时间直接校准预测。** 回执带有 `tbest_by_case`、`time_by_case` 和计分公式（`judge_log1p5_mean`）。用本地逐例时间配合标定的计时偏移，直接按公式算分，取代 `_project_score_from_speedup` 的经验投影。目前 NeighborList 的本地预测两次分别高估 3.5 和 5.1 分，靠回执偏差校正兜底。
8. **回执分数字段归一。** 在入库时把未刷新最高分的 `timing_score.score` 统一写入一个字段，再排查熔断器、队列视图、校准等其他读 `score` 的地方。

**门禁与队列**

9. **队列视图的展示门禁和真实门禁不一致。** `queue_service.eval_gates` 仍按「增益 ≥ 底线」画灯，S2 已改为期望提升；应直接使用门禁结论。
10. **`opsb queue approve` 的输出自相矛盾。** 已写入覆盖并入队，却同时打印 `enqueued: false / Already queued`。
11. **合规拒绝卡片显示原始 Python 堆栈。** 应只显示判题返回的消息。
12. **逃逸锁缺少 MCP 入口。** 改绑复核、提交复现用例目前只能走 CLI 或手工改文件（本场主控手工重命名过绑定文件）；应提供 `parhelion_escape_*` 工具，并在卡片上给出下一步。

**编排与上下文**

13. **主控和 worker 上下文增长过快。** 后台主控约 10 分钟涨到 136K（51 次 `cd`/`cat`），worker 10 分钟内到 150–193K；新 worker 起步就有 21–44K，大多来自读文件。状态卡应直接带上逐例加速比、最近回执摘要和工作树路径，让第一次调用就够用。
14. **主控手算逐例比例。** 状态卡应给出相对同代基线的逐例加速比和置信度，与门禁使用同一套数据。
15. **DSH 插件的副作用。** 比赛目录下每个打开的对话都会起一个 `opsb mcp serve`（本场 7 个），主控的原生子代理也拿到 orchestrator 角色；改为首次调用时再挂载，并按 Agent 类型区分角色。

**设备主机与界面**

16. **用例缓存的全局上限与可见性。** 现在按题 8GB 上限，FFT 15 个用例就占 2.1GB；需要全局上限，并在 `opsb doctor` 里显示命中率与占用。
17. **「新开对话」只能用指针点击。** 本场 Token Notch 浮层挡住按钮，只能改用等价 CLI；应加菜单项和快捷键。
18. **总线消息序号在清理后继续递增**（AUTOINCREMENT），清理后的第一条消息序号仍接着旧值；只影响观感，可与清理一起重置。
19. **守护进程内的代码改动要重启才生效，而重启会中断 worker。** 本场 `ipc_bridge.py` 的卡片文案和 `dispatch_next` 只能等下次重启；平台代为请求只好放在每次都新起子进程的桥里。需要让守护进程热加载调度相关模块，或把 worker 与守护进程解耦。
20. **本地 15/15 通过、线上 RE/WA 的比例偏高。** 本场 Quant 三次 WA、Sgemm 10f21fd 一次 RE，本地均为零误差。除第 5、6 项外，提交前应在与判题相同的 CANN 版本和编译选项下再做一次只编译不计时的检查。
