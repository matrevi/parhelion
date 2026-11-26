---
id: COM-process-correctness-before-speed
pillar: community
portability: portable
status: unverified
mechanism_confirmed: confounded
trigger: 逐例比对存在 mismatch，或一发同时改正确性路径与性能结构
cost: 拆发多一次提交与验证；混发的失败代价更高
failure_domain: 纯结构改动不动精度路径时；或拆分后的合并成本高于收益
bottlenecks: [general-perf]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16]
---

# 先修正确性，再求快

## 规则

一次迭代只做一件事：先把精度与确定性修到位（bit-exact、边界 dtype、非对齐尾部），再做性能结构（去
框架、分核、流水）。两者混在一发里，线上失败时分不清是哪一半坏了，也无法把结果写回假设。

历史上最大的两次跳变都发生在"先修对"之后——快是正确性的副产品，不是并列目标。

## 可测触发条件

- 本地逐例比对存在 mismatch；
- 或精度判据未过，而同一发里还有性能结构改动；
- 或一发同时改了正确性路径与数据通路（无法二分）。

## 反例与失效域

- **反例**：在一棵精度未过的树上继续叠优化，每发都失败，最后既没有正确版本也没有可归因的性能结论。
- **失效域**：纯结构改动、不触碰精度路径时，本卡不要求先做一次纯精度提交；正确版本已稳定、结构改动
  过大时，强行拆发反而增加合并成本，此时应保留完整 diff 以便二分。

## 来源

- `docs/v4.3/research/antigravity/00-SUMMARY.md` §2（两次最大跳变的读法）、§4.5；
- `docs/v4.3/research/antigravity/R4-methods-vs-kb.md` §9（第 4 步"改一个变量"的形式化问题）。
