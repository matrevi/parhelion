---
id: COM-process-judge-time-shared-budget
pillar: community
portability: portable
status: unverified
mechanism_confirmed: yes
trigger: 编译耗时接近时限的 80%，或模板实例化数/内核体积环比 > 1.5 倍，或本题出现过 TLE
cost: 瘦身要一次编译验证；过度瘦身可能牺牲性能
failure_domain: 瘦身换不到分；本卡只防 TLE，不能当涨分归因
bottlenecks: [icache-bloat, general-perf]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16]
---

# 判题时限是编译加评测共享的总时限

## 规则

判题机上"编译 + 全部用例"共享一个**总时限**（历史观测在 7 分钟量级）。决定你会不会超时的不是算法快慢，
而是模板实例化数与编译量。提交前的机械体检要读三个数：

- 模板实例化数；
- 编译耗时；
- 内核体积。

任一进入红区，就把这一发当成"可能 TLE"处理：先减实例化、减编译量，再谈性能。**临界区间里不要原样
重投同一 SHA 去"再试一次"**：同一 SHA 曾在 400s 通过、7 分钟后 420s 超时。

## 可测触发条件

- 提交前测得的编译耗时进入本场时限的 80% 以上；
- 或模板实例化数 / 内核体积环比明显增长（例如 > 1.5 倍）；
- 或本题历史上出现过 TLE。

## 反例与失效域

- **反例**：剪掉近一半模板实例化、编译更快，线上分数反而下降。瘦身换到的是"不 TLE"的保险，不是涨分。
- **失效域**：本卡只用于防 TLE，不能作为涨分归因；紧贴红线的编译可能"剪完仍然临界"，需要继续留余量。

## 来源

- `docs/v4.3/research/antigravity/00-SUMMARY.md` §4.5（提交前机械体检）、§2 末（瘦身与涨分的因果只在单题成立）；
- `docs/v4.3/research/antigravity/R4-methods-vs-kb.md` §4（约 7 分钟总时限）、§7 C9 / C10。
