# P07 队列灯与门禁判定的只读核对（4.1 #9）

## 环境

比赛目录 `results/` 和 `opsb.workspace.json` 的副本放在会话临时目录；各题仓库用符号链接指向原位置，只做读取。`PARHELION_HOME` 指向临时目录，其中放有 `config.json`（只保留 device、scheduler 两段）、`scheduler_state.json` 和 `bench-specs` 的副本。真实比赛目录没有写入，`.candidate-archive.json` 只出现在副本里。

命令：本分支执行 `opsb queue list --json --workspace <副本>`，耗时 6s。然后对同一副本运行 `scheduler.cli_bridge status --offline`，逐项比较两边的门禁结论。

## 结果

- **灯与门禁一致：** 92 张卡片中，非历史卡片有 14 张。其中 13 张有门禁结论，SAFETY、NOISE_FLOOR、CIRCUIT_BREAKER 三盏灯和 `dispatch_ok`（对应 QUOTA_RESERVE）与 bridge 的 stages 全部一致，**不一致 0**。
- **剩下 1 张**是 RadialBasisBatch 8636afe 的 `HOLD: COMPLIANCE`。它是判题拒绝的历史行，本来就没有门禁结论。卡片显示的是判题消息，不再是堆栈。
- **徽章分布：**
  - HOLD: LOW GAIN 5；
  - EXPLORE 4；
  - TEST FAILED 2；
  - NOT TESTED 1；
  - ESCAPE LOCK 1：QuantScaleCalib 369d215，下一步为"awaiting reproduction"。
- **措辞检查：**
  - 所有卡片里都没有「额度」二字。
  - 灯、徽章、理由、下一步里也没有 "quota"。
  - 英文 "quota" 只出现在 4 张已完成卡片的 `note` 中：那是 599d8cc 之前请求提交时写进队列行的旧 S2 文案（"holding to preserve daily quota"），属于历史记录，没有改写。
- **候选档案（R12）：** 例如 CGTensorProduct 的前三名为 f8a1b62 25.11、3727609 25.00、879278d 24.73，每个都带 15 个用例的逐例最小值。EdgeIndexCoalesce 目前只有 1 个候选，即 4f954c3（14.96）。

## 仍需主会话决定

门禁的 S4（QUOTA_RESERVE）对 `RESERVED_FOR_CLOSING` 仍然会让 `approved=false`，也就是为收官保留剩余槽位而暂缓提交。这与"额度不得进入门禁阈值或暂缓理由"相冲突。本包只把 S4 移出了灯与理由：这类卡片显示 `WAITING FOR SLOT`，S1–S3 的灯照常亮。门禁的判定本身没有改动，要不要删除这条暂缓规则由主会话决定。
