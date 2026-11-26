# P09 #20：判题工具链只编译检查

日期 2026-11-26，分支 `impl/P09-host-daemon`。本报告只写证据的位置和比较结论。真实版本号、芯片型号和路径只存在于 `~/.parhelion/config.json` 的 `device` 段和比赛目录里，仓库中不出现。

## 1. 判题工具链：已经查到的证据

| 问题 | 证据位置 | 结论 |
|---|---|---|
| 判题用的工具链版本 | 每题 `PROBLEM.md` 首行的 Target 字段；`opsb.workspace.json` 里的 `problems[].cann_version` | 14 道题写的是同一个版本。 |
| 设备主机本地用的版本 | 10f21fd 的 bench 报告 `results/SgemmFp16Cube/10f21fd…/bench-runs/<run>/report.json` 的 `environment.sdk`；同目录 `build.log` 里编译器 include 路径中的版本目录 | 本地比判题新一个次版本。也就是说，到目前为止所有本地套件都是用判题以外的工具链编译的。 |
| 编译选项 | 题目模板 `run.sh` 和 `CMakeLists.txt`（判题要求只读模板文件保持原样，见 `runtime/judge/project_files.py` 的 `collect_kernel_files`） | configure 参数是 `-DCMAKE_ASC_RUN_MODE=npu -DCMAKE_ASC_ARCHITECTURES=<arch>`，编译选项是 `--npu-arch=<arch>`，C++14，不指定构建类型。平台的 case-bench 和 `npu-test.py` 模板路径用的参数相同，只多一个 `CMAKE_EXPORT_COMPILE_COMMANDS=ON`，它不影响代码生成。没有找到选项不同的证据。 |
| 判题实际编译什么 | `submission-manifest.json`（10f21fd 只上传了 `kernel.asc`）；`project_files.py` 里的 `PROTECTED_KERNEL_FILES`（`main.asc`、`judge.asc`、`data_utils.h` 不允许提交） | 判题用自己的评测框架文件，加上提交的可编辑 `.asc/.h` 来编译。本地的 `main.asc` 是模板里的样例框架，它和判题框架是否相同，本地证据无法确认。判题的完整编译命令也没有公开，所以本地最多做到"模板脚手架 + 候选 kernel + 判题版本工具链"。 |
| 设备主机上有没有判题版本的工具链 | 需要登录主机看安装目录（`device.sdk_glob` 匹配的目录） | **未能确认**：本次会话期间主机 SSH 一直不可用（见 §4）。 |

## 2. 只编译检查能拦住哪些线上失败

`results/submit-queue-outcome.jsonl` 里没有任何一条状态是 `Compile Error` 的回执，而判题是会报这种状态的（`judge_score_fetch.TERMINAL_JUDGE_STATUSES`）。本场所有线上失败都是先编译通过，然后才 RE、WA 或 TLE：

- Runtime Error：NeighborListPbcFixedK 1394d23；EdgeIndexCoalesce 6ac73b0、4a47553；MeshEdgeMessagePassing 61028cb；SgemmFp16Cube 10f21fd。
- Wrong Answer：QuantScaleCalib 9f1789c、9ffca10、5041c86、a2717b5，以及 CGTensorProduct、FFT3DBatchComplex、NeighborList、MeshEdge、EdgeIndex 各一到两发。
- Time Limit Exceeded：SgemmFp16Cube 67930dd 两发。

SgemmFp16Cube 10f21fd（submission `6b07bde1…`）的状态是 Runtime Error，不是 Compile Error，说明判题编译它是成功的。所以无论用哪个版本的工具链，只编译检查都发现不了这一次的问题。本地用本机工具链跑 build-only，CPU 编译 16.0 s，墙钟 17.8 s（`result-build.json`）；完整套件 15/15 通过。

只编译检查仍然有用，它能防住两类风险：一是判题版本的编译器拒绝本地版本能接受的写法；二是 worker 在根目录加了新的 `.asc/.h`，或者依赖了本地脚手架里独有的内容，导致判题那边编译失败。但它解释不了本场的 RE 和 WA。RE 更可能出在运行时，比如两个工具链版本生成的代码不同，或者判题 harness 不同。下一步更有价值的是在判题版本的工具链下真正跑一次（见 §5）。

## 3. 实现

- 配置写在 `~/.parhelion/config.json` 的 `device` 段：`judge_toolchain_root`（设备主机上与判题版本一致的安装目录，会 source 其中的 `set_env.sh`），可选的 `judge_toolchain_env`（直接指定 env 脚本），可选的 `judge_compile_flags`（追加的 CMake configure 参数，可以写成列表或一个字符串）。`device_env.DEFAULTS` 里这几项都是空的，表示未配置。
- `scheduler/judge_compile.py`：
  - `check`：调用 `runtime/npu-test.py --build-only` 的模板路径，并带上 `OPSB_TOOLCHAIN_ENV`（判题工具链）、`OPSB_COMPILE_CHECK=judge`、`OPSB_CMAKE_ARGS` 三个环境变量。远端只走 CPU 车道（受 `lanes.compile_lane` 预算约束），不进设备锁，不生成样例数据，使用独立的 `build-judge` 目录，日志写到 `judge-build.log`。结果记录在 `results/<题>/<SHA>/judge-compile.json`，记录带工具链和选项的 identity，换了配置旧记录就作废。
  - 结果分三类：`pass`；`fail`，只有 `cmake --build` 这一步失败才算候选自身的问题；`error`，包括传输失败、git fetch 失败、configure 失败、找不到 env 脚本，这一类不拦截。
  - `preflight`：给上传前用，不阻塞。未配置时返回 `upload`。没有记录时在后台起一次 `check`，返回 `wait`。记录为 `fail` 时返回 `hold`。
- 门禁 S1（`scheduler/gate.py` 只加了薄接入点）：参考套件通过之后读取记录。`fail` 时以 `JUDGE_COMPILE_FAILED` 拒绝，理由里带编译器的 error 行（只保留文件名和行列号）。其余情况放行，并在理由末尾附一句说明：`compiles with the judge toolchain`、`check pending`、`check error: …`，或 `not configured`。卡片徽标显示 `COMPILE FAILED`。
- 守护进程上传前（`daemon/dispatch.py`）：调用 `preflight`。返回 `wait` 时退避 10 s，`hold` 时退避 300 s 并刷新卡片。未配置时不起子进程。
- 手工运行：`~/.parhelion/venv/bin/python -m scheduler.judge_compile check -w <比赛目录> --title <题> --sha <40 位 SHA>`，`show` 用来查看记录。

## 4. 真机验收：本次未完成

- 主机状态：TCP 端口可以连通，但 sshd 在密钥交换阶段就重置连接（`kex_exchange_identification: Connection reset by peer`）。会话期间重试了十多次，间隔从几十秒到几分钟。运行中的 daemon 的设备流也是 `stream_connected=false`，所以不是本机的问题。
- 因此以下三项都没有做：确认主机上是否装有判题版本的工具链；用该工具链对 10f21fd 跑只编译检查并计时；`opsb doctor --cases --remote`。
- 主机恢复后可以这样验收，不需要改动配置文件，用环境变量临时覆盖即可：
  ```bash
  OPSB_DEVICE_JUDGE_TOOLCHAIN_ROOT=<主机上判题版本的安装目录> \
    ~/.parhelion/venv/bin/python -m scheduler.judge_compile check \
    -w /Users/winbeau/Competitions/mat-chem-sim-pred --title SgemmFp16Cube \
    --sha 10f21fd26e555c0efd19c13957a3d1e31d854ebf
  ```
  预期：编译时长与本机工具链的 build-only 相当（CPU 约 16 s，不含样例数据生成）。按 §2 的分析，结果应该是 `pass`；如果是 `fail`，说明在判题版本下这个 SHA 根本编不过，与它线上是 RE 而非 Compile Error 的事实矛盾，需要怀疑判题 harness 与本地脚手架的差异。主机上如果没有判题版本，就需要先安装。只要 `judge_toolchain_root` 还没配置，检查就一直跳过，门禁也只是附一句提示。

## 5. 建议

1. 先在设备主机上确认或安装与判题一致的工具链版本，再配置 `judge_toolchain_root`。
2. 本场 RE 和 WA 的主因不在编译阶段。建议另立一项：对本地通过、准备上传的候选，用判题版本工具链跑一次 `standard`，只比较正确性、不计时，把"版本差异导致的运行期失败"和"数据相关的 WA"（4.1 #5、#6）分开。
3. 判题 harness 和本地 `main.asc` 不是同一个文件。如果判题模板接口（`/api/problems/{id}/template`）能拿到 harness 的源码，可以把它放进 oracle，让本地检查与判题编译的内容一致。

## 6. 补充：主机恢复后的检查（2026-11-26 07:25–07:35 UTC）

只做只读检查，加一次只编译检查验证链路，没有改 `~/.parhelion/config.json`。

- **主机为何拒绝连接**：设备主机是一个容器，07:29 UTC 前后重启过（sshd 与 tmux 目录都在那一刻重新创建；`uptime` 显示的是宿主机的开机时间，所以不变）。容器根目录（含 `/tmp`）是 overlay，`/home` 是独立的持久盘。重启期间 sshd 在密钥交换阶段重置连接，与 §4 记录的现象一致。
- **`opsb doctor --cases --remote`**（P09 分支，耗时 1.4 s）：`/tmp/opsb-case-cache` 不存在，0 条、0 B，没有统计。容器重启把 `/tmp` 整个清掉了，用例缓存、`/tmp/opsb-npu-tickets` 一并丢失；`~/.opsb`（broker 脚本、die ledger）在 `/home` 上，保留了下来。重启后的第一轮准备要重新生成参考用例（FFT 约 50 s）。
  建议后续把用例缓存和 case-bench 的临时目录一起放到 `/home`（两者要在同一个文件系统上，才能用硬链接），例如经 `OPSB_CASE_CACHE_DIR` 再加一个临时目录选项。本次没有改。
- **装了哪些工具链**：主机上只有一套工具链，`cann` 和 `ascend-toolkit/latest` 两个符号链接都指向它，版本号就是 §1 说的比判题新一个次版本的那个。`/usr/local/Ascend` 下只有驱动。另有一个同名的 `cann-tools` 目录，是工具脚本仓库，不是工具链。**没有与判题版本一致的安装。**
- **启用检查还缺什么**：先在主机上并排装一套判题版本的 toolkit（例如装到 `~/Ascend/cann-<判题版本>`；`/home` 还有约 176 GB 空闲），再把 `device.judge_toolchain_root` 指向它，或者把 `device.judge_toolchain_env` 指向它的 `set_env.sh`。在此之前检查保持跳过，门禁只附一句提示。
- **链路验证**：用环境变量 `OPSB_DEVICE_JUDGE_TOOLCHAIN_ENV` 临时指向主机自带的工具链，在一个临时工作区里对 SgemmFp16Cube 10f21fd 跑了一次 `check`，真实比赛的 `results/` 没有写入。
  - 结果：`pass`，墙钟 19.4 s，其中远端 CPU 编译 17.1 s（-j1，只有一个编译单元）；不进设备锁，在独立的 `build-judge` 目录里构建，不生成样例数据。日志里有 `[TEMPLATE] toolchain …`、`cmake configure`、`cmake build`。
  - 结论：只能证明链路可用，以及每个 SHA 的检查成本大约 20 s。它不能说明在判题版本下能否编译通过，那必须等判题版本装好之后再测。
