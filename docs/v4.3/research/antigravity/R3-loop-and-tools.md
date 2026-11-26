# R3 迭代闭环与工具

> 只读侦察。数据源全部为赛后落盘物，未 ssh、未联网、未跑测试、未访问判题站。
> 全文用【证据】标可直接复核的落盘事实，【推断】标需要模型判断的结论。

## 0. 口径与数据源

| 数据源 | 规模 | 用来量什么 | 口径限制 |
|---|---:|---|---|
| `/tmp/p19/receipts.json` | 233 条已判回执 | 提交次数、status、score、submit→finish 耗时 | 已按 `submission_id` 去重；`title/sha12/status/score/beat/submitted/finished/note` |
| `results/<题>/<40位sha>/` | **1 034** 个目录，10 题 | 本地验证次数（每个目录 = 一次成功落盘的 SHA 验证） | 目录名是 SHA，**同一 SHA 重跑会覆盖**，故 1 034 是**下界**，不是运行次数 |
| 上述目录内 `result.json` | 904 有 `finished_at`，616 有 `wall_sec` | 阶段耗时、退出码、`arena_cases_inproc` | 早期（11-10）记录缺 `wall_sec`/`build_only` 字段 |
| `results/feedback-log.jsonl` | 42 行 | 门禁判定 | **全部 2026-11-25**，即赛后重建，不是赛中实时日志 |
| `results/scoring-evidence/my-score-table.json` | 31 条（30 Pass） | `sum_vis`/`public_pts`/`hidden_pts` 拆账 | 只覆盖 11-10 早期窗口，9/10 题 |
| `results/score-snapshot.json` × `scoring-evidence/live-board-tbest.json` | 10 题 | 终态公开/隐藏拆账 | tbest 会漂移，跨代相减有误差 |
| Antigravity transcript | 93 段 workspace=ops-bench，53 095 行，**10 768** 次 `run_command` | 工具频次、命令原文 | 另有 10 段 workspace=parhelion 已排除 |
| `brain/<id>/.system_generated/swarm/board.jsonl` | 3 份 | 派活说明、公开/隐藏口径、额度 | 只有 2 段主控有 board |

**已验证的自洽性锚点**：按回执时间序逐题算「首发分 → 最终最高分」的累计增量 = **444.95**，与 `/tmp/p19/FACTS.md` 的台阶口径逐位一致。以下所有「新高/退步」分类都建立在这个排序上。

---

## 1. 单题迭代闭环

### 1.1 一次循环的耗时骨架

【证据: `results/*/*/result.json`，n=616 有 `wall_sec` 的记录】

| 阶段 | 字段 | 中位 | 均值 | p90 |
|---|---|---:|---:|---:|
| Phase-A 本地 CPU 编译 | `phase_a_cpu_sec`（n=547） | **53.1 s** | 80.2 | 205.9 |
| Phase-B 独占 Die 0 | `phase_b_die_sec` | **11.6 s** | 16.8 | 23.8 |
| └ 其中真 NPU 计算 | `npu_exec_sec` | 4.4 s | 4.4 | 8.0 |
| └ 其中 ACL Host 开销 | `acl_host_sec` | 8.3 s | 12.4 | 16.9 |
| Phase-C 免锁 verify | `phase_c_cpu_sec` | 3.8 s | 3.2 | 3.9 |
| 单次本地验证 wall | `wall_sec` | **79.7 s** | 132.7 | 279.7 |
| 覆盖用例数 | `arena_cases_inproc` | 132 | 219.9 | 544 |

两个结论：
1. **编译是迭代成本的主项**（53 s / 80 s = 66%），不是 NPU。全部 1 034 次本地验证累计只占 **2.6 h** 的 Die 锁时间、**0.67 h** 的真 NPU 时间——设备不是瓶颈，**Phase-A 编译和 agent 自己的读写才是**。
2. 优化前后差异极大：11-10 的 `run-npu.sh` 里 LayerNormV3 在锁内起 16 次独立 `msprof` 会话（8 case × custom+torch），单次持锁 241.5 s；到 11-22 这套已被压到中位 11.6 s。【证据: `95eafe6a#147`（PadV3Grad `cce79e1`：Phase-B 145.69 s，`npu_exec` 4.78 s，Host 140.91 s = 96.7%）】[证据: `95eafe6a#161`]

### 1.2 本地测 N 次 → 提交 1 次

**统计口径**：N = 落在 `(上一次官方提交时刻, 本次提交时刻]` 区间内、且 `result.json` 有 `finished_at` 的**不同本地 SHA 目录数**（含最终被提交的那一发）。每题目单独按 `submitted` 排序切窗。

【证据: `results/<题>/<sha>/` 目录 + 回执 `submitted`，n=233】

| N | 提交数 | 占比 |
|---:|---:|---:|
| 0 | 28 | 12.0% |
| 1 | 79 | 33.9% |
| 2 | 37 | 15.9% |
| 3 | 19 | 8.2% |
| 4 | 15 | 6.4% |
| 5 | 7 | 3.0% |
| 6 | 8 | 3.4% |
| 7–9 | 12 | 5.2% |
| 10–15 | 16 | 6.9% |
| ≥16 | 12 | 5.1% |

mean **3.83**，median **2**，p25 1，p75 4，p90 **11**，p95 15，max **32**。另两把尺子：distinct 本地 SHA 1 034 / 233 = **4.44**；transcript 里 `opsb test` 出现 **842** 次 / 233 = 3.61。三把尺子落在 3.6–4.4，互证。

**典型 = 「测 1 发就发」（33.9%），不是「测很多次再发」**；但尾部很重：p90 要测 11 次才发一发。

| 题 | 提交数 | N 中位 | N 均值 | N max | 线上失败 |
|---|---:|---:|---:|---:|---:|
| CTCLossV2 | 16 | 3.0 | 7.62 | **32** | 1 |
| SyncBatchNormGatherStats | 10 | 3.0 | 6.50 | 16 | 2 |
| PadV3Grad | 33 | 3.0 | 4.79 | 21 | 0 |
| ConcatD | 30 | 1.5 | 4.03 | 19 | 3 |
| PReluGradUpdate | 14 | 1.5 | 3.79 | 15 | 1 |
| LayerNormV3 | 18 | 1.5 | 3.44 | 29 | 2 |
| MultilabelMarginLoss | 25 | 2.0 | 3.16 | 17 | 3 |
| QuantUpdateScatter | 17 | 2.0 | 3.06 | 24 | 1 |
| ConfusionTransposeD | 25 | 1.0 | 2.92 | 16 | 1 |
| StridedSliceGrad | 45 | 1.0 | 2.38 | 16 | 1 |

### 1.3 四个可复现的极端样本

【证据: 各 SHA 目录 `finished_at` 与回执时间】

| 类型 | 题 · 回执 | 本地序列（时刻 → SHA10 → exit） | 读法 |
|---|---|---|---|
| 极端高 | CTCLossV2 `6af3da24` score=37.18 | 窗口内 32 个 SHA 全落盘后才发 | 首发题在无参考模型时纯靠堆本地证据 |
| 极端高 | LayerNormV3 `6af2bffe` score=**0.0**（WA） | 29 次本地全绿 | 本地次数与线上成功率无关（见 §2.2） |
| 极端低 | ConfusionTransposeD `6b0342c6` score=**0.0**（TLE） | **N=0**，note=`restore best validated SHA (76.37)` | 没有新本地证据，纯回滚，仍吃一发额度 |
| 中位典型 | PadV3Grad `6af2e3ac` score=30.38 | 11 次 exit=1/2 红 + 1 次 exit=0 才发 | 本地迭代里**红发占多数**：全局 exit_code 分布 0:619 / 1:202 / 2:66 / 255:11 / 127:3 / 其他:4 / 缺:130 |

**N=0 的 28 发全部是回滚/重发**，不是「没测就发」。全部 233 发**都能在本地找到对应 SHA 目录**（命中率 100%）——不存在「本地没跑就提交」的情况。【证据: 233/233 匹配】

顺带把 restore 抽出来：note 含 `restore|rollback|retry` 的 **34 发**，平均本地 N=**1.8**，其余 199 发平均 N=**4.2**。回滚是「少测试、多花钱」的一类提交。

### 1.4 决定提交的依据：门禁三层，但判别力接近零

**（a）机器可读的门禁判据确实存在。** 赛中 `opsb status` 每次都会打印：

```
Gate & Quota: Cooldown 0s/190s | 24h Used 34/100 (Reserve 2, Avail 64) | Noise Floor Δ>=2.5
```
【证据: `813867cb#91`、`813867cb#47`】

四关判据在旧 `opsb_core/commands/queue_cmd.py` 里是三条布尔：
```
noise_ok = is_restore_zero or prob["current_score"] == 0.0 or delta_exp >= noise_floor or explore_ok
circuit_ok = is_restore_zero or not prob["circuit_open"]
```
【证据: `95eafe6a#1677` 的 grep 输出，`queue_cmd.py:65-66`】即 **S1 安全 / S2 噪底 / S3 熔断 / S4 额度**，其中 S2 是 `Δ>=2.5` 分。

**（b）回执 note 里留下了 46 发带门禁标签**：`[S2:NOISE_FLOOR]` 27、`[S4:QUOTA_RESERVE]` 13、`[S3:CIRCUIT_BREAKER]` 5、`[S1:SAFETY]` 1；另有 51 发写 `All 4 stages passed`。

【证据: 回执 note + 逐题时间序判「是否刷新峰值」】

| note 标签 | 发数 | 刷新峰值 | 与峰值持平 | 低于峰值 |
|---|---:|---:|---:|---:|
| `[S2:NOISE_FLOOR]` | 27 | **9** | 4 | 14 |
| `[S4:QUOTA_RESERVE]` | 13 | 8 | 0 | 5 |
| `[S3:CIRCUIT_BREAKER]` | 5 | **0** | 0 | 5 |
| `[S1:SAFETY]` | 1 | 1 | 0 | 0 |
| `All 4 stages passed` | 51 | — | — | 其中 **4 发线上失败（7.8%）** |
| （无标签） | 187 | 65 | 19 | 103 |

三条读法：
- **S3 熔断是全数据集里唯一有判别力的信号**：5/5 全部低于峰值，0 刷新。它是事后正确的。
- **S2 噪底把 9 发真提升打成了「不该发」**。原因是阈值 `2.5` 与实测噪声几乎相等，见下。
- **`All 4 stages passed` 的线上失败率 7.8% > 全体基线 6.4%**，即「过四关」在统计上**不比不发筛选更差也不更好**，判别力为零。

**（c）噪底 2.5 分是 1.07σ，卡在噪声中央。** 用同一 SHA 重投的分数残差直接估 σ：

| 指标 | 值 | 口径 |
|---|---:|---|
| same-SHA 重复组 | 25 组（35 次多余提交） | `(title, sha40)` 去重 |
| 组内残差 SD（pooled, df=49） | **2.34 分** | 扣组均值后的平方和 |
| \|残差\| 中位 / p90 / max | 1.12 / 3.03 / **7.98** | 同上 |

【证据: `ConcatD e799eb6` = [52.36, 48.41, 0.00 WA, 50.08]；`SyncBN 544439e` = [57.45, 54.96, 56.20, 52.47]；`ConfusionTransposeD 8e7ba6b` = [78.18, 74.39, 79.08]】

`2.5 / 2.34 = 1.07σ`。要真正区分一个改动，门槛应 ≈ **2σ = 4.7 分**。PERF-EXP 自述的 `σ≈2.7` 与实测 2.34 同量级，两处都偏低。【证据: `PERF-EXP.md` §1】

**（d）`feedback-log.jsonl` 不是赛中的门禁日志。** 42 行**全部**在 `2026-11-25T07:55Z` 之后，`status` 全是 `HOLD_LOW_GAIN`、`approved` 全 `false`，`stages` 里 `SAFETY_OK/CIRCUIT_CLOSED/QUOTA_OK` 各 42、`BELOW_NOISE_FLOOR` 28、`BELOW_BEST_IN_RESTORE` 14。也就是说我们能审计的是一份**赛后重建的、只保留了「拒」的样本**——没有一条「放」的对照。赛中门禁的实时形态只存在于回执 note 的标签与 `opsb status` 的打印里。【证据: `results/feedback-log.jsonl`】

**（e）worker 的主观成分确实存在。** 除了门禁，主控还会直接做「regime 翻译」判断，原话：

> `in [RESTORE] regime (best=59.56 vs board=57.32): est=58.41 gives delta_vs_best=-1.15`
【证据: `results/feedback-log.jsonl` 第 2 行 reason】

以及明确的「双发重测求均值」这种非门禁决策：

> `retry: same source measured 52.36 / 48.41, hidden noise is +-4`
【证据: 回执 `ConcatD / 6af296a66bd0f9be37dc11c6`】

结论：**依据 = 门禁（S1–S4，主判据是 S2 噪底）+ 逐例桶对比（Simplex/Pairwise 的 `d_hid`）+ 主观 regime 判断；三者权重里，逐例桶对比是实际驱动提交的那个，门禁主要在「按住」而不是「放行」。**

### 1.5 本地估计 vs 官方分

两把独立的预测器，偏差方向相反，必须分开看。

**（i）`d_hid` —— 改动级预测器（本地隐藏桶几何均值 Δ），59 条可比对样本**

note 形如 `Simplex-MAP (15/16 buckets vs f2cee3b, beta=0.843, d_hid=+21.29, d_pub=+1.32, ...)`，`vs <sha7>` 给出比较基准，把基准的官方分与实际分相减即得 act。

| 指标 | `d_hid`（n=59） | `d_pub`（n=59） |
|---|---:|---:|
| Σ预测 | **263.92** | 14.80 |
| Σ实际 | **71.35** | 71.35 |
| 预测/实际 | **3.70×** | 0.21× |
| 中位预测 / 中位实际 | 3.05 / 1.24 | 0.14 / 1.24 |
| 残差中位（pred−act） | **+1.25** | −0.44 |
| 残差 p10 / p90 | −6.32 / +13.73 | −8.25 / +4.85 |
| 最大高估 / 最大低估 | **+57.17 / −15.62** | +54.61 / −21.80 |
| 高估(>+0.5) / 低估(<−0.5) / 中性 | 32 / 24 / 3 | 17 / 29 / 13 |

【证据: 59 条 Simplex-MAP/Pairwise-v2 note + 基准回执分】

- `d_hid` **系统性高估 3.7 倍**，且是**双向放大**：预测为正时共 259.62 vs 实际 46.41；预测为负的 8 条 Σpred −8.01 vs Σact **+15.66**（把该涨的判成跌）。这是「外扩型」估计，不是简单的常数偏移。
- **哪类改动最容易高估：覆盖面广的「全桶变好」声明。** 按 note 里的 `improved=` 分组：

| note `improved` | n | Σpred | Σact | 残差中位 |
|---|---:|---:|---:|---:|
| ≤5 | 28 | 33.70 | −53.56 | −0.06 |
| 6–9 | 19 | 110.15 | 91.51 | +1.36 |
| **≥10** | 12 | **120.07** | **33.40** | **+7.73** |

即「13/16、18/18 桶全绿」这类声明的高估最严重（+7.73 中位）。反之 `regressed>=3` 的 note（预测自己有回退）残差中位只有 +1.36——**自陈回退的估计反而更准**。`beta`（校准斜率）中位 0.701、范围 0.355–3.000，说明这层校准是**每题手调的超参**，不是学出来的；`beta > 0.8` 的 11 条残差中位 +5.42，是本数据集里最差的一档。三个最坏样本：`ConcatD` pred +2.74 / act **−54.43**（后续那发 WA 归零）、`StridedSliceGrad` pred +21.29 / act +1.24、`MultilabelMarginLoss` pred +15.69 / act −2.98。

**（ii）`est=` —— 重投级预测器（同 SHA 再测的绝对分预测），8 条**

残差（est−act）= +5.05, +5.60, +1.51, −1.94, −2.03, −2.22, −2.47, −46.32（最后一条口径错，note 的 `est` 是「分数增量 2.72」不是绝对分，剔除）。**剔除后 7 条残差中位 −1.94 分，全部落在 ±5.6 内。**

【推断】两个预测器分工清楚：`est=`（对已判 SHA 再测的绝对分预测）**基本无偏、误差 ±2~5 分**，可以直接当门禁输入；`d_hid`（对未判改动的 Δ 预测）**不可直接当分数用**，必须过一层饱和变换。这个不对称说明「同代重测」是可学的，「跨代改动」不是。

### 1.6 公开 5 例 vs 隐藏 45 例的剪刀差：17–23 分只在低分区间站得住

先要确定计分公式。`my-score-table.json` 同时给了 `sum_vis`（公开 5 例的 Σ tbest/τ）、`public_pts`、`hidden_pts`、`score`，30 条 Pass 记录里恒有 `public_pts = 2 × sum_vis`、`score = public_pts + hidden_pts`。因此：

- `pub_avg% = 100 × sum_vis / 5`
- `hid_avg% = 100 × (score/2 − sum_vis) / 45`
- 剪刀差 = `pub_avg% − hid_avg%`【推断，但被 30 条记录的恒等式验证】

**口径 A（早期窗口，11-10，n=30，权威拆账）**

| | 中位 | 均值 | p10 | p25 | p75 | p90 | min | max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 剪刀差 (pt) | **20.5** | 17.9 | 7.5 | 13.5 | 22.3 | **23.2** | 3.9 | 27.6 |

按题的中位剪刀差：ConcatD 22.7（n=8）、PReluGradUpdate 21.2、StridedSliceGrad 20.9、MultilabelMarginLoss 20.2、SyncBN 18.9、QuantUpdateScatter 17.9、PadV3Grad 13.3、ConfusionTransposeD 9.9、LayerNormV3 4.2。

**口径 B（终态，n=10，用 `score-snapshot.public_times_us / public_tbest_us` 算公开比，再反推隐藏比）**

| | 中位 | 均值 | min | max |
|---|---:|---:|---:|---:|
| 剪刀差 (pt) | **12.4** | 12.8 | 5.5 | 21.4 |

逐题：CTCLossV2 21.4、PadV3Grad 20.2、MultilabelMarginLoss 16.9、StridedSliceGrad 15.9、SyncBN 13.6、ConfusionTransposeD 11.2、PReluGradUpdate 9.0、ConcatD 7.3、LayerNormV3 7.2、QuantUpdateScatter 5.5。

**口径 C（swarm 派活说明里的人工口径，可直接引原文数字）**

| 题 | 公开 | 隐藏 | 差 | 证据 |
|---|---:|---:|---:|---|
| PadV3Grad | 60.2%（`ours≈5.95us vs best≈3.58us`） | ~38% | **22.2** | `95eafe6a` board seq 1 |
| StridedSliceGrad | 82.3% | 66.0% | 16.3 | board seq 2 |
| CTCLossV2 | 77.5% | 64.3% | 13.2 | board seq 5 |
| ConcatD | 80.5% | ≈69% | 11.5 | board seq 3 |

**结论（三条口径一致）**：
1. **17–23 分这个量级是「低分区间 + 早期窗口」的数**，口径 A 的中位 20.5、p75–p90 正好 22.3–23.2，该说法在其原始窗口内成立。
2. **但它随分数上升而收敛**：同一批题打到 70–86 分后，口径 B 的中位只剩 **12.4**，最小 5.5。用 17–23 去外推终态会高估约 1.6 倍。
3. **口径 A 与 B 差 8 分的物理原因**是公开 5 例也被追平了（公开比从 70–77% 涨到 82–90%），而隐藏比涨得慢（44–50% → 65–84%）。所以「剪刀差」不是常数，是**随着你逼近 tbest 而单调收缩的**——这正好解释了为什么后期提分越来越贵。
4. **不要用 `d_pub` 当剪刀差的度量**：`d_pub` 的中位只有 0.14 分，它是「改变量」不是「水平差」，与 §1.5 的剪刀差是两个量纲。

---

## 2. 失败提交的类别与代价

### 2.1 六类分账

**口径**：先按 `status` 切出失败；Pass 内部按「是否刷新该题历史最高分」切成新高 / 与峰值持平（≤0.5 分）/ 退步。耗时 = `finished_at − submitted_at`。

【证据: 233 条回执 + 逐题时间序】

| 类别 | n | 占比 | 平均耗时 | 中位 | max | 说明 |
|---|---:|---:|---:|---:|---:|---|
| 新高 | **83** | 35.6% | 3.5 min | 2.6 | 8.9 | 唯一真正推进分数的类别 |
| 与峰值持平 | 54 | 23.2% | 3.9 min | 2.9 | 8.4 | 花了额度没买到分 |
| 退步（仍 Pass） | 81 | 34.8% | 4.2 min | 2.7 | **20.0** | 峰值不掉，但白花 |
| TLE | 7 | 3.0% | **6.9 min** | **7.0** | 12.6 | 归零 + 需要一发 restore |
| WA | 6 | 2.6% | 3.7 min | 2.7 | 8.6 | 归零 + 触发 ESCAPE_LOCK |
| RE | 2 | 0.9% | 2.0 min | 2.0 | 2.6 | 归零 |
| **合计** | **233** | 100% | 3.98 min | 2.7 | 20.0 | 判题机累计 **15.4 h** |

**只有 35.6% 的官方提交买到了分。** 剩下 64.4% 里，15 发（6.4%）是硬失败，135 发是 Pass 但没有超过自己的峰值。

### 2.2 「本地全绿」是常量：233/233

把每发回执对应的本地 `result.json` 的 `exit_code` 与线上 `status` 交叉：

| 本地 exit_code | 线上 Pass | 线上 TLE | 线上 WA | 线上 RE |
|---|---:|---:|---:|---:|
| **0（全绿）** | 218 | **7** | **6** | **2** |

【证据: 233 个 `results/<题>/<sha>/result.json` × 回执 status】

**本地 `exit_code` 在这份数据里方差为零，因此对线上结果的信息量严格为 0。** 任何把 `exit_code == 0` 写进门禁的实现（`gate.py` 的 SAFETY_OK 环节）在数学上等价于「什么都不筛」。这不是 agent 偷懒，是**本地验证套件的能力边界**：它跑的是本地 `samples/` + `validation/extended.py`，与官方 50 例（5 公 + 45 隐）没有共享用例。

### 2.3 八个「本地看不出、线上炸」的具体模式

每例都写清「本地为什么看不出来 / 线上为什么炸」。这些已经是现成的 KB 卡片骨架。

| # | 题 · SHA | 线上 | 本地 | 本地为什么看不出 | 线上为什么炸 |
|---|---|---|---|---|---|
| 1 | LayerNormV3 `cec9fed` | TLE | 编译通过、性能 210→229 s 还涨了 | 本地只测**编译成功**，不测编译**多久**；`wall_sec` 只记总时长 | 27 dtype × 3 schMode = **81 份实例化**，判题机「编译+50 例」总时限 **420 s** 被撞穿 |
| 2 | LayerNormV3 `07ae054` | WA | note 写 `75/75, DETERMINISTIC` | 75 例是本地生成分布，**没有覆盖官方 Test_050 那个数值区间** | `fp32 accumulate + one rounding` 在极端量级上越过 `float:1e-4` 阈值 |
| 3 | StridedSliceGrad `dfa784f` | WA | 常规测试全过 | 本地用例的尾部长度恰好落在安全填充区内 | `BuildBatchGatherTable` 尾部对齐到 64 元素的 `kInfOff` 区**未清零**，`Gather` 读到脏偏移 |
| 4 | PadV3Grad `be05c5b` | Pass 但 43.62→**42.45** | 16 桶提速 1.4–2.2× | 本地 warmup 后取中位，**冷态被暖态抹掉** | `.o` **172 KB**（全 10 题最肥）撞 910B3 **32 KB I-cache**，3-run 冷启每发白交 +1.8 us；公开 5 例死锁在 5.3–5.6 us |
| 5 | SyncBN `1795a85` | WA | `18 paired cases … raw_geo=1.412x, improved=17, regressed=1` | 17/18 桶赢，唯一输的那桶不是精度桶；本地没有 `Test_050` 级别的边界数值 | `vrsqrt` 近似 + 大数值范围溢出 + 非对齐 `dcci`，在 `Test_050` 精度边界逃逸 |
| 6 | QuantUpdateScatter `becddb8` | TLE | `122/122 green`，kv_decode 1.28×、rows_big 2.26× | 本地只比**核内时间**，不比**编译规模** | E15 的 3-op cast + dispatch-floor + FusedGroupPass 组合把 Cube 编译推到 12 min 红线 |
| 7 | ConfusionTransposeD `b32a0a0` | TLE | note 是 `restore best validated SHA (76.37)` | 回滚一发**不需要**本地验证（N=0），所以没有任何本地信号 | 同一份已验证源码在线上 300 s 内没跑完——判题机负载/编译波动本身就能把 Pass 变 TLE |
| 8 | MultilabelMarginLoss `f77146d` | TLE | 17 次本地全绿 | 本地 `arena_cases_inproc` 只 132 例量级 | `mean/sum atomic add into y[0]` 在大 batch 上原子串行化，50 例累计超 420 s |

【证据: `docs/debugging-and-escape-postmortems.md` 事故 1/2/3/4/5/6；回执 `6b02dbc4`/`6b02de02`/`6b02da42`/`6b0342c6`/`6b04137c`；`results/ConcatD/…/validation.log`】

抽象成 **4 类可复用逃逸机制**：① **冷启动税**（warmup 后取中位掩盖冷态，代表 `.o` 172 KB → +1.8 us，防线 = 每次本地运行都记 `.o` 字节 + `cold3` 三发冷启）；② **编译规模税**（只判「编译成功」，代表 81 实例化 → 420 s TLE，防线 = `template_instantiations` 与 Phase-A 秒数做硬闸门）；③ **隐藏 shape 未覆盖**（本地用例自己编，代表 `kInfOff` 尾部 / `Test_050`，防线 = append-only 桶表 + `*_proxy` 盲区显式计数）；④ **精度边界**（随机分布打不到，代表 `fp32 accumulate` 单次舍入，防线 = 极值/次正规/Inf 定点用例集）。

### 2.4 TLE 的代价与额度账

- **判题时限就是 420 s**：7 发 TLE 的耗时 mean **413.3 s**、median **420.0 s**，最大 758.7 s（判题机超时后仍继续跑到收尾）。直接成本 **7 × 420 s ≈ 48 min** 判题机墙钟；间接成本更高——`latest` 计分制下一次 TLE 立刻把该题归零，必须再花一发 restore，而第 2 次 restore 又可能 TLE（`ConfusionTransposeD b32a0a0` 就是这样，`8e7ba6b` 的 79.08→78.18 也是这么掉下来的）。所以**一次 TLE 的真实成本 ≈ 2 发额度 + 14 min**。
- **额度其实不是瓶颈。** 赛中 `24h Used` 观察到的最高值是 **34/100**（`813867cb#91`），冷却 **190 s**（= 19 发/h 上限），而 233 发分摊在 11-10→11-23 约 336 h 里只有 **0.69 发/h**，远低于上限。
- **但 11-10 那天确实撞过额度墙**：进度快照明确写「旧账号用尽 **50 发/日** 配额（平台 429 `daily_limit`）」，`CTCLossV2 02c2048` 因此「validated but never submitted successfully」。【证据: `cacd27d9#8`】——这是「额度焦虑」在整个项目里被反复强调的起点，而在 11-18 之后它已经不再成立。

---

## 3. 用了哪些平台能力

### 3.1 工具面频次

**Antigravity 原生工具（93 段全量，53 095 行）**

| 工具 | 次数 | 备注 |
|---|---:|---|
| `run_command` | **10 768** | 一切 CLI/ssh/脚本的唯一入口 |
| `read_file` | 7 033 | 只能读**本机**文件 |
| `edit_file` | 5 555 | |
| `read_board` / `swarm_wait` / `swarm` | 2 301 / 898 / 152 | swarm 看板轮询与派活 |
| `update_focus` / `update_steps` | 940 / 179 | |
| `write_file` | 712 | |
| `post_message` / `send_message` | 678 / 206 | |
| 其余（`search_web` 60、`invoke_subagent` 42、`manage_*` 255…） | ≈360 | |

**MCP 工具调用次数 = 0。** 93 段里 `parhelion_status`/`parhelion_test`/`parhelion_submit_gate` 只作为**文本**出现（414 次提及，集中在做 Parhelion 自身的会话），**没有任何一次作为 tool_call 被调用**。Parhelion v3 的 MCP 面当时在线（`opsb mcp serve` 在 `813867cb#47` 里被确认），但刷分链路完全没走它。

**`opsb` 子命令（全量 10 768 条命令里的出现次数；同一命令里可出现多次）**

| 子命令 | 次数 | 子动作分布 |
|---|---:|---|
| `bench` | 858 | `status` 249 / **`elf` 183** / **`cold3` 132** / **`diagnose` 129** / `pmu` 66 / `verify-escape` 25 / `calibrate` 9 / `evolve` 6 |
| `test` | **842** | 主流形式 `opsb test <题> --sha "$SHA"`；少量 `--build-only` |
| `status` | 541 | 赛况 + 额度 + 冷却 |
| `history` | 352 | 判题历史 |
| `kb` | 296 | `record` 115 / `query` 102 / `feedback` 34 / `list` 15 |
| `queue` | 293 | |
| `npu` | 224 | 主要是 `npu agent-install`（11）+ `npu status` |
| `breakthrough` | 179 | `list` 98 / `show` 67 |
| `submit` | 127 | |
| `daemon` / `mcp` / `driver` / `auth` | 58 / 19 / 8 / 3 | 平台自身运维 |

**ssh 直连设备主机：2 616 行（占 `run_command` 的 24.3%）**

| ssh 用途（按远程首命令归类） | 次数 |
|---|---:|
| `grep` | 729 |
| `sed` / `cat` / `ls` / `find` | 238 / 198 / 162 / 134 |
| `python3` / `bash` / `source set_env.sh` | 121 / 106 / 113（+262 处路径出现） |
| `flock`（设备锁） | 50（`flock` 关键字共 147 行 / 204 处） |
| `readelf` | 50 |
| `ps` / `pgrep` / `npu-smi` | 27 / — / 9 |
| **纯读类合计（grep/cat/ls/head/tail/find/sed/readelf/nm/objdump/wc）** | **2 401（91.8%）** |

被读最多的远程路径：

| 路径类别 | 次数 | 说明 |
|---|---:|---|
| `Ascend/ascend-toolkit/.../tikcpp/tikcfw/impl/dav_c220/*` | 145 | **CANN SDK 头文件实现**，本地无镜像 |
| `.../ascendc/include/basic_api/*` + `.../asc/impl/basic_api/dav_c220/*` | 134 + 100 | 同上 |
| `opp/built-in/.../tbe/impl/ops_math/*` | 89 | 内置算子 TBE 参考实现 |
| `.../build/op_kernel/ascendc_kernels/binary/*.o` | 74+56+42+37+35 | **编译产物 `.o`**，只存在于设备主机 |
| `ict-cann-nov-device0.lock` / `set_env.sh` | 88 / 262 | 设备互斥锁 / 每条远程命令的环境引导 |

【证据: 10 768 条 `run_command` 的 `CommandLine` 全量正则聚合】

结论：**设备主机被 ssh 直连得非常频繁，而且几乎（91.8%）是在做「远程文件系统读取」**——读 SDK 头、读 `.o`、读 `build/autogen` 展开后的源码。用来看 profiler 的只有 146 行（`ssh + msprof`）、`ssh + readelf/objdump/llvm` 合计 121/141 行。也就是说 ssh 的主职不是「跑设备」，而是**补一个本地不存在的只读文件 API**。

### 3.2 绕行核实：`tools/cj` 与 `tools/submit-validated.py`

派活说明里明确禁止过（swarm board 原文）：

> 再通过 `~/.local/bin/opsb submit <题> <full_sha> --note "..."` 入队；**严禁直接调用 `tools/cj` 或 `tools/submit-validated.py`**。
【证据: `95eafe6a/.system_generated/swarm/board.jsonl` seq 1，同一句在 seq 2/3/4/5/6/7/9/10/11/12/13 重复】

**核查结果：真实绕行 = 0 次提交，但有 1 次判题站直读。**

| 命令 | 行数 | 是不是绕行 |
|---|---:|---|
| `tools/cj submit ... --dry-run` | **36** | 否，`--dry-run` 只做 payload 预检，不入队 |
| `tools/cj submit`（无 `--dry-run`） | **0** | — |
| `tools/cj status <sid>` | **1**（`240583c7#386`） | **是**：直接向判题站查单发状态，绕过 `opsb` |
| `tools/cj rank --help` / `--help` | 2 / 6 | 否 |
| `submit-validated.py` 被执行 | **0** | 9 行全是 `grep`/`py_compile`/`ps aux` 引用 |

【证据: 42 行含 `tools/cj` 的命令；9 行含 `submit-validated`】

**为什么最终还是没绕行**：`tools/cj submit --dry-run` 的存在本身就是「绕行冲动」的证据——36 次预检说明 agent 反复在确认 payload 对不对，而正式提交路径始终走 `opsb submit`。真正压住绕行的是**队列串行化**：`tools/submit-validated.py` 只能提交「服务端有过成功验证记录的源码」，绕行会被它自己 `raise RuntimeError('Current source does not match any successful server validation')` 打回。【证据: `results/submit-queue-outcome.jsonl` 第 1 行 detail】

---

## 4. agent 自造脚本与工具

### 4.1 `brain/<id>/scratch/` 清单

113 个 brain 目录里 **17 个有 `scratch/`**，共 60 个文件。

| 目录 | 文件 | 用途 |
|---|---|---|
| `8630232c` | `bench_msprof_all.py` `head_to_head.py` `h2h_v3.json` `sim_v3.py` `sim_v4.py` `pad_v3_grad_*.{h,cpp}` | PadV3Grad：msprof 批量基准 + 头对头比较 + 两代 UB 仿真 |
| `78401942` | `test_full_sim.py` `test_incremental.py` `test_pipe_v_algo.py` `test_sort_algo.py` `verify_merge.py` `59f8065_*.{h,cpp}` | MultilabelMarginLoss：4 个算法路线的纯 Python 对拍 |
| `3da08ff0` | `tu0.cpp/tu1.cpp/.o` `linked_ok` `verify_gather_concat.py` | ConcatD：**分离编译单元 + 链接**的 ELF 体积实验 |
| `a6605b9f` | `mock_ascendc.h` `stats.cpp` `test_sim.cpp` `test_sim` `strided_slice_grad_v2.h` | StridedSliceGrad：mock 掉 AscendC 头做**纯 CPU 编译 + 计数** |
| `ba0f12f5` | `backtest_v21.py` `backtest_v2_proto.py` `forensic_corr.py` `forensic_deep.py` `forensic_replay.py` `inspect_details.py` | 估计器回测与相关性取证 |
| `59faaca6` | `linear_block.h` `v27_linear.h` `v27_small.h` `v29_small_single.h` | SyncBN：把 scratch 当版本仓存内核变体 |
| `10f0bfd7` / `efe4d5d0` / `240583c7` | `layer_norm_v3*.{h,cpp}` / `expb_v8.h` `main_v12.h` / `becddb8.h` | 同上，头文件试验田 |
| `3cfa00b3` / `be7b8945` / `6344d8e3` / `68ceb146` | `verify_v2.py` `verify_v3.py` / `verify_algo.py` `test_gauge.py` / `solve_ub_coloring.py` / `pmu_probe.py` | 精度对拍 / UB bank 着色求解器 / PMU 探针 |

### 4.2 内联脚本聚类（照 P12 命名，保持可比）

**口径**：一次 `run_command` 视为一次「脚本调用」，与 `P12-script-clusters.md` §1 的单位一致。含内联 Python 的命令行 **2 403** 条（heredoc 1 091 处 + `-c` 1 385 处），其中 1 322 条能被正则解析出脚本体并归类。**脚本文件调用只有 370 条**——即 87% 的 Python 使用是**一次性、不落盘**的。

| 聚簇（P12 名） | 次数 | 占比 | 代表 | 复用 | 沉淀 |
|---|---:|---:|---|---|---|
| `platform_internal` 直读平台内部 | **552** | 41.8% | `cacd27d9#67` 读 `~/.cannjudge/session.json`；`95eafe6a#994` 读 `~/.config/ops-supervisor/last_jump_submit.log` | 无 | ❌ |
| `calibration` 估计器回测 | 244 | 18.5% | `cacd27d9#392` 汇总 `d_hid` vs 实分 | 部分（`ba0f12f5/scratch/backtest_*.py`） | ⚠️ 部分进 `docs/local-bench-methodology.md` |
| `history` 判题历史/回执 | 238 | 18.0% | `cacd27d9#58` 走 `skills/problem-submit/scripts/ict_cli` | 无 | ❌ |
| `per_case` 逐例时间对比 | 90（ELF 类）+ 11（msprof 类） | 7.6% | 解析 `validation.log` 的 case 时间表 | 无 | ❌ |
| `numeric_repro` 数值复现 | 61 | 4.6% | 检查 `torch/torch_npu/ml_dtypes` 版本与 numpy 对拍 | 部分 | ⚠️ 部分进 `skills/ops-precision-standard/scripts/` |
| `platform_action` 平台动作壳 | 43 | 3.3% | `3d7f1d11#755` 批量 `git show` 两个 ref 的源码差异 | 无 | ❌ |
| `queue` 队列/门禁 | 6 | 0.5% | 解析 `submit-queue-outcome.jsonl` | 无 | ❌ |
| `other` | 76 | 5.8% | 设备环境探测、`acl.get_soc_name()` 等 | 无 | ❌ |

**与 P12 的对照**：P12 里 A 组（平台数据可替代）占 65.6%；这份 Antigravity 语料里 `platform_internal + history + per_case + queue` = 70.9%，比 P12 更高。原因是 Antigravity 侧连 `parhelion_report` 都没有，只能自己 `json.loads`。

### 4.3 沉淀率

- 把 10 个有名字的 scratch 脚本名在 `docs/` + `skills/` 里全量 grep：**9 个 0 命中**，只有 `walk_model` 出现 3 次（因为写进了复盘文档正文，不是作为脚本被引用）。**沉淀率 ≈ 1/20 = 5%。**
- `skills/` 现有 20 个技能、`skills/*/scripts/` 下有 13 个 `.py`，但**没有一个是从 scratch 提升上来的**——它们是另一条线（`_npu_info.py` / `parse_plog.py` / `bitwise_match.py` 等环境与精度类）。
- 唯一成体系落盘的产物是 5 篇 `docs/`：`local-bench-methodology.md`（桶权重 + γ/α 冷启惩罚项建模）、`debugging-and-escape-postmortems.md`（7 大事故）、`ascendc-optimization-patterns.md`（7 大杀手锏）。**这三篇覆盖的正是 §2.3 的逃逸机制，说明复盘链路是通的，只是「脚本 → 可执行资产」这一段断了。**

---

## 5. 平台本该提供却没提供的

按「对迭代速度的影响」降序。每条：缺什么 → 当时用什么土办法顶 → 证据 → 4.3 落点。

| # | 缺的能力 | 当时的土办法 | 证据 | 4.3 落点 |
|---|---|---|---|---|
| 1 | **本地 case ↔ 官方 case 的映射表 + 每发逐例官方耗时**（只能在赛后从 `my-score-table.json` 反推） | 自建 13–48 个「桶」，用手调 `beta`/`gamma`/`trust` 把桶 Δ 映射成分数；`d_hid` 高估 3.7× | 59 条 Simplex/Pairwise note；`my-score-table.json` 只 31 条；§1.5 | `scheduler/judge_cases.py` + `scheduler/case_map.py`，出口 `parhelion_report(view=per_case)` |
| 2 | **设备主机上文件的只读 API**（CANN 头、`.o`、`build/autogen`） | 2 401 行 `ssh grep/cat/sed`，每条再套一次 `source set_env.sh` | §3.1 远程路径表 | `cli/opsb_core/device_env.py` 扩 `opsb npu cat/ls/grep`，并挂成 MCP 资源 |
| 3 | **冷启动税的可测口径**（本地 warmup 中位 vs 线上 3-run 冷启） | 数 `.o` 字节当代理：`readelf/objdump/nm` 788 处、`.text` 体积当第一指标 | §3.1；`opsb bench cold3` 132 次（已是半成品） | `scheduler/case_calibration.py` + `cli/opsb_core/fast_feedback/` 的 cold3 |
| 4 | **提交后不回灌结构化逐例回执**（只有 `note` 一段自然语言） | 552 条内联 Python 解析 `receipt/score/history` 的 JSON | §4.2 | `cli/opsb_core/replay/` + `parhelion_report(view=history/diff)` |
| 5 | **本地验证对线上结果零判别力**（233/233 `exit_code=0`） | 无解；继续靠 `gate.py` 的 `SAFETY_OK` 假装筛过 | §2.2 交叉表 | `scheduler/gate.py` 去掉 `exit_code` 判据，改挂 paired-timing 与 `.o` 体积 |
| 6 | **回滚没有一键化**（34 次 restore 平均只测 1.8 次，且再吃一发额度） | 手写 `restore best validated SHA (xx.xx)` note + `git checkout <sha>` | §1.3；`ConfusionTransposeD 6b0342c6` 的 restore 又 TLE | `scheduler/candidate_archive.py` + `cli/opsb_core/replay/restore` |
| 7 | **在线噪声 σ 没有估计**（门禁写死 `Δ>=2.5` = 1.07σ） | 靠 same-SHA 重投手工测噪（35 次多余提交） | §1.4；σ=2.34（df=49） | `scheduler/calibration.py` 在线 σ + `scheduler/predict_interval.py` 的区间门禁 |
| 8 | **估计器把「本地比例改进」线性映射成分数** | 每题手调 `beta`（0.355–3.000），加 `gamma`/`trust`/`UCB` 补丁 | §1.5；`improved>=10` 组 +7.73 中位 | `scheduler/estimator.py` 的饱和变换 + `scheduler/plausibility.py` 拒收不物理的估计 |
| 9 | **编译规模没有前置闸门**（实例化数/Phase-A 秒数不是判据） | 事后从 `result.json` 的 `template_instantiations` 倒查（只 203 次运行有该字段） | §2.3 例 1；`LayerNormV3 cec9fed` 81 实例化 | `scheduler/judge_compile.py` + `gate.py` 的 Phase-A 时限闸 |
| 10 | **队列/门禁状态在 GUI 与 CLI 之间不一致** | 手工对照 `opsb queue list --json` 与 GUI badge，写成 issue 记录 | `813867cb#132`（`REJECTED GATE` 被 GUI 显示成绿色 `待提交 · READY`）、`813867cb#136`（V3-009/V3-010） | `cli/opsb_core/services/queue_service.py` + `ipc_bridge.py`，出口 `parhelion_report(view=queue)` |

【推断】这 10 条可以归成三簇：**① 输入侧缺「官方口径」**（#1 #4 #8 → 估计器只能靠猜）；**② 观测侧缺「设备与冷态」**（#2 #3 #9 → 只能靠 ssh 和数文件大小）；**③ 控制侧缺「可信闸门」**（#5 #6 #7 #10 → 门禁要么不筛要么筛错）。三簇分别对 4.3 的 `estimator/judge_cases`、`device_env/fast_feedback`、`gate/calibration/queue_service`。

---

## 附录 A 复现要点

- N-分布：遍历 `~/Competitions/ops-bench/results/<题>/<40位sha>/result.json` 取 `finished_at`，按 `/tmp/p19/receipts.json` 的 `submitted` 逐题排序切窗计数。分类：Pass 内「是否超过该题历史最高分（>0.5 分）」三分类；失败按 `status`。
- `d_hid` 偏差：正则 `vs ([0-9a-f]{6,12})` 取基准 SHA7，在同题已判回执里找基准分，`act = score − base_score`。剪刀差：`pub_avg = 100·sum_vis/5`、`hid_avg = 100·(score/2 − sum_vis)/45`，恒等式 `public_pts = 2·sum_vis` 已用 30 条记录验证。
- 噪声 σ：同 `(title, sha40)` 重复组的组内残差平方和 / (n−g)，df=49。工具频次：`run_command` 的 `args.CommandLine` 统一剥外层引号后做全字符串正则，不取首 token（首命令是 `cd`/`ssh`/`python3` 会被 `&&` 链掩盖）。

## 附录 B 最不确定的三点

1. **1 034 是本地验证次数的下界**。目录名即 SHA，同 SHA 重跑会覆盖 `result.json`，所以真实运行次数一定更高；文中用 `opsb test` 出现次数（842）做交叉校验，但 transcript 里的 `opsb test` 也可能被 `&&` 链重复计数。§1.2 的三把尺子（3.83 / 4.44 / 3.61）只能保证量级，不能保证精确到 0.1。
2. **`d_hid` 的偏差 3.7× 里有多少是「估计器错」、多少是「分数函数在高分区必然饱和」尚未分离**。59 条样本里 `vs` 基准不一定是当代 tbest，基准分本身也会漂（`ec0867d` 同 SHA 出现过 49.49 与 65.45 两读）。所以「哪类改动最容易高估」这一节给的是**按 `improved=` 分组的相对结论**，绝对倍数不宜直接搬。
3. **`feedback-log.jsonl` 是 2026-11-25 的事后重建**，42 行全是 `HOLD_LOW_GAIN`，没有任何「放行」样本。§1.4 里对门禁判别力的定量（S2 打出 9 发真提升、`All 4 stages` 失败率 7.8%）是从**回执 note 的标签**反推的，不是从门禁自己的日志读的；如果当时存在过未落盘的 HOLD 决策，这些比率会有偏。
