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
- 子代理默认用 Sonnet。某个包的 Sonnet 代理被安全过滤中止后，才对该包改用 Opus。
  - 简报不写设备厂商字眼，验证类工作用「正确性与测量可信度」的说法；
  - 会话日志只做聚合统计；
  - 同时最多 3 个子代理；
  - 每 5 分钟最多派 1 个。
- 子代理的工作树由工具按 `origin/main` 切出，所以第一步必须 `git checkout -b <分支> main`（或指定的基线分支）。
- 每合入一个包：两条全量测试（`swift build` 视改动而定）→ 更新 00-MASTER §4 → `aoci scope acknowledge --reviewed-by claude`（有新测试文件时）→ `aoci_maintain` 直至 aligned → 提交索引 → `git push origin main`。
- 中途不重启 daemon，最后统一重启一次，见 §4。

## 2. 状态表（随进度更新）

| 包 | 分支 | 状态 | 下一步 |
|---|---|---|---|
| P01 P02 P02b P03 P04 | — | 已合入 | — |
| P05 | impl/P05-ledger-core | Sonnet 代理在做（组 2b） | 交付后合入，再 SendMessage 同一代理续做 P06 |
| P06 | impl/P06-ledger-seed | 未开始 | P05 合入后由同一个 Sonnet 代理续做 |
| P07 | impl/P07-gate-queue | 已完成 | P06 之后合入 |
| P07b | impl/P07b-no-closing-reserve | 已完成（Opus）。另有一点：日配额用尽时仍判放行，只等待名额，与用户规则一致 | 随 P07 合入：先 P07，再 P07b。遗留：`available_normal_shots` 仍扣除预留，因此显示偏低；`drivers/brief.py:188` 仍写着 quota gates，留给 P08 |
| P09 | impl/P09-host-daemon | 已完成，含主机检查报告 | P06 之后合入 |
| P10 | impl/P10-profiler-ab（叠在 P09 上） | 已完成，Edge 复测已补（b10a8ff） | P09 之后合入 |
| P11 | impl/P11-batch-ui（叠在 P10 上，已合并当时的 main） | 已完成，组 3 结束。合并批次在本主机上没有可测的节省（作业间切换约 1ms），开关保持开启 | P10 之后合入。界面截图要等 daemon 重启后补 |
| P17 | impl/P17-v2 | Sonnet 代理在做（已含 P07b 与 main） | P11 之后合入。旧分支 `impl/P17-verify-calibrate` 弃用，收尾时删除 |
| P12→P13 | impl/P12-report-views → impl/P13-bound-output | 未开始 | P06 合入后派一个 Sonnet 代理（组 4a） |
| P14→P08→P15 | impl/P14-peer-protocol → impl/P08-startup-pack → impl/P15-worker-boundary | 未开始 | P06 合入后派另一个 Sonnet 代理（组 4b）；P15 等 P12 合入后再做 |
| P16 | — | 未开始 | 全部合入后由主会话决定 H/I/J，依据 P03 基线：缓存命中已有 99.3–99.6%，H 价值低 |

## 3. 合入顺序（00-MASTER §1）

P04 → P05 → P06 → P07 → P07b → P09 → P10 → P11 → P17 → P12 → P13 → P14 → P08 → P15

**预计会冲突的位置**
- `scheduler/gate.py`：P07、P07b、P09（#20 编译检查钩子）、P10（S2 证据顺序）、P17 都改了它。
- `scheduler/case_bench.py`：P09 与 P10。
- `agents/manager.py`：P03（已在 main）、P04、P05、P06、P14。
- `mcp/schemas.py` 与 `mcp/tools.py`：P04、P06、P07、P12、P13、P14、P08。

合并时逐个解决冲突，每合一个就跑两条全量测试。

## 4. 收尾

1. 全部合入后：
   - 重启 daemon：`opsb daemon stop`，再 `opsb daemon start --background --workspace /Users/winbeau/Competitions/mat-chem-sim-pred --host npu --port 18910`。追溯事件已预先登记（seq 2304）。
   - 确认 `stream=True`、hot mesh 带上缓存字段、`opsb daemon reload` 可用。
2. 用 P03 的 `opsb replay metrics` 与 `opsb replay bench` 给出 4.2 前后对比，决定 H/I/J。
3. 版本号升到 4.1/4.2：改 `cli/opsb_core/__init__.py` 与 `scripts/bundle_app.sh` 的两个版本字段。在 `docs/v4/RELEASE-NOTES.md` 写发布说明。
4. `scripts/bundle_app.sh` 打包。安装前要先退出正在运行的 Parhelion.app。
5. 提醒用户开新会话：新会话用 aoci 恢复认知，比拖着长上下文便宜。

## 5. 设备主机

主机是容器。重启会清空 `/tmp`，用例缓存随之丢失，首轮准备会变慢。用户睡前机器在线，但可能停机。SSH 不通时，真机步骤标为待补，每 30 分钟最多检查一次。
