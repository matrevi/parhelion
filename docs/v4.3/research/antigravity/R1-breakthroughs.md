# R1 涨分归因

> 数据窗口：2026-11-10 ～ 2026-11-23；回执 233 条；10 题。

本文件回答三个问题：官方分是怎么一格格涨上去的、哪些涨分是真的、真涨的分来自哪几类代码手法。
所有回执口径来自 `scheduler/receipts.py`（`official_score`：Pass 取 `score`，WA/RE/TLE/CE 记 0，`latest` 计分），
阶梯由 `/tmp/p19/receipts.json`（233 条按 submission_id 去重）重算，与 `/tmp/p19/breakthroughs_raw.md` 一致。

---

## 0. 三个前置发现（决定了后面怎么读数）

### 0.1 锚点复核通过：421 → 542 → 723 → 752.3

按「各题历史最高分之和」重算四个时点，与任务书锚点误差 ≤0.04：

| 时点 | 各题最高分之和 | 与锚点 |
|---|---|---|
| 11-10 末 | **420.97** | 421 |
| 11-18 末 | **541.95** | 542 |
| 11-22 末 | **722.86** | 723 |
| 最终 | **752.33** | 752.3 |

[证据: /tmp/p19/receipts.json 全量重算, 脚本口径同 scheduler/receipts.py::official_score]

11-10 之后共 **74 次**上台阶（刷新历史最高分），累计 **331.36**，与 752.33−420.97 精确闭合。
这 74 步就是本报告的全部"涨分样本"。

### 0.2 同一份二进制重判，能差 16 分

判题不是确定性的。把「同一 sha12 被投了两次以上」的 24 组回执拉出来，其中 **8 组第二次分数更高**，合计
**26.08 分**——占 331 的 **7.9%**。这不是算法的功劳，是重判：

| 题 | SHA | 同一二进制拿到的分 | 跨度 |
|---|---|---|---|
| LayerNormV3 | `ec0867dd795b` | 49.49 → **65.45** | **15.96** |
| StridedSliceGrad | `f2cee3b1691a` | 52.54 → 56.33 → 52.29 | 4.04 |
| ConfusionTransposeD | `8e7ba6bc0da3` | 78.18 → 74.39 → 79.08 | 4.69 |
| SyncBatchNorm | `544439ed46c3` | 57.45 → 54.96 → 56.20 → 52.47 | 4.98 |
| LayerNormV3 | `9773dff47a13` | 73.40 → 73.65 → 67.91 | 5.74 |
| ConcatD | `474dc59b3f64` | 76.10 → **77.96** | 1.86 |
| QuantUpdateScatter | `bfc6420a2618` | 65.81 → 67.83 | 2.02 |
| ConcatD | `7919dcbfac7b` | 54.43 → 55.42 | 0.99 |

[证据: 回执 6b02dd43… / 6b02dec1…（LayerNormV3, sha ec0867dd795b）]

最极端的一例值得单独说：`ec0867dd795b` 的两次回执 **`queued_at` 完全相同**（2026-11-22T12:17:03.673813Z），
即同一次排队被重判两次 —— 12:23:38 判出 49.49，12:23:30 重新提交、12:30:12 判出 65.45。
[证据: results/submit-queue-outcome.jsonl 中 sid=6b02dd4385ad43bbe0e11d0e 与 6b02dec185ad43bbe0e53734 两行]
【推断】49.49 那一次应是设备冷启/被前一个任务占用时的慢跑；两次回报的 note 文本逐字相同，源码与二进制都没变。

**推论（贯穿全报告）**：单次回执 ±2～5 分是常态噪声，极端情况 ±16 分。所以「+2 分」级别的"突破"多数不可信；
本报告把 **同二进制重判涨分** 全部移到 §2.3，不计入手法归因。

### 0.3 还有第二种噪声：抽到慢节点

11-23 的看板里有一条自发记录，等于官方承认了节点漂移：

> `f60fa82` drew a `+2.5%` slower node (`P1/P4/P5` `+0.10us` vs `0fb436a`), yet **`P2` dropped `4.39 -> 4.20us`**

[证据: 95eafe6a 共享看板 `.system_generated/swarm/board.jsonl` #630]
即同一份代码换台机器，5 个公开用例整体 ±0.10us（约 2.5%），足以吃掉 1～2 分。
另一种"漂移"是 `tbest` 刷新：score = `100/(1+ln(t/tbest)/ln1.5)` 的均值，tbest 变快则同代码分数变低，
这也解释了为什么 11-22 之后同一 SHA 重投普遍比第一次低（CTC 69.25→67.80、CCA 72.53→67.45）。

另外，回执 `note` 里自认 `same-SHA` 的至少有 4 条，例：
`same-SHA 1 draw(s) raw=[54.43] -> contemp=[52.95] (gamma=1.0000) => est=52.95`
[证据: 回执 6b02d14785ad43bbe0c2d3b1（ConcatD, sha 7919dcbfac7b）]

### 0.4 第三种缺口：`Pass` 但分数没记上（7 条）

有 7 条回执 `status=Pass` 却 `score=None`，其中 **6 条集中在 StridedSliceGrad 的 11-22～11-23**：
`9585ce52`(6b02e506)、`e635a6c`、`121c9c4`、`734e0975`(6b03fbed)、`b15fea4`、`dad8589`，外加 ConcatD `38d5d548`。
这不是 0 分，是**没拿到分数**。11-23 的巡检定位到了根因：

> `get_submission` returns `Pass` (never `score`) slightly before `/ranking` indexes the new `submission_id` onto the team's single row

[证据: 813867cb 看板 board.jsonl #513，记为 `[V3-025]`；同一问题在 #533/#539 被再次确认]
后果：**这 6 次 SSG 提交的真实分数未知**，所以本报告不能把它们算作"失败"，§2 里凡涉及"同方向失败几次"的
计数均已剔除这 6 条。这也是"回执 233 条"之外还有 `score=None` 缺口的原因。

---

## 1. 十题官方分时间线

### 1.0 总表

| 题 | 判题数 | 首发分 | 11-10 末 | 最终最高 | 全程涨幅 | 最大单次涨幅（真实） | 上台阶次数<br>(11-10 后) |
|---|---:|---:|---:|---:|---:|---:|---:|
| CTCLossV2 | 16 | 37.18 (11-11) | 0 | **75.11** | +75.11 | +15.67 `0d5125c` | 9 |
| ConcatD | 30 | 52.36 (11-10) | 52.87 | **77.96** | +25.09 | +7.14 `c8b5521` | 9 |
| ConfusionTransposeD | 25 | 38.64 (11-10) | 43.55 | **80.07** | +36.52 | +9.32 `31affc6` | 9 |
| LayerNormV3 | 18 | 26.81 (11-10，首发是 WA=0) | 27.13 | **73.65** | +46.52 | +16.53 `af0807c` | 6 |
| MultilabelMarginLoss | 25 | 46.09 (11-10) | 56.64 | **75.54** | +18.90 | +10.55 `13c554d` | 3 |
| PReluGradUpdate | 14 | 56.56 (11-10) | 56.84 | **78.01** | +21.17 | +5.81 `1baa854` | 6 |
| PadV3Grad | 33 | 27.12 (11-10) | 30.38 | **59.98** | +29.60 | +10.00 `49f905f` | 9 |
| QuantUpdateScatter | 17 | 47.15 (11-10) | 47.15 | **86.61** | +39.46 | +18.05 `28b9ce3` | 8 |
| StridedSliceGrad | 45 | 47.48 (11-10) | 48.96 | **72.45** | +23.49 | +4.64 `306c457` | 11 |
| SyncBatchNormGatherStats | 10 | 57.45 (11-10) | 57.45 | **72.95** | +15.50 | +10.58 `de25877` | 4 |

合计：233 条回执（Pass 218 / TLE 7 / WA 6 / RE 2），74 次上台阶，331.36 分。
[证据: /tmp/p19/receipts.json + 各题 git 树差（见 §2）]

三个可读的形状规律：

1. **11-10 的 421 分几乎全是"首发绝对分"**：除 CTC（11-10 记 0、11-11 才首次得分）外，其余 9 题的 11-10 分数
   都是它们在窗口内的**第一次**得分，不含任何"涨"。这 421 是 11-10 之前本地工作的第一次线上兑现，
   也是任务书把涨幅起点定在 11-10 之后（421→752.3）的原因。
2. **11-11～11-17 是空窗期**：只有 11-11 两条（CTC 37.18、Pad 29.60 的回退），其余日期无判题。
   真实节奏是「11-10 首爆 → 停 8 天 → 11-18 一轮 → 11-22 大爆发 → 11-23 收尾」。
3. **11-22 单日 +180.9 分**（541.95→722.86），占全部涨幅的 54.6%；11-23 再 +29.5。

### 1.1 CTCLossV2：0 → 75.11，靠两个阶跃

| 时刻 | SHA | 分 | Δ | 判题耗时 |
|---|---|---:|---:|---:|
| 11-11 03:02 | `02c2048c37ef` | 37.18 | +37.18（首发，此前 0） | — |
| 11-18 06:11 | `0d5125cd90ba` | 52.85 | **+15.67** | 2.4 min |
| 11-18 09:12 | `2b9d49de0f68` | 56.21 | +3.36 | 2.4 min |
| 11-22 12:03 | `1af7003e755c` | 60.63 | +4.42 | 2.4 min |
| 11-22 13:23 | `db8d058ffac4` | 61.95 | +1.32 | 2.4 min |
| 11-22 13:38 | `be126856c32c` | 65.65 | +3.70 | 2.6 min |
| 11-22 15:59 | `0c769babf5b8` | 69.25 | +3.60 | 2.6 min |
| 11-23 10:02 | `cca48606e69a` | 72.53 | +3.28 | 2.4 min |
| 11-23 12:33 | `dcc7754d4a02` | **75.11** | +2.58 | 2.6 min |

形状：**11-11 首发 37 → 11-18 换算法阶跃 +15.67 → 11-22 起每 1～2 小时一发小步 → 11-23 收尾**。
中间 11-22 17:50 与 11-23 上午有一段回退带（66.61、67.80、68.03、65.11、70.51、RE=0），
其中一次 RE（`a1df9d5`，11-23 11:06，记 0 分）是 11-23 最大的单次下跌。全题 16 次判题里有 7 次低于历史最高。

### 1.2 ConcatD：一天里连跳三格

| 时刻 | SHA | 分 | Δ | 判题耗时 |
|---|---|---:|---:|---:|
| 11-10 03:36 | `e799eb6ed744` | 52.36 | 首发 | — |
| 11-10 04:34 | `899a2dc6a824` | 52.87 | +0.51 | — |
| 11-18 08:32 | `7919dcbfac7b` | 54.43 | +1.56 | 2.7 min |
| 11-22 11:28 | `7919dcbfac7b` | 55.42 | +0.99（**同二进制**） | — |
| 11-22 11:44 | `0d6ea0690206` | 57.14 | +1.72 | 3.6 min |
| 11-22 12:29 | `5a8d07f9a1e6` | 57.17 | +0.03 | 3.6 min |
| 11-22 14:03 | `c8b55211e9e5` | 64.31 | **+7.14** | 3.6 min |
| 11-22 14:47 | `aa74f5696992` | 71.00 | **+6.69** | 3.6 min |
| 11-22 15:15 | `48325243a133` | 75.60 | +4.60 | 3.6 min |
| 11-22 16:41 | `474dc59b3f64` | 76.10 | +0.50 | — |
| 11-22 17:22 | `474dc59b3f64` | **77.96** | +1.86（**同二进制**） | — |

形状：**11-10 首发 52 → 11-18 小步 → 11-22 下午 exp-b/main 双轨并行，2 小时内三发 +18.43（57.17→75.60）**。
最典型的一天：14:03、14:47、15:15 三发连续刷新。注意末位 +1.86 是把已验证的 `474dc59b` 重投一次拿到的，
源码一字未改（见 §2.3）。全题 30 次判题中有 2 次 WA、1 次 TLE。

### 1.3 ConfusionTransposeD：最平滑的一条 40 分长坡

| 时刻 | SHA | 分 | Δ |
|---|---|---:|---:|
| 11-10 00:54 | `c43309cc1213` | 38.64 | 首发 |
| 11-10 00:57 | `a4fef306608d` | 40.62 | +1.98 |
| 11-10 02:58 | `b15e2ddf7c3e` | 42.31 | +1.69 |
| 11-10 07:13 | `fa81ef9b64c2` | 43.55 | +1.24 |
| 11-18 08:35 | `d4d090fbd3e4` | 48.83 | +5.28 |
| 11-18 10:23 | `6606085a0a1f` | 50.11 | +1.28（回滚） |
| 11-22 13:06 | `d8eb6daa1044` | 56.40 | +6.29 |
| 11-22 13:29 | `31affc6e0efa` | 65.72 | **+9.32** |
| 11-22 14:14 | `39388639a651` | 74.11 | +8.39 |
| 11-22 14:28 | `c17a4bc4913a` | 75.25 | +1.14 |
| 11-22 14:45 | `8e7ba6bc0da3` | 78.18 | +2.93 |
| 11-22 19:41 | `8e7ba6bc0da3` | 79.08 | +0.90（**同二进制**） |
| 11-23 12:37 | `235f754f7a99` | **80.07** | +0.99 |

形状：**11-10 小步爬 38→43.5 → 11-18 一次 +5.28 → 11-22 下午 100 分钟里 +21.8 → 之后只有 0.9/1.0 的收尾**。
13 次上台阶里有 3 次来自回滚（`a4fef30`、`6606085`、`8e7ba6b` 重投），说明该题的"最优解"一直在几个候选间来回。

### 1.4 LayerNormV3：分最碎的一题，而且含全数据集最大噪声

| 时刻 | SHA | 分 | Δ |
|---|---|---:|---:|
| 11-10 07:00 | `07ae054bb65b` | 0（WA） | — |
| 11-10 08:40 | `bf2beb9a968a` | 26.81 | +26.81（首发转正） |
| 11-10 08:48 | `bf2beb9a968a` | 27.13 | +0.32（同二进制） |
| 11-18 06:05 | `af0807c2c600` | 43.66 | **+16.53** |
| 11-22 12:13 | `e21fdced97de` | 49.49 | +5.83 |
| 11-22 12:30 | `ec0867dd795b` | 65.45 | +15.96（**同二进制重判**） |
| 11-22 14:03 | `8c8c1643dea3` | 71.30 | +5.85 |
| 11-22 14:13 | `9773dff47a13` | 73.40 | +2.10 |
| 11-22 14:20 | `9773dff47a13` | **73.65** | +0.25（同二进制） |

形状：**11-10 首发 WA 归零 → 11-18 一次 +16.53 → 11-22 中午两发拉满 → 之后 9 发全部低于 73.65**。
该题 18 次判题里最后 6 发（14:51～16:36）全部没破纪录，还有 1 次 TLE（`cec9fed`）。
真正的手法涨幅是 `af0807c` 的 +16.53 与 `8c8c164` 的 +5.85；中间那个 +15.96 是噪声（§2.3）。

### 1.5 MultilabelMarginLoss：两头大、中间平

| 时刻 | SHA | 分 | Δ |
|---|---|---:|---:|
| 11-10 01:12 | `f77146dcb0d4` | 0（TLE） | — |
| 11-10 01:14 | `495ca5c48202` | 46.09 | +46.09（首发） |
| 11-10 04:17 | `59f80657c0fc` | 56.64 | +10.55 |
| 11-22 12:36 | `9f92b0a4702b` | 61.93 | +5.29 |
| 11-23 09:37 | `8aca680b3428` | 64.99 | +3.06 |
| 11-23 09:54 | `13c554da2437` | **75.54** | **+10.55** |

形状：**11-10 早上两发定江山（46→56.6）→ 停 12 天 → 11-23 09:37 与 09:54 连跳两格到 75.54**。
之后 11-23 的 12 发全部落在 61～74.3，没有一发越过 75.54 —— 该题的最高分停在 09:54。

### 1.6 PReluGradUpdate：11-22 半小时内打完 21 分

| 时刻 | SHA | 分 | Δ |
|---|---|---:|---:|
| 11-10 04:30 | `0761133e995c` | 56.56 | 首发 |
| 11-10 07:48 | `33363f041707` | 56.84 | +0.28 |
| 11-18 07:38 | `2a2953126eed` | 57.81 | +0.97 |
| 11-18 10:00 | `7efadf8d4370` | 62.44 | +4.63（**仅 10 行源码差**，见 §2.3） |
| 11-22 13:42 | `29a557a129c5` | 65.74 | +3.30 |
| 11-22 13:44 | `1baa85444835` | 71.55 | +5.81 |
| 11-22 13:50 | `03998e43a3c3` | 75.04 | +3.49 |
| 11-22 14:21 | `11bd996f0ef5` | **78.01** | +2.97 |

形状：**11-10 首发 56.6 → 11-18 微动 → 11-22 13:42～14:21 四发把 62.44 抬到 78.01（+15.57）**。全题只有 14 次判题，是最省的一题。

### 1.7 PadV3Grad：V3 一次 +10，然后磨了 30 发

| 时刻 | SHA | 分 | Δ |
|---|---|---:|---:|
| 11-10 02:18 | `8d5eaa9b3a44` | 27.12 | 首发 |
| 11-10 09:29 | `0fc367de124f` | 30.38 | +3.23 |
| 11-18 06:28 | `49f905f4157a` | 40.38 | **+10.00** |
| 11-22 15:10 | `1dc3411c15e5` | 42.77 | +2.39 |
| 11-22 15:16 | `d13d44d0b3a6` | 43.62 | +0.85 |
| 11-22 16:05 | `37fd777790a8` | 51.54 | **+7.92** |
| 11-22 19:35 | `37fd777790a8` | 51.85 | +0.31（同二进制） |
| 11-23 09:38 | `e3cd79cc5349` | 54.86 | +3.01 |
| 11-23 10:19 | `d8ad9f15be13` | 55.08 | +0.22 |
| 11-23 12:10 | `398d58753951` | 58.99 | +3.91 |
| 11-23 12:21 | `b6600e108969` | **59.98** | +0.99 |

形状：**11-10 起于 27 → 11-18 V3 引擎 +10 → 11-22 全天 16 发只在 40～51.9 之间挪动 → 11-23 上午三发再 +8**。
该题 33 次判题，11-22 中午起连续 6 发不破纪录（39.71/39.75/40.18/36.97/40.16），是典型的"桶不匹配"平台期；
转折点是 11-23 的 `.o` 体积体检（§3.4）。

### 1.8 QuantUpdateScatter：两次结构级跃迁，最终 86.61 是全场最高分

| 时刻 | SHA | 分 | Δ |
|---|---|---:|---:|
| 11-10 06:25 | `956ecc93c03d` | 47.15 | 首发 |
| 11-18 05:51 | `d5244737fa3a` | 49.04 | +1.89 |
| 11-18 08:38 | `7d6aa463fe3c` | 64.30 | **+15.26** |
| 11-22 11:33 | `e946343b8f41` | 65.05 | +0.75 |
| 11-22 12:00 | `bfc6420a2618` | 65.81 | +0.76 |
| 11-22 12:32 | `bfc6420a2618` | 67.83 | +2.02（同二进制） |
| 11-22 12:59 | `e29b28533f0b` | 67.84 | +0.01 |
| 11-22 14:54 | `28b9ce34802f` | **85.89** | **+18.05** |
| 11-22 16:23 | `0aa17ab3c7e7` | **86.61** | +0.72 |

形状：**11-10 首发 47 → 11-18 一次 +15.26 → 11-22 14:54 一次 +18.05 直接干到 85.89**。
+18.05 是全部 74 步里第二大的真实涨幅，且此后 7 发（80.07/83.62/77.13/80.17/84.40/80.35）都没超过它。

### 1.9 StridedSliceGrad：45 发、没有大步、全靠累计

| 时刻 | SHA | 分 | Δ |
|---|---|---:|---:|
| 11-10 02:47 | `10a9c2abbd04` | 47.48 | 首发 |
| 11-10 05:16 | `c48e3cf3dcbc` | 48.90 | +1.42 |
| 11-10 08:28 | `45eb08f08878` | 48.96 | +0.06 |
| 11-18 09:48 | `f2cee3b1691a` | 52.54 | +3.58 |
| 11-18 09:56 | `f2cee3b1691a` | 56.33 | +3.79（**同二进制**） |
| 11-22 11:42 | `e6e64ca3c9c7` | 56.60 | +0.27 |
| 11-22 12:54 | `9585ce52256f` | 59.34 | +2.74 |
| 11-22 13:29 | `8dca63c50c09` | 61.75 | +2.41 |
| 11-22 14:05 | `dd7de480db02` | 62.99 | +1.24 |
| 11-22 14:18 | `306c457a2e01` | 67.63 | **+4.64** |
| 11-22 17:47 | `ba4a357b94d9` | 71.57 | +3.94 |
| 11-23 09:51 | `dd1fefce14af` | 72.13 | +0.56 |
| 11-23 11:09 | `0c9b3bc9b514` | 72.20 | +0.07 |
| 11-23 12:42 | `a031e4e98a7c` | **72.45** | +0.25 |

形状：**11-10 首发 47.5 → 11-18 一次 +7.4（其中一半是同二进制重判）→ 11-22 密集小步 → 11-23 只有 0.07/0.25 的收尾**。
最大单次真实涨幅只有 +4.64，却有 45 次判题（全场最多）——这题的 +23.49 是"每发 +1～3 分 × 十几次"堆出来的，
判题耗时也最长（约 8～9 min/发），是最贵的一题。9 次低于历史最高；另有 6 次 `Pass` 但分数未被记录（§0.4），
这 6 次的真实水平未知，所以不能算作失败。

### 1.10 SyncBatchNormGatherStats：10 发里 7 发在互相打架

| 时刻 | SHA | 分 | Δ |
|---|---|---:|---:|
| 11-10 04:48 | `544439ed46c3` | 57.45 | 首发 |
| 11-22 12:55 | `de25877ccc37` | 68.03 | **+10.58** |
| 11-22 14:50 | `59b5c9a75b3e` | 71.81 | +3.78 |
| 11-22 15:52 | `73046fe82ea3` | 72.26 | +0.45 |
| 11-22 17:07 | `9c943d326f2b` | **72.95** | +0.69 |

形状：**11-10 首发 57.45 → 停 12 天 → 11-22 中午一次 +10.58 → 三发小步到 72.95**。
只有 10 次判题，却有 4 次是同二进制重判（57.45→54.96→56.20→52.47），说明这题一直在"重投旧版找回分数"。

---

## 2. 关键突破清单

判定规则（两条取并集，去重）：
(a) 历史最高分提升 ≥2 分的回执；(b) 每题提升最大的 3 次。
按 §0.2 的结论，**二进制未变的重复回执不算突破**，移入 §2.3。

### 2.1 详细卡（每题最大的一发或两发）

**卡 1 · CTCLossV2 `0d5125c` +15.67（37.18→52.85）**
- 回执 `6afd410585ad43bbe081d148`，2026-11-18T06:11:31Z，判题 2.4 min；之后被 `2b9d49d` 继续推高，无回退。
- 手法：把 DP 的内层改成**环形缓冲 + 窗口切片 gather**，`log_probs` 异步双缓冲，TPipe 提到 kernel 外；
  同时修掉 gather 路径的行读写竞争（这正是 11-11 首发那条 note 里"9 条 -inf lane"的真凶）。
  树差 18 文件 / kernel+host 净增 1034 行。
- 代码证据：`git -C ict-cann-nov/CTCLossV2 log --oneline 02c2048c..0d5125cd` 里三条关键提交：
  `ae55db7 Localise the gather path's nondeterminism to a row read/write hazard` /
  `e9ff529 Row double buffering makes the gather path deterministic` /
  `cccd474 Ring-buffered window-sliced gather DP with async double-buffered log_probs`。
  注意被提交的 HEAD 本身只是 `0d5125c Restore read-only template files`，真正的算法在这三条里。
- 由谁产出：`c65a2177`（agent `self`，父 `cacd27d9`）第 224～225 步：commit → push → `tools/cj submit`。
  [证据: c65a2177#225]
- 触发线索：**失败现象驱动**。11-11 首发 note 已写 `in-chain PipeBarrier (real cause of the 9 -inf lanes)`，
  之后 11-18 里连续三条提交都在定位同一个 nondeterminism，属于"先修对再看分数"。
  [证据: 回执 6af3da246bd0f9be37281425 note]
- 此前同方向：11-10～11-18 之间这题只有 1 次判题（首发 37.18），所以严格说是**第 1 次尝试就成**；
  但失败的本地尝试至少有 8 条提交（`b120e60`/`ae55db7`/`e9ff529`/`7260e27` 等），未计入回执。


**卡 2 · QuantUpdateScatter `28b9ce3` +18.05（67.84→85.89）**
- 回执 `6b03019285ad43bbe03cb47d`，2026-11-22T14:54:35Z，判题 2.6 min；此后 6 发都没追上（77.13～84.40），
  直到 16:23 的 `0aa17ab` 才以 86.61 再破一次。
- 手法：**V2 零 TPipe 裸金属 CCE 架构**——4 个极瘦 schMode（TinyInUB / FastCCE / div-free FusedGroupCCE /
  4-slot-ring StreamCCE），单次 `set_mask_count` 完成量化，索引 ≤8 直接内联；收尾把 lambda 换成
  `__aicore__ ChunkCursor` 结构体（消栈）。树差 5 文件 / 2444 行。
- 代码证据：`1efaeaa E19: V2 zero-TPipe bare-metal CCE architecture (4 razor-thin schModes …)` +
  `28b9ce3 E19.2: replace lambda with __aicore__ ChunkCursor struct`。
- 由谁产出：`0405365d`（agent `carbon-quantupdate`）第 50/163 步首次出现；同 SHA 在 `240583c7`（Track A）第 606 步、
  `a6c56b2b` 第 100 步出现（转述）。真正的提交动作在 `0405365d` 这条 carbon 线上。
  [证据: 0405365d#50]
- 触发线索：**架构经验迁移**。11-18 的 E9（`7d6aa46`，+15.26）已经证明"零 TQue + FusedGroupPass + 动态 colTile"
  在线上有效；11-22 把这套推到"零 TPipe 裸金属"，note 写 `d_hid=+12.63, d_pub=+0.77, improved=11`。
  [证据: 回执 6b03019285ad43bbe03cb47d note]
- 此前同方向：11-18 的 E7/E9（`d524473` +1.89、`7d6aa46` +15.26）是同族的两次前进；
  11-22 上午的 E15（`becddb8`）TLE 记 0、E18（`e29b285`）只 +0.01 —— 同一方向在成功前有 1 次 TLE、1 次无效。


**卡 3 · LayerNormV3 `af0807c` +16.53（27.13→43.66）**
- 回执 `6afd3eb685ad43bbe07d3685`，2026-11-18T06:05:22Z，判题 6.1 min；之后被继续推高，无回退。
- 手法：**ping-pong + 原生 fp16 + bf16 TransDataTo5HD SIMD（约 9x）+ ReduceSumMultiN + 单 TBuf arena**，
  一次改 13 文件 1838 插入。核心是把逐行 ReduceSum 换成 `ReduceSumMultiN` 批量归约、并让 bf16 走硬件转置指令。
- 代码证据：`git -C ict-cann-nov/LayerNormV3 show af0807c --stat` =
  "Optimize LayerNormV3 with ping-pong, native fp16, bf16 transpose SIMD, ReduceSumMultiN, single-TBuf arena"。
- 由谁产出：`10f0bfd7`（agent `self`，父 `cacd27d9`）第 340～341 步：`git add -A && commit && push`，随后取 SHA。
  [证据: 10f0bfd7#340]
- 触发线索：**换实现路径（文档/硬件指令）**，不是剖析数据。这台 worker 在 11-10 留下的 best 只有 27.16，
  11-18 直接按"fp16 原生 + 硬件转置 SIMD"重写，note 只报了 `75/75 DET green`（先保正确性）。
  [证据: 回执 6afd3eb685ad43bbe07d3685 note]
- 此前同方向：11-10 的 `07ae054b` 因为"fp32 accumulate + one rounding"被判 WA 记 0，
  `bf2beb9a` 两次回滚到 26.81/27.13 —— 同一数值路径方向先失败过 1 次 WA + 2 次回滚。


**卡 4 · QuantUpdateScatter `7d6aa46` +15.26（49.04→64.30）**
- 回执 `6afd636985ad43bbe0c83f25`，2026-11-18T08:38:07Z，判题 2.2 min；后被 28b9ce3 超过，无回退。
- 手法：`SCH_MODE_0` 零 TQue 快核 + `FusedGroupPass` 分组融合 + 动态 `colTile`/`copyStep`；
  另有两条配套修复（LP64 模板推导、Phase-B bash 保持 cwd 以便批量 msprof）。
- 代码证据：`6a89b3c Specialize SCH_MODE_0 zero-TQue fast kernel, FusedGroupPass, and dynamic colTile/copyStep`。
- 由谁产出：`240583c7`（agent `self`，父 `95eafe6a`，QuantUpdateScatter 专责）第 8 步首次出现。
  [证据: 240583c7#8]
- 触发线索：**逐例耗时 + 分桶**。同批提交里带 `batch 14-stratum msprof into one session`，
  说明这一发是按 14 个 stratum 的 msprof 结果分别调参的。
- 此前同方向：11-18 05:51 的 E7（`d524473`）只 +1.89，note 自述 `zero-regression E7: 4/6 bands improved`，
  即 E1–E6 六次都没换来分（无回执），E7 才 +1.89，E9 才爆发。**同方向失败 6 次**（E1–E6 未入回执）。


**卡 5 · SyncBatchNormGatherStats `de25877` +10.58（57.45→68.03）**
- 回执 `6b02e5c585ad43bbe0f8354e`，2026-11-22T12:55:07Z，判题 1.7 min；此后三发继续 +3.78/+0.45/+0.69，无回退。
- 手法：**修线上 WA 的数值路径**——bit-exact 12-bit split（`invHi+invLo`）除法、
  `[1e-14,1e16]` 夹紧的 `vrsqrt + 2×Newton`、`dcci` 计数、`n<=0` 兜底，外加 11 条 escape 复现用例。
  这一发是把"能过但算错"变成"能过且算对"，分数才上来。
- 代码证据：`c0a7b8b V11: fix online WA (bit-exact invHi+invLo div, [1e-14,1e16] clamped vrsqrt+2NR, dcci count, n<=0 fallback)`。
- 由谁产出：`6344d8e3`（agent `carbon-syncbn`，父 `95eafe6a`）第 168 步首次出现；Track A 的 `b26d4ec5` 第 394 步转述。
  [证据: 6344d8e3#168]
- 触发线索：**线上 WA**。11-22 12:12 与 12:22 两发（`1795a856`、`080ee029`）都是 WA 记 0，
  紧接着 12:55 就出了这一发修复 —— 是被判题打回来逼出来的。
  [证据: 回执 6b02dbc485ad43bbe0dd1dd9 附近两行 WA 记录]
- 此前同方向：V4b/V7/V8/V9 四代都在改数值路径（`4843789`、`1906b7d`、`1795a85`、`080ee02`），
  其中 V9 又被线上判 WA，前后至少 5 次同类尝试才落到 V12。


**卡 6 · StridedSliceGrad `306c457` +4.64（62.99→67.63）——全题最大单步**
- 回执 `6b02f7bc85ad43bbe024a34a`，2026-11-22T14:18:58Z，判题 8.9 min；后被 ba4a357 推高。
- 手法：**零 TPipe/TBuf 静态 LocalTensor UB arena + 精确增量清零 + 把 BuildSuperGatherTable 缩小 13～128 倍
  （批量 1-sync 奇数 Q 种子）+ 把 zero-Odo 的 RunFast2D 推广到所有 outer stride**。
- 代码证据：`669d01d v8: zero-TPipe/TBuf static LocalTensor UB arena, exact-size incremental zeroing, 13x-128x smaller BuildSuperGatherTable …`
  + `306c457 v8a: fix PIPE_ALL global namespace`。
- 由谁产出：`3d7f1d11`（agent `self`，父 `95eafe6a`，StridedSliceGrad 专责）第 6 步首次出现本 SHA（该 worker 共 19 次命中）。
  [证据: 3d7f1d11#6]
- 触发线索：**msprof 逐级剖析**。同族前一发 `f2cee3b` 的 note 直接写
  `13-level msprof min sum 221.5(c0630a2)->134.38us (1.65x): blocks sized by element/256 not traffic`。
  [证据: 回执 6afd745285ad43bbe0ea04ba note]
- 此前同方向：`c0630a2e` 的 "host-sized work unit" 退 -1.90、`edc7d7ba` 的配对比测 -2.90 —— 两次失败后才换到"按元素数分块"。


**卡 7 · ConfusionTransposeD `31affc6` +9.32（56.40→65.72）**
- 回执 `6b02ed4685ad43bbe00b6cad`，2026-11-22T13:29:53Z，判题 4.4 min；随后 3938863 继续 +8.39。
- 手法：**40 的约数核数阶梯（1/10/20/40）+ 零 Cast 的 u32 TinyLut（≤240 word）+ 20-block ≤64KB mid-UB**。
  即"按数据量在 1/10/20/40 核之间跳"，避免 10 核这类会触发 DVFS 抬频的核数。
- 代码证据：三条连续提交 `a019b43 1-block dispatch for <=8KB … zero-idle proportional blocks` →
  `2d92899 6-block warm low-dispatch on TINY_LUT/SMALL_VEC` → `31affc6 divisor-of-40 block ladder (1/10/20/40) + zero-Cast u32 TinyLut`。
- 由谁产出：`0418d94b`（agent `self`，父 `95eafe6a`，ConfusionTransposeD 专责，17 次命中）第 286 步首次出现；
  carbon 线 `12ba4615` 第 42 步更早提到。
  [证据: 0418d94b#286]
- 触发线索：**核数/DVFS 经验从别题迁移**。同一发之前 `4b4303f`（SCH_TINY_LUT + SCH_SMALL_VEC）大跌到 34.05，
  而 11-23 的 Confusion 复盘明确把"消灭 10 核 DVFS 尖峰"列为最大单项（`f32_256x256 mean 8.74->2.66`）。
  [证据: 95eafe6a 看板 board.jsonl #658]
- 此前同方向：`4b4303f` 一次 −16.06（34.05）是这道题最惨的一次；`d8eb6da` 把 SMALL_VEC 上限收到 ≤8KB 才回 +6.29。


**卡 8 · PadV3Grad `37fd777` +7.92（43.62→51.54）**
- 回执 `6b03125485ad43bbe065e97e`，2026-11-22T16:05:36Z，判题 2.1 min；后 11-23 继续推高。
- 手法：**V12.0 H-First 纯矢量 2D 引擎**（tiny/pub/bigpad 三档）+ Zero-Stack 1D + Plane-Aligned Pack3C +
  周期 u64 mask + 批量 fp16 H-fold。方向从"逐 tile 搬"改成"先按 H 方向折叠再一次性矢量写"。
- 代码证据：`37fd777 V12.0: H-First Pure-Vector 2D engine (tiny/pub/bigpad) + Zero-Stack 1D + Plane-Aligned Pack3C + Periodic u64 Mask + Batched fp16 H-fold`。
- 由谁产出：`f0e528e7`（agent `pad-int`，父 `813867cb`）第 12 步首次出现；`2eff9ddc`(pad-ml-pipe) 第 30 步、`59516056`(pad-recon) 第 36 步转述。
  [证据: f0e528e7#12]
- 触发线索：**`.o` 体积横向体检**。看板 #19 的原话是对 10 题 NPU `.o` 与 pub#1..5 做横向 CT 扫描后
  "`PadV3Grad` 的 `.o` 高达 `172 KB`（全 10 题最肥）"，#20 随即播报"`37fd777` 从 42.45 狂飙到 51.54"。
  [证据: 95eafe6a 看板 board.jsonl #19/#20]
- 此前同方向：11-22 当天 `fe57767`/`dcd6fe4`/`f81661d`/`29afe3e`/`b759e91`/`bcce905` 六发都在 36.97～40.18 徘徊
  —— **连续 6 发不破纪录**，是全场最长的平台期。


**卡 9 · MultilabelMarginLoss `13c554d` +10.55（64.99→75.54）**
- 回执 `6b040cef85ad43bbe05e1f4c`，2026-11-23T09:54:42Z，判题 1.9 min；此后 12 发均未超过，无回退。
- 手法：**把 suffix-sum 的 vnchwconv 内联进 RunSortSuffixPhase2**，配 zero-WAR 的 `CompareScalar/Maxs`、
  `TBuffAddr MakeUb`、overlapped stats、`8-stride >8c`。核心是消除 Sort 阶段那次跨阶段搬运。
- 代码证据：`13c554d b-track2 v8: inline vnchwconv suffix-sum in RunSortSuffixPhase2 + zero-WAR CompareScalar/Maxs + TBuffAddr MakeUb + overlapped stats + 8-stride >8c`。
- 由谁产出：`85aad6a0`（agent `mml-t2`，父 `813867cb`）第 696 步；`fbe77bd2`（mml-arch）第 297 步在架构分析里先提到。
  [证据: 85aad6a0#696]
- 触发线索：**记因式分解（2×2 factorial）**。看板 #500/#501 用表格把 `#A ALGO_SORT_TWO_HALF`、`#B noinline RunC1Multi`、
  `#C 4-way odd-row hist`、`#E` 四项在线上的单独贡献算出来，然后只组合赢的那几项。
  [证据: 813867cb 看板 board.jsonl #500/#501]
- 此前同方向：`b767ceb` −14.16、`32e3c248` TLE 记 0，同一批里还有 6 发落在 68.67～74.32 ——
  即 **14 次尝试里只有 1 次越过 75**。


**卡 10 · ConcatD `c8b5521` +7.14（57.17→64.31）**
- 回执 `6b02f57285ad43bbe01f1b03`，2026-11-22T14:03:55Z，判题 3.6 min；随后 aa74f56 继续 +6.69。
- 手法：**exp-b 线把 kernel 拆成 7 个极瘦零溢出的 schMode**（512B GM 对齐、固定 TILE2D colCores、
  VEC32 `minW<=64`、`noinline Ring4`）。本质是"按形状分派到专用小内核，每个都不超 L1I"。
- 代码证据：`c8b5521 [exp-b] v2: 7 razor-thin zero-spill schModes + 512B GM alignment + fixed TILE2D colCores + VEC32 minW<=64 + noinline Ring4`。
- 由谁产出：`7018749e`（agent `self`，父 `95eafe6a`，ConcatD Track A）第 187 步：
  在 `.worktrees/ConcatD-expB` 里 `git add … && commit -m "[exp-b] v2: 7 razo…"`，随后 `opsb test ConcatD`。
  [证据: 7018749e#187]
- 触发线索：**双轨竞争（exp-b vs main）**。同一时刻 `a763738c` 在跑 main 线，两线各自提交、比线上分再合流。
  [证据: 95eafe6a 看板 board.jsonl #11]
- 此前同方向：11-18 的 `2dd9acd`（UB Vector Gather）退 −6.58，11-10 的 `3c15ca9` 退 −5.13；
  11-22 11:44 的 `0d6ea06` 只 +1.72。


**卡 11 · PReluGradUpdate `1baa854` +5.81（65.74→71.55）**
- 回执 `6b02f13785ad43bbe014d6a3`，2026-11-22T13:44:09Z，判题 1.9 min；随后 03998e4/11bd996 继续 +3.49/+2.97。
- 手法：**exp-b 裸金属 V1 系列**——bare-metal CCE + `GET_TILING_DATA_PTR_WITH_STRUCT`（56B，不整块拷栈）+
  constexpr 48 bank UB + 硬件 2D `ROW_2D_PAD` + 三缓冲 bf16 + `vbrcb`；V1.2 补了 bf16 `vconv/vsel` 的
  `pipe_barrier` 与 GATHER tile 放大（3840/6912）。
- 代码证据：`6245362 [exp-b] V1` → `091e954 V1.1` → `1baa854 [exp-b] V1.2: fix bf16 vconv/vsel RAW pipe_barrier + gate ROW_2D_PAD B + enlarge GATHER tile 3840/6912`。
- 由谁产出：`04944d3e`（agent `self`，父 `95eafe6a`，PRelu 专责）第 601 步；`d9ffac18` 第 188 步转述。
  [证据: 04944d3e#601]
- 触发线索：**逐例时间瓶颈在 pipe barrier**（V1.2 的修复项就是 RAW 依赖的 barrier），不是分数驱动。
- 此前同方向：11-10 的 `2d5837e6` 退 −2.36；11-22 13:04 的 `405329e`（V4 零 TPipe）退 −2.08 —— 同方向先失败 2 次。


**卡 12 · PadV3Grad `49f905f` +10.00（30.38→40.38）**
- 回执 `6afd452f85ad43bbe08a31ef`，2026-11-18T06:28:37Z，判题 1.7 min；后被继续推高。
- 手法：**V3 多档可分向量化引擎**（按 padding 形态分 regular / bigpad / 边界档），
  `bigpad_p64 1523->12.3us`，一次把该题从"逐元素"变成"按档位选内核"。树差 15 文件 / 1829 行。
- 代码证据：`49f905f Implement V3 multi-regime separable vectorized PadV3Grad engine and restore read-only template hashes`。
- 由谁产出：`8630232c`（agent `self`，父 `cacd27d9`，PadV3Grad 专责）第 229～230 步：
  `git push` 后取 SHA；同 SHA 在 `a66cda31`/`a42d5e8e` 第 12 步被转述。
  [证据: 8630232c#229]
- 触发线索：**逐例耗时**（note 直接报 `bigpad_p64 1523->12.3us`、`340/340 green`）。
- 此前同方向：11-10 的 `ac71b7ba` 退 −0.78（paddings 改标量 GM load），11-11 的 `0fc367de` 只 +3.23。

### 2.2 压缩卡（其余 ≥2 分且二进制有变的台阶）

| # | 题 | SHA | 回执 sid | 时刻 | Δ | 代码层手法 | 产物对话 |
|---|---|---|---:|---:|---|---|---|
| 13 | CTCLossV2 | `1af7003` | 6b02d981 | 11-22 12:03 | +4.42 | V4–V6：Even/Odd 拆 DP 把 10 barrier 降到 7/8、direct vgather、多核 batch、再按 schMode 隔离 | 59faaca6#171 (self/CTC) |
| 14 | CTCLossV2 | `be12685` | 6b02efad | 11-22 13:38 | +3.70 | V8–V11：带守卫的 3-barrier 线性 DP、hoisted cmpmask+vsel、`2^-120` 计数器、5-schMode 细分 | 59faaca6 (self/CTC) |
| 15 | CTCLossV2 | `0c769ba` | 6b0310d5 | 11-22 15:59 | +3.60 | V21：隔离 SCH_MODE_1/4，把 RunInterleavedLinear 扩到 `kSaf=128` | ea0d896c#73 (ctc-arch) |
| 16 | CTCLossV2 | `cca4860` | 6b040e9e | 11-23 10:02 | +3.28 | T2-V9a：零 TPipe raw-CCE 单步 EO 线性 DP（3 bar/step、2 vgather/step、`-4KB .text`） | ea0d896c#233 |
| 17 | CTCLossV2 | `dcc7754` | 6b0431fa | 11-23 12:33 | +2.58 | T2-V18：砍掉 `RunWideLinBlockTilt`（−10KB `.text`，≤66KB），保留 6-reg `MidFlushCtx` 修 hang | d36fa8fc#1517 (ctc-t1) |
| 18 | CTCLossV2 | `2b9d49d` | 6afd6b82 | 11-18 09:12 | +3.36 | 只改 2 行：`lossBuf` 补到 64 float，避免 Level-0 尾部 Exp/Adds 压到 `packBuf0`（属正确性修复，涨幅主要来自 MAP 选型） | 59faaca6#32 |
| 19 | ConcatD | `aa74f56` | 6b02ffa4 | 11-22 14:47 | +6.69 | V3：半片免端口争抢核数阶梯 1/4/8/10/20/40 + `VREDUCEV2` TinyPack schMode=7（pub1-5 单次 store） | a763738c (self/main) |
| 20 | ConcatD | `4832524` | 6b03061e | 11-22 15:15 | +4.60 | exp-b v8：零索引 `TransDataTo5HD<u16>` ZIP2 交织 + 16KB Gather4 + 半片上限 | 68ceb146#18 (carbon-concatd) |
| 21 | ConcatD | `0d6ea06` | 6b02d4fd | 11-22 11:44 | +1.72 | 32B 对齐的跨核行切分 + 干净的 4/8/16/32/40 核斜坡 + 原生 `DataCopy IMAGE32` | 7018749e#22 |
| 22 | ConfusionTransposeD | `3938863` | 6b02f7af | 11-22 14:14 | +8.39 | exp-b V2/V3：10 个 kernel 全部零 TPipe/零 TBuf + 1-block 零 Cast `SCH_LUT_FAST` + 32..1024B `vcopy` + 3 档 prefetch | 0418d94b#274 |
| 23 | ConfusionTransposeD | `d8eb6da` | 6b02e7e4 | 11-22 13:06 | +6.29 | 收 SMALL_VEC 到 ≤8KB/2048 word（恢复 16 核 `rev_3d_16`）+ 半片 20-block 派发（**代码仅 8 行**） | 0418d94b#22 |
| 24 | ConfusionTransposeD | `d4d090f` | 6afd62a8 | 11-18 08:35 | +5.28 | 规范化合并（canonical coalescing）+ 全 dtype 通用双缓冲 Gather 转置 | d98bf146#18 (self) |
| 25 | LayerNormV3 | `8c8c164` | 6b02f4b3 | 11-22 14:03 | +5.85 | v11：零 TPipe/TBuf SCH_MODE_0/1 + 通用 1..8192 `BlockReduceSum` & 2D DMA，修 pingpong/bf16/宽行三处悬崖 | cf625085#609 |
| 26 | LayerNormV3 | `e21fdce` | 6b02db04 | 11-22 12:13 | +5.83 | v5：3-schMode 单 tile/pingpong 隔离 + 双峰 40 核暖机 + `BlockReduceSum` 1-2 barrier + resident==2 参数双缓冲 | cf625085#150 |
| 27 | LayerNormV3 | `9773dff` | 6b02f6fd | 11-22 14:13 | +2.10 | v12：零 TPipe/TBuf SCH_MODE_0/1 + 通用 `BlockReduceSum` + 融合 | cf625085#649 |
| 28 | MultilabelMarginLoss | `9f92b0a` | 6b02e144 | 11-22 12:36 | +5.29 | 4-TILING_KEY 二进制隔离（KernelTiny<5KB / KernelMid2D / DirectLoop / Sort）+ 双行 64b 流水直方图 + `-FLT_MAX` 掩码 | 8f4190e0 (self/MML) |
| 29 | MultilabelMarginLoss | `8aca680` | 6b0408fd | 11-23 09:37 | +3.06 | b-track2 v1：零 TPipe 64B tiling + KernelMicroSmall + DualCompact 角色互换 + 64B workspace 握手 | 813867cb#364 |
| 30 | PReluGradUpdate | `29a557a` | 6b02f0bc | 11-22 13:42 | +3.30 | exp-b V1 裸金属 CCE + 48 bank UB + 硬件 2D ROW_2D_PAD + 3 缓冲 bf16 | 04944d3e#522 |
| 31 | PReluGradUpdate | `03998e4` | 6b02f2ce | 11-22 13:50 | +3.49 | exp-b V2：32B 打包 tiling（4×u64 + `SetDataSize`）+ 零掩码改写大内区 Muls + 48-bank Gather/bf16 着色 | 04944d3e#609 |
| 32 | PReluGradUpdate | `11bd996` | 6b02fa10 | 11-22 14:21 | +2.97 | exp-b V3：按 `d_hid=+6.56` 的桶证据继续加宽 tile / 减少 gather 次数 | d9ffac18 / 60e41d06#198 |
| 33 | PadV3Grad | `398d587` | 6b042cc3 | 11-23 12:10 | +3.91 | Round-4 集成：`64B` 交织 `dims[64]` tiling + 零 `PreLoad` + 真 `1c`；线上 Public 4.825→4.628us 破纪录 | 13a8bc5d#522 (pad-arch) |
| 34 | PadV3Grad | `e3cd79c` | 6b04091d | 11-23 09:38 | +3.01 | 38 桶校准后的选型提交（Pairwise-v2, `gamma=0.94`），源码树相对上发净差 3149/6281 行 | 813867cb#382 |
| 35 | PadV3Grad | `1dc3411` | 6b030562 | 11-22 15:10 | +2.39 | V9.0 双引擎零 TPipe SCH4（1D-burst 多平面 `vreducev2` + DirectPadShift 3 槽环） | a42d5e8e#17 |
| 36 | PadV3Grad | `d13d44d` | 6b0306dd | 11-22 15:16 | +0.85 | V11.0 零 TPipe 静态 UbArena + 直取 `__gm__` tiling + 裸 `__ubuf__` tiny | a42d5e8e#74 |
| 37 | StridedSliceGrad | `ba4a357` | 6b0328be | 11-22 17:47 | +3.94 | exp-b v14-fix：给 `RunSmallStridedCold` 的 64 元素 `kInfOff` 尾巴补零（**仅 6 行**，修一个 WA） | 9575f4fa#23 |
| 38 | StridedSliceGrad | `f2cee3b` | 6afd7270 | 11-18 09:48 | +3.58 | 重写 v2：维度合并 + 按流量定核数 + 多行 Gather + 单次突发小 UB；第二发（同二进制）再 +3.79 | 3d7f1d11#6 |
| 39 | StridedSliceGrad | `9585ce5` | 6b02e442 | 11-22 12:54 | +2.74 | v4：17 例 msprof 中位 185.90→124.24us(1.50x)、最小 172.72→107.96us(1.60x) | bd9c83a5#13 |
| 40 | StridedSliceGrad | `8dca63c` | 6b02ec50 | 11-22 13:29 | +2.41 | v5：`RunEngine2` 改 noinline（Engine 0/1 消 Odo 栈）+ Super-Row 2.1C/2.1B 批处理 + 通用 interior `BuildStridedRow` | bd9c83a5 (self) |
| 41 | SyncBatchNorm | `59b5c9a` | 6b0300d4 | 11-22 14:50 | +3.78 | V14：2201 个 4KB bank 着色的 UB arena（偶 v / 奇 s 交替）+ 100% 无读写冲突的向量排程 | 6344d8e3#168 |
| 42 | SyncBatchNorm | `0aa17ab` | 6b031672 | 11-22 16:23 | +0.72 | E28：两操作精确 RNE 转 cast（`vconv_f322f16o` + `vconv_f162s8r`）腾出 16KB UB 给 48KB×2 | 60e41d06#198 |

> 说明：上表"回执 sid"取前 8 位；"产物对话"列给的是**该 SHA 在对应阶段 worker 里首次出现的步号**，
> 不一定是 `opsb submit` 的那一步（部分 submit 输出未落盘到 transcript）。标 `#` 的可直接 grep 复核。

### 2.3 不算涨分的"涨分"（噪声/重判，已从手法归因剔除）

| 题 | SHA | Δ | 性质 | 证据 |
|---|---|---:|---|---|
| LayerNormV3 | `ec0867dd795b` | **+15.96** | 同一次排队被重判两次（`queued_at` 相同） | 回执 6b02dd43 / 6b02dec1 |
| StridedSliceGrad | `f2cee3b1691a` | +3.79 | 同二进制，两次判题 52.54/56.33 | 回执 6afd7270 / 6afd7452 |
| QuantUpdateScatter | `bfc6420a2618` | +2.02 | 同二进制，65.81→67.83 | 回执 6b02d8c1 / 6b02e040 |
| ConcatD | `474dc59b3f64` | +1.86 | 同二进制，76.10→77.96（note 为 `restore best validated SHA`） | 回执 6b031a69 / 6b032403 |
| ConcatD | `7919dcbfac7b` | +0.99 | 同二进制，note 自认 `same-SHA` | 回执 6b02d147 |
| ConfusionTransposeD | `8e7ba6bc0da3` | +0.90 | 同二进制三判 78.18/74.39/79.08 | 回执 6b034474 |
| PadV3Grad | `37fd777790a8` | +0.31 | 同二进制，51.54→51.85 | 回执 6b034385 |
| LayerNormV3 | `9773dff47a13` | +0.25 | 同二进制，73.40→73.65 | 回执 6b02f891 |
| PReluGradUpdate | `7efadf8d4370` | **+4.63** | 源码树仅差 10 行（`Deduplicate BUILD_GROW scalar GM weight reads`），note 自认 `no perf telemetry found` | 回执 6afd76b5 |

合计 **30.71 分**（严格同二进制 26.08 + 存疑 4.63），占 331 的 **9.3%**。
[证据: 上表逐行，全部可用 `results/submit-queue-outcome.jsonl` 复核]

---

## 3. 按手法类别汇总

### 3.1 归因方法（先说清楚怎么分的）

- **样本**：§0.1 的 74 次上台阶，合计 331.36 分，与方法论上"421→752.3"的口径完全一致。
- **单标签法**：每次台阶只打一个"主手法"标签，**不按 note 里的多个数字分摊**。
  依据顺序：① 该次提交的**源码树净差**（`git diff <上一判题 SHA> <本次 SHA> --numstat`，只数 `op_kernel/`+`op_host/`）
  ② 树差里各 commit 的 message ③ 回执 note 里的实测数字。
  之所以不按 note 分摊：note 是"我怎么选中它"（Simplex-MAP / Pairwise-v2）而不是"它改了什么"，
  两者经常不是一回事（例：`8e7ba6bc` 的 note 写 Simplex-MAP，树差却是 2749 行真实代码）。
- **二进制未变的重判**单独归入 G1 类，不进 A–F。
- **闭合校验**：A1+A2+B+C+D+E+F+G1+G2 = 331.36，误差 0。

### 3.2 分类结果

| 类别 | 分值 | 占比 | 说明 | 代表 SHA |
|---|---:|---:|---|---|
| **A1 算法/数据流结构重写** | 111.18 | 33.6% | 换 DP/转置/归约的整体结构：环形缓冲窗口 DP、H-First 纯矢量 2D、可分离多档引擎、suffix-sum 内联、零 TPipe 裸金属 CCE | `02c2048c`(37.18) `0d5125cd`(15.67) `7d6aa46`(15.26) `13c554da`(10.55) `49f905f`(10.00) `37fd777`(7.92) |
| **A2 零 TPipe/裸 UB/schMode 专用化** | 85.53 | 25.8% | 把通用 kernel 拆成多个"极瘦零溢出"专用 schMode + 静态 UB arena + 去 TQue/TBuf | `28b9ce3`(18.05) `3938863`(8.39) `c8b5521`(7.14) `8c8c164`(5.85) `e21fdce`(5.83) `9f92b0a`(5.29) `306c457`(4.64) |
| **D 精度与数值/确定性路径** | 27.83 | 8.4% | 原生 fp16、bit-exact 12-bit split 除法、`vrsqrt+2NR`、精确 RNE cast、哨兵握手位一致 | `af0807c`(16.53) `de25877`(10.58) `0aa17ab`(0.72) |
| **B 核数与调度阶梯（含 DVFS）** | 25.16 | 7.6% | 1/10/20/40 这类"40 的约数"阶梯、半片 20 核避端口争抢、消灭 10 核 DVFS 尖峰 | `31affc6`(9.32) `aa74f56`(6.69) `d8eb6da`(6.29) `0d6ea06`(1.72) |
| **F 选择与发放（桶校准/回滚/重投）** | 36.28 | 10.9% | 用 Simplex-MAP / Pairwise-v2（UCB 多臂）在若干候选 SHA 里挑一发；或用 `restore best validated SHA` 把已验版本重投找回被覆盖的板分 | `1baa854`(5.81) `ba4a357`(3.94) `2b9d49de`(3.36) `29a557a`(3.30) `8aca680`(3.06) `e3cd79c`(3.01) |
| **G1 同二进制重判** | 26.08 | 7.9% | 见 §2.3 | `ec0867d`(15.96) `f2cee3b`(3.79) `bfc6420`(2.02) |
| **C 搬运形状与 tiling** | 7.37 | 2.2% | 单次 1D/2D DMA、`TransDataTo5HD`、Gather 尺寸、ring/双缓冲、对齐 | `4832524`(4.60) `9585ce5`(2.74) `5a8d07f`(0.03) |
| **E ELF/L1I/冷启动** | 7.30 | 2.2% | `.text`/`.o` 瘦身、`inline/noinline`/`static` COMDAT 去重、`tmpl` 数压到阈值内 | `398d587`(3.91) `dcc7754`(2.58) `dd1fefc`(0.56) `a031e4e`(0.25) |
| **G2 存疑（10 行改动 +4.63）** | 4.63 | 1.4% | 见 §2.3 | `7efadf8` |

[证据: 逐条见 §2.1/§2.2，分数来自 /tmp/p19/receipts.json 重算的 74 步阶梯]

### 3.3 这个划分有多可靠：上限/下限/最可能

- **代码类合计（A1+A2+B+C+D+E）= 264.37 分，79.8%**；选型/找回类（F）= 36.28，10.9%；噪声（G1+G2）= 30.71，9.3%。
- **A1 的 33.6% 是上限**：它含 CTC 首发 `02c2048c` 的 +37.18（那是 11-11 把 11-10 的本地成果第一次交上去，
  严格说是"首发"而不是"涨幅"，但因为该题 11-10 记 0，只能算进 331）。扣掉它，A1 = 74.00，占 22.3%。
  下限口径：A1 只在"确有一次结构级重写"时成立，若把"多手法混合、只是一次大提交"的
  `37fd777`/`49f905f`/`13c554da` 也算作 A1，则 A1 ∈ [22.3%, 33.6%]。
- **A2 的 25.8% 是下限**：`schMode 专用化` 常与 `ELF 瘦身` 同时发生（典型如 `c8b5521` 既拆 7 个 schMode 又压 L1I），
  按主标签只记一次，所以 E 类被系统性低估（仅 2.2%）。若把"拆 schMode 同时瘦身"的对半分给 E，
  E 可到 8%～10%，A2 相应降到 18%～20%。
- **F 的 10.9% 是最不确定的一档**：这些提交的源码树确实变了，但 note 全是选型记录，无法证明"分数主要来自选型"。
  - 下限口径（只算 note 为 `restore`/`same-SHA`/纯选型且树差 <500 行：`6606085`(62 行)、`235f754`(428)、
    `b6600e1`(262)、`e946343`(117)、`2a295312`(1712 行但 note 是"2 paired cases"选型)、`0c9b3bc`、`e6e64ca`、
    `d8ad9f1`）= **5.54 分，1.7%**。
  - 上限口径（F 全体）= **36.28，10.9%**。
  - 折中口径：把树差 >1000 行的 7 条（`1baa854` 3026 行、`8aca680` 4045、`e3cd79c` 9430、`dd7de480` 3070、
    `c17a4bc` 2399、`2a295312` 1712、`d8ad9f1` 3053，合计 15.45 分）算作真实代码贡献、移出 F，
    则 F = **20.83 分，6.3%**。
  - 结论：F ∈ **[1.7%, 10.9%]**，最可能 **6%～9%**。区间宽度约 9 个百分点，是本表最大的一处不确定。
- **B 的 7.6% 可信度较高**：该类的三发（`31affc6`/`aa74f56`/`d8eb6da`）commit message 都直接写核数阶梯，
  且 11-23 的 Confusion 复盘给出独立佐证（"ZERO 10c DVFS spikes, f32_256x256 mean 8.74->2.66"）。
  [证据: 95eafe6a 看板 board.jsonl #658]
- **C 只有 2.2%，是本文最反直觉的一项**：`DataCopyPad/tiling` 这类"教科书优化"在这个联赛里几乎不直接换分，
  它总是作为 A1/A2 的配套出现（例 `5a8d07f` 的 32KB 展开流水只换来 +0.03）。

### 3.4 跨题复用过的手法（迁移价值最高）

按"第一次出现"排序。每行给首例 SHA / 回执 / 对话位置，以及后来复用到哪几题。

1. **零 TPipe / 裸 UB 静态 arena**（首例 `1dc3411` PadV3Grad V9.0，+2.39）
   首例 `a42d5e8e#17`（self/PadV3Grad），回执 `6b030562`。复用：→ StridedSliceGrad `306c457`(+4.64)
   → QuantUpdateScatter `28b9ce3`(+18.05) → ConcatD `c8b5521`(+7.14) → LayerNormV3 `8c8c164`(+5.85)
   → MultilabelMarginLoss `5cf3bc7`。11-22/11-23 覆盖 6 题，是复用最广的一招。
   [证据: `git -C ict-cann-nov/PadV3Grad log --oneline` 中 V9.0 提交；看板 #21 把它列为"全算子通用 3 招"之一]
2. **按核数阶梯避端口争抢 / 避 DVFS**（首例 `aa74f56` ConcatD V3 半片 1/4/8/10/20/40，+6.69）
   首例 `a763738c`（self/ConcatD-main），回执 `6b02ffa4`。复用：→ ConfusionTransposeD `31affc6`(+9.32)
   → PadV3Grad `a8e1d70`(1/20/40) → SyncBatchNorm `73046fe8`(+0.45) → StridedSliceGrad `b7e7639`(三层 1/20/40)。
3. **`inline/noinline/static` 与 COMDAT 去重压 L1I**（首例 `b8855a8`/`90ddfe9` PadV3Grad Round-3，属 `.o` 体检第一批）
   首例回执 `37fd777`(+7.92)，起点是看板 #19 的 `.o` 横检。复用：→ ConcatD `e461671`（首破 80KB）
   → StridedSliceGrad `a690e69`（`.text=51.6KB` 最小）→ MultilabelMarginLoss `6e9bf87`（`48.2KB`）。
   [证据: 813867cb 看板 board.jsonl #485/#601/#531]
4. **干掉 `GET_TILING_DATA_WITH_STRUCT` 全量栈拷贝**（首例 `3267c09` StridedSliceGrad v11，+…）
   首例：StridedSliceGrad Track-B v11，17 桶中位 110.18→88.36us、冷启 MAX −23.08us，
   由看板 #21 明确点名"核心 3 招全算子通用，建议各位 Carbon 架构师直接照搬"。
   复用：→ PadV3Grad `b0b210f`（"direct `__gm__` tiling"）→ PReluGradUpdate `6245362`
   （`GET_TILING_DATA_PTR_WITH_STRUCT (56B)`）→ ConcatD `3267c09` 同源。
   [证据: 95eafe6a 看板 board.jsonl #21]
5. **桶校准选型（`bench_feedback.json` 的 +corr / −corr 桶）**
   首例：看板 #23 宣布"全 10 题均已生成基于 **131 对真实线上提交反演**的 `bench_feedback.json`"，
   明确标注哪些桶与线上强正相关、哪些是误导桶。
   复用：11-22 之后几乎所有 11-23 的提交 note 都写成 `Simplex-MAP (N buckets …)` / `Pairwise-v2 (… UCB=…)`，
   覆盖 PadV3Grad/StridedSliceGrad/ConcatD/MultilabelMarginLoss/ConfusionTransposeD/PadV3Grad 六题。
   [证据: 95eafe6a 看板 board.jsonl #23；文件 `ops-supervisor/bench-specs/<Title>/bench_feedback.json`]
6. **判题 TLE 红线（编译+评测 ≤420s）**
   首例：LayerNormV3 `cec9fed` 线上 TLE（11-22 14:51），看板 #17 定调"判题机总编译+评测上限约 7 分钟"。
   复用：变成所有题的提交前检查项（`.text` 阈值、`tmpl<=72`、`.o_max` 上限），11-23 的
   `wantZip 14-bit 16384->0 TLE`、`b4fc9d3` TLE 都被这条规则拦过。
   [证据: 95eafe6a 看板 board.jsonl #17；回执 6b02ffd585ad43bbe0385c22(TLE)]
7. **半核 20 核 / warm-only 低派发**（首例 `d8eb6da` ConfusionTransposeD，+6.29）
   首例：`0418d94b#22`，回执 `6b02e7e4`（"test half-chip 20-block dispatch"）。
   复用：→ ConcatD `4832524` 的"half-chip cap"（+4.60）→ QuantUpdateScatter `c391115` 的 20 核阶梯
   → PadV3Grad `a8e1d70`。
8. **逐例 msprof 分档调参**（首例 `7d6aa46` QuantUpdateScatter 的 14-stratum msprof，+15.26）
   首例：`240583c7#8`，同批提交带 `batch 14-stratum msprof into one session`。
   复用：→ StridedSliceGrad `f2cee3b`（13-level msprof min sum 221.5→134.38us）
   → PadV3Grad `90ddfe9`/Round-4（48 例扫表）→ ConcatD `5a8d07f`（32-case 汇总）。
9. **修数值路径换分数（先保对再保快）**（首例 `c0a7b8b`/`de25877` SyncBatchNorm V11，+10.58）
   首例：`6344d8e3#168`，回执 `6b02e5c5`。
   复用：→ LayerNormV3 `b57ee20`（`RoundHalf` 有符号 int32 转 + 构建失败即停）
   → QuantUpdateScatter `0aa17ab`（精确 RNE cast）→ MultilabelMarginLoss `-FLT_MAX` 掩码。
10. **回滚最高分 SHA 找回板分**（首例 ConcatD `6af295e7`，11-10 03:59）
    首例：`6af295e76bd0`，note 写 `rollback: a458798 scored 49.94 < 52.36`。
    复用：几乎每题都用（`bf2beb9a`、`7919dcb`、`474dc59b`、`8e7ba6b`、`11bd996`、`37fd777`…），
    是"latest 计分制"下的必需品而非优化。

---

## 4. 短结论：这个联赛里"刷分为什么能涨"

1. 一次结构级重写吃下 15～18 分，之后全是 1～3 分碎步；四发最大涨幅（+18.05/+16.53/+15.67/+15.26）全是换算法骨架。[证据: sha 28b9ce3 回执 6b030192…]
2. "零 TPipe + 按形状拆一堆极瘦 schMode" 是 11-22/11-23 的通用胜负手，占 25.8%，从 PadV3Grad 起六题复用。[证据: sha 1efaeaa→28b9ce3]
3. 分桶校准比再写一版 kernel 便宜：131 对线上提交反演出 +corr/−corr 桶后，纯选型/重投类合计 36.28 分（10.9%）。[证据: 95eafe6a 看板 #23]
4. ELF 体积与冷启动是隐形第二战场：L1I 挤爆能让"本地快 1.5x"线上倒跌 5.7 分，`.text` 80→63KB 直接 +2.58。[证据: 95eafe6a 看板 #18 / sha dcc7754]
5. 核数不是越多越好：40 的约数阶梯 + 半片 20 核更快，因为 10 核会触发 DVFS 尖峰（`f32_256x256` 8.74→2.66us）。[证据: 95eafe6a 看板 #658]
6. 先修对再谈快：+10.58 与 +16.53 两发的主干都是数值/同步正确性，不是速度优化。[证据: sha de25877 回执 6b02e5c5…]
7. 判题本身 ±2～5 分噪声（极端 ±16），同二进制重判贡献了 331 里的 26.08，"每发 +2 分"多数不可信。[证据: 回执 6b02dd43 / 6b02dec1]
8. `latest` 计分制逼出"回滚/重投"这一整类动作：不产生新分，但能防止板分被实验覆盖。[证据: sha 474dc59b 回执 6b032403…]

---

### 附：本文没查清的地方

- **精确的 submit 步号**：`opsb submit` 的输出并不总落在 transcript 里，§2.1/§2.2 的"产物对话"有 3 处
  只能给"该 SHA 首次出现步"（`6b02f6fd` LayerNorm `9773dff`、`6b040cef` Multilabel `13c554d`、
  `6b0432d4` StridedSliceGrad `a031e4e` 未定位到 commit 步）。卡住的原因：这些 SHA 的 commit 动作在
  worktree 里由 `git -C <worktree>` 执行，命令文本被 tool 摘要截断，无法与 transcript 的 SHA 出现位置一一对应。
- **11-10～11-17 空窗期的本地尝试量**：只能看到 11-18 之后的提交，11-11～11-17 的仓库提交没有对应回执，
  所以"某方向先失败过几次"在 §2.1 里只能按 commit 数估，不能按回执数算。
- **`tbest` 到底什么时候刷新**：`score` 的下降（同二进制重投）与 `tbest` 变快方向一致，但没有逐场 tbest 时间戳，
  无法把"节点变慢"与"tbest 变快"两种漂移分离，§0.2 的拆分是【推断】。
