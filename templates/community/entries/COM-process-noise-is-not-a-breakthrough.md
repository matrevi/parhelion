---
id: COM-process-noise-is-not-a-breakthrough
pillar: community
portability: portable
status: unverified
mechanism_confirmed: confounded
trigger: 只有一条回执且 |Δ| < σ̂；或同一 SHA 重判的差值落在 σ̂ 内
cost: 证实一条机制要多判 1–2 发；额度紧时只对关键结论做重复
failure_domain: 判题侧出现双峰或同刻重复回执时 σ̂ 失真，此时任何单发结论都不可用
bottlenecks: [general-perf]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16]
---

# 判题噪声 σ̂ 以内的差不算突破，同源重投期望略负

## 规则

单发的分差落在判题噪声量级内时，不能当突破。先估本题的 σ̂（同一 SHA 重判差值的离散度；历史 pooled
σ 约 3 分），`|Δ| < σ̂` 的"突破"只记为候选，不作为下一轮的前提。

要证实一个机制，优先：

1. 同一 SHA 多判几次取均值（`n_draws` ≥ 2），或
2. 做**配对**比较：同一批用例、同一环境下的逐例时间差，而不是换一个 SHA 再判一发。

同源重投的期望略负（历史中位约 −1 分），它不是免费期权；不能靠重投吃噪声。

## 可测触发条件

- 本题只有一条回执，且 `|Δ| < σ̂`；
- 或两次回执的 SHA 相同，而分差落在 σ̂ 内；
- 或一条"突破"没有任何配对的逐例差。

## 反例与失效域

- **反例**：同一 SHA 在同一时刻产生两条回执，分差远大于 σ̂ —— 这是判题侧双峰，不是代码贡献；把它
  记成突破会同时抬高 σ̂ 与突破计数。
- **失效域**：判题侧双峰、或重判样本只有个位数时 σ̂ 会被低估，单发结论一律不可用。换机制后 `|Δ|`
  明显大于 σ̂ 时，本卡不阻止结论。

## 来源

- `docs/v4.3/research/antigravity/R4-methods-vs-kb.md` §2（σ 与重投噪声）、§7 C1 / C4；
- `docs/v4.3/research/antigravity/00-SUMMARY.md` §4.9 补充测量、§5.2（不照搬"同源重投吃噪声"）。
