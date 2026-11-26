---
id: COM-process-restore-before-experiment
pillar: community
portability: portable
status: unverified
mechanism_confirmed: yes
trigger: 在榜分归零或最近一发硬失败，且存在线上 Pass 过的更高分 SHA
cost: 一发额度；restore 不产生新信息，只是止损
failure_domain: 最高分 SHA 不可提交（分支未并入、工作树丢失）；或最高分与基线差在噪声内
bottlenecks: [general-perf]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16]
---

# 榜面归零后先 restore 再继续实验

## 规则

`latest` 榜型下一次失败会让整题归零。归零后在榜分是 0，任何后续实验的"基线"都失真，所以第一优先级
是恢复到**历史最好且本地全绿**的 SHA，而不是在归零状态下继续试新方向：

1. 找出该题线上 Pass 过的最高分 SHA；
2. 确认它可编译、本地全绿；
3. 发一发 restore，把基线拿回来；
4. 再开新实验。

restore 的目标必须是一个线上 Pass 过的 SHA，不要用"改回上一版"的近似提交代替——那不是 restore，是
一发新实验。

## 可测触发条件

- 本题在榜分为 0，或最近一发是 WA / RE / TLE；
- 且存在一个线上 Pass 过的、分数高于当前在榜的 SHA。

## 反例与失效域

- **反例**：归零后不 restore 直接试第二版，第二版又失败，整题继续归零，最后连"原来的分"都要靠复盘
  找回来。
- **失效域**：历史最高那一版的 SHA 已不在可提交路径上（分支未并入、工作树已清理）时，restore 发不
  出去，只能从最近一次线上 Pass 的 SHA 恢复。若最高分与基线之差在噪声内（见噪声卡），restore 到它
  只是回到基线，不算涨分。

## 来源

- `docs/v4.3/research/antigravity/00-SUMMARY.md` §4.2（restore 是主要手法）、§4.10（收官差额）、§5.1；
- `docs/v4.3/research/antigravity/R4-methods-vs-kb.md` §7 C5（基线必须是回退前的真峰值）。
