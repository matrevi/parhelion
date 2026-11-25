# 16 · 集成、端到端测试与发布（工作包 E）

> 索引：[10-DETAILED-INDEX.md](10-DETAILED-INDEX.md) · 执行方：集成方（主会话）

## 1. 集成

1. 按顺序合入 `release/4.0`：B2 → B1 → A → C → D。每合一个都跑一遍 `pytest -q cli/tests scheduler/tests runtime/tests`。
2. 在 `cli_app.py` 里调用 `register_doctor_subcommands`。
3. 冲突处理原则：以契约文档（索引 §3）为准；契约本身需要修改时，同步更新文档。
4. 全部合入后跑 `swift build`，确认 UI 能编译通过。新增的 JSON 字段在 Swift 端都是可选解码，不需要改 UI。

## 2. 端到端测试

验收单位是"候选交付 → 测试 → 网关预览"这一完整流程。不做官方提交：提交会消耗比赛额度，属于对外动作，需要用户另行确认。

### 2.1 本地全流程（不需要设备）

- **E1**：全量 pytest 通过；跑测试前后 `~/.parhelion/workspaces/` 目录数不变。
- **E2**：通过 stdio 启动 `opsb mcp serve`（orchestrator 角色），用假的 `opsb test` 脚本（环境变量 `OPSB_TEST_CMD_OVERRIDE` 由 A 支持；如果 A 没有提供，就在测试里 monkeypatch 掉）。验证：`parhelion_test` 在 2 秒内返回；连续发送 `parhelion_status` 不受影响；`parhelion_test_wait` 能拿到 digest。

### 2.2 真机全流程（需要设备主机可达，只做只读的网关预览）

在当前比赛工作区挑一道已经通过的题（以 RevINNorm 为首选），使用它的 best SHA：

- **E3**：`opsb test <题> --sha <best> --level smoke`。确认远端启动了代理（`~/.opsb/broker/broker.pid` 存在），结果 PASS，digest 里有计时。
- **E4**：`--level standard`。记录：
  - 端到端耗时、wait_sec、hold_sec；
  - 是否 `timing_score_eligible`；
  - 公开例 `min_us` 与官方 `public_times_us` 的偏差（目标 ≤ 5%）。
- **E5**：立即再跑一次 standard，应该返回 `reused`；带 `--remeasure` 时新建运行，两次 `min_us` 的差异应 ≤ 3%。
- **E6**：同时对 3 道题发起 standard。确认代理串行执行、没有孤儿票据、`segments` 正确，并记录各自的等待时间。
- **E7**：通过 MCP 走一遍：`parhelion_test` → `parhelion_test_wait` → `parhelion_submit_gate(enqueue=false)`，确认网关预览能读取到新的 formal 证据。
- **E8**：`opsb doctor clean --remote`（dry-run）列出死票；确认后再 `--apply`。

### 2.3 编排流程（不调用付费模型）

- **E9**：用假 adapter 跑通 `start_sprint(mode="solo")`、供应商熔断和测试结果投递。这部分已包含在 C 的测试里，集成后整体再跑一遍。

## 3. 发布清单

1. `cli/opsb_core/__init__.py` 中 `__version__ = "4.0.0"`。
2. `scripts/bundle_app.sh`：`CFBundleShortVersionString` 改为 4.0.0，`CFBundleVersion` 加 1。
3. 写 `docs/v4/RELEASE-NOTES.md`：变更、迁移方式（`opsb bench suite migrate`、`opsb doctor clean`）、已知限制、4.1 路线。
4. 更新 `README.md` 里的测试与协作章节；在 `AGENTS.md` 第 5、6 条补充代理和默认单 worker 的说明（保持原有的九条结构）。
5. 合入 `main`（fast-forward），打 tag `v4.0.0`。
6. 执行 `aoci_maintain`，按 Guide 完成条目维护，单独提交。
7. 不 push，不安装到 `/Applications`；在汇报里给出安装命令，由用户决定。

## 4. 汇报内容

- 每个工作包的交付情况，以及集成时做的修正；
- 测试结果，以及 E3–E8 的实测数据（附命令输出摘要）；
- 没有验证到的部分，以及原因；
- 4.1 的待办。
