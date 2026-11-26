# P11 跨题设备批次与界面展示（4.1 路线 #3 #4）

分支：`impl/P11-batch-ui`。在 P10 合入后的 `main` 上开发。

## #3 跨题合并设备批次

现状：每个整题验证任务各排一次设备队（`runtime/npu_runtime/device_broker.py` 按 WSPT 串行，`case_batch.execute_batch` 复用 ACL 会话）。两题的作业前后相邻时，各自要承担一次会话建立与拆除。

1. 设备代理取作业时，如果队首之后紧跟着其他题已准备好的作业，就在同一设备会话里连续执行，最多合并 3 个。
   - 合并后仍按 WSPT 顺序；
   - 每题的 TLE、续排与逐例校验各自独立；
   - 持锁登记对探针可见，显示合并批次的成员。
2. 每题的「排队、持锁、端到端」时长分别记账，合并不能让某一题的记账失真。AGENTS.md §5 要求分别记录。
3. 开关 `device.merge_batches`，默认开。真机验收：
   - 两题并发提交 standard，对比合并前后的总持锁时长、会话建立次数和两题各自的端到端时长；
   - 结果写入 `docs/plan-4.1-4.2/reports/P11-batch.md`。

## #4 Swift UI 显示测试登记表、digest 与驱动暂停

1. 测试登记表：`services/test_runs.py` 的登记记录，含题、SHA、级别、状态、用例进度与重复进度、排队、持锁和端到端时长。
   - 经 daemon 热帧或快照下发；
   - 在 NPU 页的服务器队列旁、或题目检查器中显示；
   - 三车道卡片按运行 ID 展示，每个运行一张，见 AGENTS.md §5 与 `Features/NpuMonitor/NpuLaneModel.swift`。
2. digest：运行结束后可展开查看不超过 2KB 的 digest，包括逐例判定、计时与漂移、相对最佳官方 SHA 的提速。
3. 驱动暂停：
   - 显示供应商熔断导致的驱动暂停状态，包括驱动、原因、时间和受影响的 worker，见 `agents/manager.py` 的 suspensions；
   - 在协作页与设置页的驱动诊断中显示；
   - 提供「解除暂停」入口，调用现有的管理员显式解除路径。
4. 新字段一律 `decodeIfPresent`；热路径不新增大对象，digest 按需拉取。

## 验收

- Python 单测：
  - 合并批次的 WSPT 顺序、各题独立 TLE 与续排、记账拆分；
  - 新热帧字段可缺省。
- `swift build` 通过。用 `run` 技能或截图确认三处界面出现了数据。没有运行中的测试时，用一次 smoke 运行制造数据，需先向主会话确认设备空闲。
- 真机报告如上。
