# 4.3 实施流程（DSH 执行，主会话审查合入）

日期 2026-11-26，基线是 4.2.1 的 `main`。

本文件交给 DSH 执行：DSH 在 git 工作树里开原生子代理，实现 [00-PLAN.md](00-PLAN.md) §3 的 P21–P29。主会话（Claude）负责审查、合入 `main`、维护 aoci 索引、推送与发布。状态与续跑见 [RUNBOOK.md](RUNBOOK.md)。

## 1. 分工与流程

### DSH 总包

- DSH 主对话在仓库根 `/Users/winbeau/Developer/personal/parhelion` 打开。
- 它负责读计划、建工作树、给原生子代理派活、汇总交付。
- 它自己不改代码，不在主检出目录切分支或提交。

### 每包一个子代理、一个工作树、一个分支

工作树放在仓库外。`main` 先确认已与 `origin/main` 同步：

```bash
git -C /Users/winbeau/Developer/personal/parhelion worktree add /Users/winbeau/Developer/personal/parhelion-wt/P21 -b impl/P21-gate-replay main
```

各包的分支名：

| 包 | 分支 |
|---|---|
| P21 | `impl/P21-gate-replay` |
| P22 | `impl/P22-judge-rules` |
| P23 | `impl/P23-submit-intents` |
| P24 | `impl/P24-escape-health` |
| P25 | `impl/P25-close-sweep` |
| P26 | `impl/P26-sprint-seats` |
| P27 | `impl/P27-brief-broadcast` |
| P28 | `impl/P28-kb-evidence` |
| P29 | `impl/P29-ranking` |

依赖别的包的，等依赖合入 `main` 后，再从最新的 `main` 建分支。

### 子代理的边界

- 只在自己的工作树里改文件，只提交到自己的分支。
- 不推送，不合入 `main`。
- 不改 `aoci*.txt` 与 `.aoci/`。可以用 `aoci_search` / `aoci_get_entries` 定位文件。

### 交付

两件都要有，主会话按这两点识别：

1. 报告 `docs/v4.3/reports/Pxx-<slug>.md`，首行写 `状态：已交付`。内容依次为：
   - 分支与提交（`git log --oneline main..HEAD`）；
   - 子计划逐项的完成情况：已完成 / 部分完成（缺什么）/ 未做（为什么）；
   - 两条全量测试的末行，以及 Swift 是否构建；
   - 真机或真实数据的验证结果；
   - NOTE：发现的非显然约束，或与子计划不一致的源码事实；
   - 风险与后续；
   - 「待决」：需要用户或主会话决定的事。
2. 分支最后一个提交的标题以 `Deliver Pxx:` 开头。

### 返工

- 主会话把审查意见写进 `main` 上的 `docs/v4.3/reviews/Pxx.md` 并推送。
- 子代理用 `git show main:docs/v4.3/reviews/Pxx.md` 读取，在原分支上修正。
- 修正后，最后一个提交的标题用 `Redeliver Pxx:`，报告追加「返工」一节。

### 合入之后

- 依赖它的包从最新的 `main` 新开分支。
- 已经开工、又与新合入内容相关的分支，执行 `git rebase main`。

### 并发与顺序

同时最多 3 个子代理。

1. 先开 P21、P22、P26。
2. P21 合入后开 P23、P24。
3. P23 合入后开 P25、P29。
4. P26 合入后开 P27。
5. P28 插在任一空档。它的第 4 项等 P23 合入后再接。

## 2. 共同守则

### 不可违反

- 不改自动提交开关（`auto_consumer_enabled`，以及 `parhelion_sprint_start` 的 `enable_auto_submit` 语义）。
- 不强制提交，不调用 `queue approve` 与 force，不向判题站提交任何东西。
- 额度不进入任何门禁阈值或暂缓理由。probe 预算是试验预算，与账号额度无关。
- 不重启、不停止守护进程，不派发或停止真实 worker，不清理总线。
- 不运行 `opsb daemon reload`，不安装插件或应用，不写 `~/.dsh`。

### 数据只读

以下数据只读，不移动、不删除、不压缩：
- 会话日志：`~/.parhelion/logs/sessions/mesh-dee-*.jsonl`、`~/.dsh/sessions/**`；
- 总线数据库；
- 两场比赛的目录：`/Users/winbeau/Competitions/ops-bench/**`、`/Users/winbeau/Competitions/mat-chem-sim-pred/**`，包括它们的工作树与分支。

另外：
- 回放与核对一律在副本上做：复制到临时目录。
- 会话日志与总线只出聚合数字。
- 分析产物放进工作树的 `docs/v4.3/reports/`。

### 隐私与设备

- 报告里不出现 cookie、token、账号名、用户 ID、主机地址。
- 仓库里不出现真实设备值：芯片型号、驱动路径、库名等。设备值只存在于 `config.json` 的 `device` 段与运行时环境（`cli/opsb_core/device_env.py`）。
- 新写的文档、提示词与知识卡片里，不写具体芯片型号与厂商专有术语。

### 门禁改动

- 每个改变门禁或派发行为的改动，都要给出 `GateConfig` 开关，登记进 `GATE_SWITCHES`，或者给出配置项，能回到 4.2.1 的行为。
- 门禁包的验收要附 `opsb replay gate` 的结果（P21 合入后）。

### MCP 与工具表

- 新工具或新参数，要同时登记 schema（`mcp/schemas.py`）、handler（`mcp/tools.py`）与允许角色，参数名两侧逐字一致。
- stdout 只留给 JSON-RPC。
- **工具表上限**：主控 15000 字节、worker 13000 字节。4.2.1 时分别是 14601 与 12507，有测试钉住。
- 优先给现有工具加参数或视图，参数不带空的 description。报告写出改前改后的字节数。
- 超限时不要改上限，在报告「待决」里说明。

### 契约与模块

- `scheduler/cli_bridge.py` 的 JSON 是 CLI 与 GUI 的共享契约。改字段时 Swift 解码同步改，并保持 decodeIfPresent。
- 新模块不超过 350 行。`gate.py`、`estimator.py`、`manager.py`、`cli_bridge.py`、`handlers_bench.py` 只加薄接入点。
- 判题回执去重按 `submission_id`。重放同一批回执，不得重复触发交接、换席、扣额或账本写入。

### 线上跑的是主检出的代码

- `~/.local/bin/opsb` 指向主检出的 `cli/opsb`，守护进程与 DSH 插件起的 `opsb mcp serve` 都用它。工作树里的改动不影响线上。
- 验证也在工作树内做：
  - 用 `PYTHONPATH=cli:. ~/.parhelion/venv/bin/python` 直接调 handler；
  - 或在工作树内运行 `cli/opsb …`。
- **真机步骤**只做子计划写明的那些：只测不提交；设备不通时标为待补。

### 测试基线（4.2.1）

```bash
~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
```

801 passed / 19 skipped。

```bash
PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
```

540 passed。改了 Swift 还要 `swift build`。

`runtime/tests/test_device_broker.py` 的两条预算测试在机器高负载时偶发失败，单独重跑通过即可，报告里注明。

### 提交信息与报告

- 提交信息用英文祈使句，正文写原因。末尾署名实际执行的模型，例如 `Generated-by: DSH <模型名>`；不要写 Claude 的署名行。
- 报告与提交信息里，不要写「用户已同意」「已获批准」之类的话。需要决定的事写进报告的「待决」。

## 3. 主会话的审查与合入

### 审查清单

- §2 的约束逐条核对；
- diff 只含本包文件；
- 新参数或新工具三处登记，角色正确；工具表字节在上限内；
- 有回退开关；门禁包附了回放结果；
- 没有设备值，也没有账号或凭据字段；
- 新模块不超过 350 行；报告完整；两条全量测试全绿。

### 合入流程

1. `git merge --no-ff`；
2. 跑两条全量测试，需要时加 `swift build`；
3. 有新测试文件时，执行 `aoci scope acknowledge --reviewed-by claude`，然后 `aoci_maintain` 直至 aligned，索引单独提交；
4. 更新 RUNBOOK §2；
5. `git push origin main`。

需要守护进程或应用生效的改动，全部合入后统一重启一次，作为 4.3.0 发布（00-PLAN §8）。

## 4. DSH 总包启动提示词

把下面这段贴进 DSH 主对话，工作目录为仓库根：

```text
你是 Parhelion 4.3 的实施总包。仓库：/Users/winbeau/Developer/personal/parhelion。
先完整读 docs/v4.3/10-DSH.md 与 docs/v4.3/00-PLAN.md，再读各包子计划 docs/v4.3/P21-*.md … P29-*.md。
按 10-DSH.md §1 的流程，为每个包在 /Users/winbeau/Developer/personal/parhelion-wt/<包> 建工作树、开一个原生子代理；同时最多 3 个。
先开 P21、P22、P26。P23、P24 等 P21 合入；P25、P29 等 P23 合入；P27 等 P26 合入；P28 插空档（第 4 项等 P23 合入）。依赖包从最新 main 新建分支。
子代理交付的标志：报告 docs/v4.3/reports/Pxx-<slug>.md 首行「状态：已交付」，分支最后一个提交的标题以「Deliver Pxx:」开头。交付后停下，等主会话审查合入。
你和子代理都不合入 main、不推送、不改 aoci 索引、不在主检出目录切分支或提交、不重启守护进程、不派发或停止真实 worker、不安装插件或应用、不写 ~/.dsh。
不改自动提交开关，不强制提交，不向判题站提交任何东西；额度不进入任何门禁阈值。比赛数据与会话日志只读，回放在副本上做。
合入进度看 docs/v4.3/RUNBOOK.md §2；返工意见在 main 的 docs/v4.3/reviews/Pxx.md（git show main:<路径> 读取），修正后提交标题用「Redeliver Pxx:」。
```
