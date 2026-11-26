---
id: COM-process-latest-state-change
pillar: community
portability: portable
status: unverified
mechanism_confirmed: yes
trigger: 赛题规则 ranking_submission_mode=latest，或本题榜面在没有新回执时归零
cost: 每次试探多留一发额度做 undo；归零窗口越长，在榜损失越大
failure_domain: best 榜型下不成立；平台已保留在榜最高分时，本卡退化为提交前检查项
bottlenecks: [general-perf]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16]
---

# latest 榜型下提交是状态变更，先备好 undo

## 规则

`latest` 计分只看最后一发：一次失败提交不是"这发没涨"，而是把整题榜面归零。所以一笔提交的代价 =
尝试成本 + 归零窗口内的在榜损失 + 补一发的成本。提交前先写下 undo 目标：当前在榜、线上 Pass 过的
SHA。没有可回滚 SHA 的方向，先当候选，不入队。

`best` 榜型下不存在这个代价，本卡不适用。

## 可测触发条件

- 赛题规则里 `ranking_submission_mode=latest`；或
- 本题榜面在没有新回执的情况下归零。

任一成立，就按本卡处理：先记录当前在榜 SHA 与分数，再决定这一发值不值得换掉它。

## 反例与失效域

- **反例**：把一次"本地更快"的新结构直接顶掉在榜 SHA，失败后整题归零，还要多发一发 restore 才回到
  原来的分。这一发的真实代价是两发额度加上归零窗口的排名损失。
- **失效域**：如果平台已经替你保留了"可回滚的线上 Pass SHA"，undo 只是一个确认动作，不需要手工翻
  记录；此时本卡只剩"提交前确认 undo 存在"这一条。

## 来源

- `docs/v4.3/research/antigravity/00-SUMMARY.md` §4.1（latest 计分与归零）、§5.1（不照搬无门禁的 force）；
- `docs/v4.3/research/antigravity/R4-methods-vs-kb.md` §7 C11（收官纪律：last 发是唯一有效发）。
