---
id: COM-process-local-profiler-needs-pairing
pillar: community
portability: portable
status: unverified
mechanism_confirmed: no
trigger: 结论只有一次本地测量，或本地与线上方向相反，或无回执就跨题推广
cost: 每条推广多一次配对测量或一条线上回执；误广播的代价是全队回滚
failure_domain: 冷启动效应使本地测量偏乐观；配对证据只升一级，不等于跨题成立
bottlenecks: [general-perf]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16]
---

# 本地 profiler 结论先经配对或线上验证再推广

## 规则

本地 profiler / 单机基准给出的结论，按证据等级只有 `local`。要升格为可复用的硬件规律，必须有：

- `paired`：同一批用例、同一环境下的逐例配对差；或
- `online_receipt`：一条官方回执。

两者都没有时，它只能留在候选，查询结果里标「待验证」。`local` 证据不允许直接写成硬件规律，也不
允许直接广播给全队。

## 可测触发条件

- 结论只来自一次本地测量；
- 或本地结论与线上回执方向相反；
- 或同一机制要被跨题推广，但没有任何线上回执。

## 反例与失效域

- **反例**：把本地 profiler 的"半核更快"当硬件规律广播，照做的题线上大跌；把"预加载更多"跨题推广，
  剂量反应把另一题的分数打下去，20 分钟内全队回滚。
- **失效域**：冷启动 / 首次运行效应会让本地测量系统性偏乐观（温启动掩盖首跑税），推广前必须用冷启动
  重复或线上回执校验。配对证据也不能替代跨题验证——它只把结论从 `local` 升到 `paired`。

## 来源

- `docs/v4.3/research/antigravity/00-SUMMARY.md` §4.8（广播与证据等级）、§5.8（不照搬未验证的硬件定律）；
- `docs/v4.3/research/antigravity/R4-methods-vs-kb.md` §7 C9（瘦 ELF 的收益上限）。
