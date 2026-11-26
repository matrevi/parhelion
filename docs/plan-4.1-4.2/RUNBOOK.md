# 无人值守续跑手册（2026-11-26 起）

主会话在上下文压缩、5 小时用量限额或定时检查之后，按本文件继续推进 4.1 + 4.2。总计划与规则见 [00-MASTER.md](00-MASTER.md)：§1 是落地顺序，§1.1 是分组，§2 是子代理守则，§3 是合并流程，§4 是进度表。本文件只记「现在到哪、下一步做什么」。

## 0. 每次醒来先做

1. **上下文刚压缩过**：先 `aoci_rules`，再做一次完整的 `aoci_overview`，带 `refresh_reasons=["context_compaction"]` 和新的 `refresh_event_id`，按 cursor 读完并提交 Attestation。
2. **盘点**：
   - `git -C <repo> status`、`git -C <repo> log --oneline -5`、`git -C <repo> rev-list --left-right --count main...origin/main`；
   - `git worktree list`，以及每个工作树的 `git log --oneline main..HEAD` 与 `git status --short`；
   - `ListAgents` 查还在跑的子代理。agent id 就是 SendMessage 的地址。
3. **对照本文件 §2 的状态表**，找出「已交付未合入」与「该派未派」的包。
4. **什么都没变**（代理都在跑、无新交付）：直接结束本轮，不发消息，不重复派发。

## 1. 硬约束（摘要，全文见 00-MASTER §2）

- 不改自动提交开关，不提交、不 force、不 approve。额度不进任何门禁阈值或暂缓理由。
- 回放数据只读：`mesh-dee-*.jsonl`、`~/.dsh/sessions/**`，以及比赛工作树与分支。
- 子代理默认用 Sonnet。某个包的 Sonnet 代理被安全过滤中止后，才对该包改用 Opus。到目前为止，Sonnet 在 P01、调研、P17、P06 上都被拦过。组 4 仍先用 Sonnet；被拦的包改用 Opus。
  - 简报不写设备厂商字眼，验证类工作用「正确性与测量可信度」的说法；
  - 会话日志只做聚合统计；
  - 同时最多 3 个子代理；
  - 每 5 分钟最多派 1 个。
- 子代理的工作树由工具按 `origin/main` 切出，所以第一步必须 `git checkout -b <分支> main`（或指定的基线分支）。
- 每合入一个包：两条全量测试（`swift build` 视改动而定）→ 更新 00-MASTER §4 → `aoci scope acknowledge --reviewed-by claude`（有新测试文件时）→ `aoci_maintain` 直至 aligned → 提交索引 → `git push origin main`。
- 中途不重启 daemon，最后统一重启一次，见 §4。

## 2. 状态表（随进度更新）

> **2026-11-26 决定：只把代码留在分支，不执行合入；组 4 暂缓。** 用户要求本阶段不合入任何包分支（`main` 停在 `9e504c6`），第 4 组（P12/P13/P14/P08/P15）先不做。下表的「已完成」一律指**分支上完成、未合入**。合并流程（§3）与 §4 收尾等用户指示后再启动。

| 包 | 分支 | 状态 | 下一步 |
|---|---|---|---|
| P01 P02 P02b P03 P04 P05 | — | 已合入 | — |
| P06 | impl/P06-ledger-seed | 已完成（3 提交：6460f8f / 166112f / 25ca29c），未合入。根目录 582 passed / 18 skipped，`cli+tests` 377 passed；回放 `ledger_full` 0.6419 与 `current` 打平（CI [−0.032,+0.032]），默认仍 `ledger_core`，报告 `reports/P06-replay.md` | 等合入指示 |
| P07 | impl/P07-gate-queue | 已完成（分支），未合入 | 等合入指示 |
| P07b | impl/P07b-no-closing-reserve | 已完成（分支），未合入。另有一点：日配额用尽时仍判放行，只等待名额，与用户规则一致 | 合入时先 P07 再 P07b。遗留：`available_normal_shots` 仍扣除预留，因此显示偏低；`drivers/brief.py:188` 仍写着 quota gates，留给 P08 |
| P09 | impl/P09-host-daemon | 已完成（分支，含主机检查报告），未合入 | 等合入指示 |
| P10 | impl/P10-profiler-ab（叠在 P09 上） | 已完成（分支），Edge 复测已补（b10a8ff），未合入 | 等合入指示 |
| P11 | impl/P11-batch-ui（叠在 P10 上，已合并当时的 main） | 已完成（分支），组 3 结束。合并批次在本主机上没有可测的节省（作业间切换约 1ms），开关保持开启 | 合入后要补界面截图（需 daemon 重启） |
| P17 | impl/P17-v2-calibrate（05ca090，基于 be22f86，含 P07、P07b 与 main） | 已完成（分支）：R1–R3 全部完成，真机两项检查通过，无误报 | 等合入指示。`impl/P17-v2` 停在 be22f86 已无用，`impl/P17-verify-calibrate` 弃用，收尾时都删除 |
| P12→P13 | impl/P12-report-views → impl/P13-bound-output | 暂缓（用户要求组 4 先不做）。P12 只做完了前置聚类：2ef788a 加 `reports/P12-script-clusters.md`（672 次临时脚本、16 桶，A 组 441 次可被视图替代） | 等用户指示再开工 |
| P14→P08→P15 | impl/P14-peer-protocol → impl/P08-startup-pack → impl/P15-worker-boundary | 暂缓（组 4 先不做） | 等用户指示 |
| P16 | — | 未开始 | 合入与组 4 完成后再由主会话决定 H/I/J，依据 P03 基线：缓存命中已有 99.3–99.6%，H 价值低 |

## 3. 合入顺序（00-MASTER §1）

已合入：P01 → P02 → P02b → P03 → P04 → P05。待合入（**当前暂停，见 §2 顶部决定**）：

P06 → P07 → P07b → P09 → P10 → P11 → P17 → P12 → P13 → P14 → P08 → P15

2026-11-26 曾按原计划试跑合入链：P07、P07b、P09 已合到临时分支、在 P10 冲突处停下；因用户改为「只把代码留在分支」，该临时分支与工作树已删除，`main` 未被触碰（仍在 `9e504c6`）。

**预计会冲突的位置**
- `scheduler/gate.py`：P07、P07b、P09（#20 编译检查钩子）、P10（S2 证据顺序）、P17 都改了它。
- `scheduler/case_bench.py`：P09 与 P10。
- `agents/manager.py`：P03（已在 main）、P04、P05、P06、P14。
- `mcp/schemas.py` 与 `mcp/tools.py`：P04、P06、P07、P12、P13、P14、P08。

合并时逐个解决冲突，每合一个就跑两条全量测试。

## 4. 收尾（未启动）

> 按 §2 顶部决定，合入与收尾都等用户指示后再做。下列步骤保持原样。

1. 全部合入后：
   - 重启 daemon：`opsb daemon stop`，再 `opsb daemon start --background --workspace /Users/winbeau/Competitions/mat-chem-sim-pred --host npu --port 18910`。追溯事件已预先登记（seq 2304）。
   - 确认 `stream=True`、hot mesh 带上缓存字段、`opsb daemon reload` 可用。
2. 用 P03 的 `opsb replay metrics` 与 `opsb replay bench` 给出 4.2 前后对比，决定 H/I/J。
3. 版本号升到 4.1/4.2：改 `cli/opsb_core/__init__.py` 与 `scripts/bundle_app.sh` 的两个版本字段。在 `docs/v4/RELEASE-NOTES.md` 写发布说明。
4. `scripts/bundle_app.sh` 打包。安装前要先退出正在运行的 Parhelion.app。
5. 提醒用户开新会话：新会话用 aoci 恢复认知，比拖着长上下文便宜。

## 4a. 定时检查

本会话设有心跳：每小时 :13 和 :43 各一次，只在会话空闲时触发，会话结束即失效。收尾第 5 步之后，用 `CronList` 找到它，再用 `CronDelete` 删除。会话被关闭后若要继续，开新会话按 §0 盘点即可。

## 5. 设备主机

主机是容器。重启会清空 `/tmp`，用例缓存随之丢失，首轮准备会变慢。用户睡前机器在线，但可能停机。SSH 不通时，真机步骤标为待补，每 30 分钟最多检查一次。
