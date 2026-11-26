# P02 QuantScaleCalib 线上 WA 的本地复现（4.1 #6）

## 1. 结论

**已在本地复现。** 坏 SHA a2717b5 在 final 级别的 55 例中只失败 1 例：tie 变体 `local/wa-e0f2d340-13`（[65,128]，模式 0，含 1024 个恰为 .5 的商）。8320 个输出码中有 24 个不符，第一处在第 4 行第 107 列，结果 29，期望 28：判题与 oracle 都采用"四舍六入五成双"，28.5 应得 28。对照 SHA 9af3c0d（当前在榜，线上 Pass）的 55 例全部通过。复现变体能区分好坏两个 SHA，因此这把逃逸锁现在处于"已复现待修复"。

## 2. 磁盘上的证据不足以确定失败用例

三条 WA 回执（9ffca10 `…e0f2d340`、5041c86 `…e0f68ef6`、a2717b5 `…e0ff92a5`）都只有 `score=0` 和 5 个公开用例的时间，没有 `case_results`、`cases_passed`，也没有失败用例的 ID。另外，判题有 19 例，本地只有 15 例，P01 的映射对这道题是 `unmapped`。所以无法从数据推断判题失败在哪一例。

按计划走回退路径：以运行日志给出的失败形状 2048×2048、模式 0 作为 `--like` 提示，选本地 official 用例中模式相同、元素数最接近的两例，即 13（[64,4096]）和 15（[8192,8192]），两者与提示都相差 16 倍。这些变体标为 `mapped=false`，`judge_testcase_id=null`。

## 3. oracle 能表达什么（未改 oracle）

- `reference.py` 的生成器有 basic、positive、negative、nan、all_nan_channel、outlier、const_channel、tiny_zero、saturate、many_channels、huge、layout_pair、multi_mode，生成的数据都要经过 `_fix_ties`。这一步会把离 .5 不到 1e-4 的商推离中点。**任何 seed 都造不出平局。** 只有 `gen='tie'`（官方 11 例）直接接收 `values`，原值保留，不做修正。
- 因此平局变体只能写成显式值。2048×2048 需要 400 万个值，没法放进套件 JSON，平局变体的上限定为 16384 元素。2048×2048 这个形状由 seed 变体覆盖：在 13、15 两例上覆盖 `shape`，数据是随机且不含平局的。
- 平局值由 `scheduler/wa_ties.py` 用 float32 精确构造。每 4 个通道中 3 个用 2 的幂作 scale，商精确；第 4 个用任意 scale，商不精确，更容易出错。构造结果分别用纯 Python 和 numpy 核对过，并在 oracle 的本地副本上跑过 `build_case` 和 `run`：平局位置的期望码全部为偶数。

| 变体 | 形状 | 恰为 .5 的商 | a2717b5 | 9af3c0d |
|---|---|---:|---|---|
| `wa-e0f2d340-12` | [1025,4]，50% 平局 | 2048 | PASS | PASS |
| `wa-e0f2d340-13` | [65,128]，12.5% 平局 | 1024 | **WA（24/8320）** | PASS |
| `wa-e0f2d340-14` | [128,128]，仅通道 0–3 各一个 | 4 | PASS | PASS |
| `wa-<sid8>-00…11` × 3 条回执 | 13 例或 15 例，覆盖为 [2048,2048]，12 个 seed | 0 | 36/36 PASS | 36/36 PASS |

这与 bench author 在 `calibration.json` 里的判断一致：a2717b5 的修复只覆盖到约 8192 个元素，超出范围后商不是正确舍入。4100 元素的变体在这个范围内，所以通过；8320 元素的变体超出范围就失败了。16384 元素的稀疏变体通过，原因是它 4 个平局中有 3 个在 2 的幂 scale 通道上，商是精确的。bench author 的 [128,128] 用例用的是任意 scale，这一点不同。

## 4. 写入的内容

由 `opsb bench wa-repro QuantScaleCalib --receipt <sid> --like '{"shape":[2048,2048],"mode":0}' --apply` 对三条回执各运行一次（每次约 0.6s，只用 CPU）：

- 平台套件原本是 legacy `case-profile.json`（版本 0），转成 `suite.json` 后升到版本 3，每条回执一个版本。共 40 个变体：原有的 `local/multichannel-round-even`，加上 3×12 个 seed 变体和 3 个平台共享的平局变体。平局变体只随第一条回执生成，后两条回执引用它们。全部只进入 `final` 级别，smoke 与 standard 不变。
- `bench-specs/QuantScaleCalib/wa-repro.json` 记录每条回执的选例、理由和变体 ID。再执行一次 `--apply` 不写任何内容，`suite_version` 保持 3。
- `suite.json` 为 716KB，主要是平局值；final 报告也会带上这些 params。
- 9f1789c 的第四条 WA 回执不在本次范围内，没有处理。

## 5. 真机运行

均从工作树的 `cli/opsb` 运行，`--workspace /Users/winbeau/Competitions/mat-chem-sim-pred`：

| 命令 | 端到端 | 排队 / 持锁 | 结果 |
|---|---|---|---|
| `opsb test QuantScaleCalib --sha a2717b5 --level final` | 39s（07:37:09–07:37:48 UTC） | 0.001s / 5.9s | 54/55，`wa-e0f2d340-13` WA；run `01fe6d95e1f749b68ace789fbfe6ddf9` |
| `opsb test QuantScaleCalib --sha 9af3c0d… --level final`（对照） | 35s（07:38:47–07:39:22） | 0.001s / 6.8s | 55/55；run `b10ee1bfee5a401392cb25e13263dc2e` |

运行结束后，`wa-repro` 给出的锁状态：

- a2717b5：`reproduced`，运行 01fe6d95，失败变体为 `local/wa-e0f2d340-13`。
- 9ffca10、5041c86：`pending`，它们的坏 SHA 还没有跑过 final。
- 按 S1 的规则，对照 9af3c0d 会被判为 `fixed`：同一组输入上，15 个变体全部通过。它本身就是线上通过过的 SHA，S1 原本就不会锁它。

## 6. 下一步

1. 修复要让任意规模的模式 0 商都正确舍入，不能只修某个大小范围。修复版本在 final 级别通过本回执的 15 个变体后，S1 会自动放行（`ESCAPE_REPRO_FIXED`），无需复核试验。
2. 如需让 9ffca10、5041c86 两条回执也标为 reproduced，可在它们上各跑一次 final。这不是解锁的必要条件。
3. 判题的第几例失败仍然未知。4.1 之后的回执会写入 `case_results`，下一次 WA 可以直接走映射路径或时间量级回退。
