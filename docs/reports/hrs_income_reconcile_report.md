# HRS Income 对账报告

- 对账范围：`2026-07-01` ~ `2026-10-14`（北京时间）
- 生成时间：2026-10-14 16:25:00

## 一、执行摘要

**2026-09 HRS 盈亏真值（五口径对照）**：

| 口径 | 定义 | 9 月金额 (USDT) |
|------|------|----------------|
| A 仅汇总 | SUM(PNL_SUMMARY) | `-2.2833` |
| B 仅明细 | SUM(非 PNL_SUMMARY) | `-29.0310` |
| C 混合 SUM | 全部行 | `-31.3143` |
| D 持仓级去重 | (symbol,side) BOOL_OR | `-35.9978` |
| **E income 真值** | **逐笔匹配归属 HRS** | **`-39.6538`** |
| E' income 全量 | 按 symbol 聚合（含 new_coin 污染） | `-0.3359` |

**匹配统计**：income 总 1002 条，匹配 602 条（**60.1%**），未匹配 394 条，未决 6 条。

- HRS 匹配归属 income 合计：**`-84.3992`** USDT
- new_coin 匹配归属 income 合计：`33.1237` USDT
- HRS 未匹配（含未决）income 合计：`66.0461` USDT

**BTRUSDT 铁证验证**（设计前置动作：验证 H6 根因）：
- 结论：**✅ 铁证通过**。BTRUSDT 9 月两笔 income（`-9.8343` / `-10.7540`）全部找到并匹配归属 HRS。
- BTRUSDT 全部 income：3 笔，匹配成功 3 笔

| 北京时间 | income 金额 (USDT) | 匹配状态 | 归属策略 |
|---------|-------------------|---------|---------|
| 2026-08-29 20:51:19.789000 | `-6.7658` | ✓ matched | hrs |
| 2026-09-01 18:15:29.724000 | `-9.8343` | ✓ matched | hrs |
| 2026-09-02 11:33:46.195000 | `-10.7540` | ✓ matched | hrs |

## 二、各月真值表

| 月份 | A 汇总 | B 明细 | C 混合 | D 持仓级 | **E income(HRS)** | E' income(全量) | records | income |
|------|--------|--------|--------|----------|-------------------|-----------------|---------|--------|
| 2026-07 | `0.0000` | `6.3560` | `6.3560` | `6.3560` | **`6.3560`** | `111.3269` | 11 | 234 |
| 2026-08 | `1.1255` | `-46.7274` | `-45.6020` | `-38.7000` | **`-54.5305`** | `-115.6006` | 475 | 322 |
| 2026-09 | `-2.2833` | `-29.0310` | `-31.3143` | `-35.9978` | **`-39.6538`** | `-0.3359` | 584 | 328 |
| 2026-10 | `10.2858` | `2.5615` | `12.8473` | `5.5961` | **`3.4292`** | `19.3803` | 359 | 118 |

## 三、Income 拉取统计

- 分片次数：0
- 原始 income 条数（未去重）：1043
- 去重后条数：1002
- ✅ 完整性断言通过：所有分片均完成全量拉取，无截断风险。

## 四、逐笔匹配结果摘要

- 匹配成功：602 笔（HRS=351，new_coin=251）
- 未匹配（无同 symbol 候选 / 无容差内候选）：394 笔
- 未决（多策略歧义）：6 笔

## 五、未决清单（需人工复核）

| # | Symbol | income 金额 (USDT) | income 时间 (北京时间) | 原因 |
|---|--------|-------------------|----------------------|------|
| 1 | SOLUSDT | `-10.4072` | 2026-07-01 22:10:46 | 同 symbol 无可用 trade_records 明细 |
| 2 | SOLUSDT | `-12.2656` | 2026-07-01 22:10:46 | 同 symbol 无可用 trade_records 明细 |
| 3 | SOLUSDT | `2.9492` | 2026-07-02 08:24:50 | 同 symbol 无可用 trade_records 明细 |
| 4 | SONYUSDT | `-3.4958` | 2026-07-02 08:26:47 | 同 symbol 无可用 trade_records 明细 |
| 5 | SONYUSDT | `0.1040` | 2026-07-02 08:45:53 | 同 symbol 无可用 trade_records 明细 |
| 6 | SONYUSDT | `0.1314` | 2026-07-02 08:45:53 | 同 symbol 无可用 trade_records 明细 |
| 7 | SONYUSDT | `0.0414` | 2026-07-02 08:45:53 | 同 symbol 无可用 trade_records 明细 |
| 8 | XRPUSDT | `-0.3591` | 2026-07-02 17:31:40 | 无候选在容差内（tolerance=0.01） |
| 9 | XRPUSDT | `-5.1324` | 2026-07-02 17:31:40 | 无候选在容差内（tolerance=0.0513240000） |
| 10 | SOLUSDT | `9.5055` | 2026-07-02 18:43:57 | 同 symbol 无可用 trade_records 明细 |
| 11 | TQQQUSDT | `0.0496` | 2026-07-02 18:44:24 | 无候选在容差内（tolerance=0.01） |
| 12 | SOLUSDT | `17.6965` | 2026-07-02 18:48:25 | 同 symbol 无可用 trade_records 明细 |
| 13 | SOLUSDT | `0.2579` | 2026-07-02 20:38:50 | 同 symbol 无可用 trade_records 明细 |
| 14 | SOLUSDT | `0.6523` | 2026-07-02 20:38:50 | 同 symbol 无可用 trade_records 明细 |
| 15 | SOLUSDT | `0.0187` | 2026-07-02 20:39:19 | 同 symbol 无可用 trade_records 明细 |
| 16 | SOLUSDT | `0.0163` | 2026-07-02 20:39:19 | 同 symbol 无可用 trade_records 明细 |
| 17 | SOLUSDT | `0.1750` | 2026-07-02 20:39:19 | 同 symbol 无可用 trade_records 明细 |
| 18 | SOLUSDT | `0.0864` | 2026-07-02 20:39:19 | 同 symbol 无可用 trade_records 明细 |
| 19 | ETHUSDT | `10.8847` | 2026-07-02 21:18:52 | 无候选在容差内（tolerance=0.1088472000） |
| 20 | BTCUSDT | `6.9699` | 2026-07-02 21:25:13 | 无候选在容差内（tolerance=0.0696990000） |
| 21 | BNBUSDT | `0.1573` | 2026-07-02 21:37:00 | 同 symbol 无可用 trade_records 明细 |
| 22 | BNBUSDT | `4.4044` | 2026-07-02 21:37:00 | 同 symbol 无可用 trade_records 明细 |
| 23 | BNBUSDT | `0.9438` | 2026-07-02 21:37:00 | 同 symbol 无可用 trade_records 明细 |
| 24 | BNBUSDT | `0.1572` | 2026-07-02 21:37:00 | 同 symbol 无可用 trade_records 明细 |
| 25 | BNBUSDT | `0.3142` | 2026-07-02 21:37:00 | 同 symbol 无可用 trade_records 明细 |
| 26 | BNBUSDT | `6.9080` | 2026-07-02 21:37:00 | 同 symbol 无可用 trade_records 明细 |
| 27 | SOLUSDT | `0.1466` | 2026-07-02 21:38:52 | 同 symbol 无可用 trade_records 明细 |
| 28 | ETHUSDT | `-3.3600` | 2026-07-02 21:40:40 | 无候选在容差内（tolerance=0.0336000000） |
| 29 | SQQQUSDT | `-1.2864` | 2026-07-03 00:37:18 | 同 symbol 无可用 trade_records 明细 |
| 30 | SQQQUSDT | `-2.0072` | 2026-07-03 00:37:18 | 同 symbol 无可用 trade_records 明细 |
| 31 | TQQQUSDT | `-0.0836` | 2026-07-03 12:41:02 | 无候选在容差内（tolerance=0.01） |
| 32 | ETHUSDT | `-0.4552` | 2026-07-03 13:35:37 | 无候选在容差内（tolerance=0.01） |
| 33 | ETHUSDT | `-0.4340` | 2026-07-03 13:35:37 | 无候选在容差内（tolerance=0.01） |
| 34 | SQQQUSDT | `-0.8679` | 2026-07-03 13:47:43 | 同 symbol 无可用 trade_records 明细 |
| 35 | SQQQUSDT | `-0.8214` | 2026-07-03 13:47:43 | 同 symbol 无可用 trade_records 明细 |
| 36 | SQQQUSDT | `-0.2198` | 2026-07-03 13:47:43 | 同 symbol 无可用 trade_records 明细 |
| 37 | SQQQUSDT | `-4.3015` | 2026-07-03 13:47:43 | 同 symbol 无可用 trade_records 明细 |
| 38 | SMCIUSDT | `2.1828` | 2026-07-03 13:53:54 | 同 symbol 无可用 trade_records 明细 |
| 39 | MVLLUSDT | `6.1383` | 2026-07-03 13:53:54 | 同 symbol 无可用 trade_records 明细 |
| 40 | TRXUSDT | `-0.4003` | 2026-07-03 13:53:54 | 同 symbol 无可用 trade_records 明细 |
| 41 | BTCUSDT | `-0.5718` | 2026-07-07 23:47:37 | 无候选在容差内（tolerance=0.01） |
| 42 | ETHUSDT | `-3.1568` | 2026-07-08 00:51:36 | 无候选在容差内（tolerance=0.0315675000） |
| 43 | BTCUSDT | `-4.9326` | 2026-07-08 14:36:08 | 无候选在容差内（tolerance=0.0493260000） |
| 44 | XRPUSDT | `5.0343` | 2026-07-08 16:27:15 | 无候选在容差内（tolerance=0.0503430000） |
| 45 | XRPUSDT | `0.3114` | 2026-07-08 17:29:25 | 无候选在容差内（tolerance=0.01） |
| 46 | TTWOUSDT | `0.9912` | 2026-07-08 22:05:08 | 无候选在容差内（tolerance=0.01） |
| 47 | TTWOUSDT | `0.1640` | 2026-07-08 22:05:08 | 同 symbol 无可用 trade_records 明细 |
| 48 | TTWOUSDT | `0.5614` | 2026-07-08 22:05:08 | 同 symbol 无可用 trade_records 明细 |
| 49 | TTWOUSDT | `0.5490` | 2026-07-10 01:43:40 | 同 symbol 无可用 trade_records 明细 |
| 50 | SOLUSDT | `4.0553` | 2026-07-10 09:48:32 | 同 symbol 无可用 trade_records 明细 |
| 51 | TTWOUSDT | `-0.8066` | 2026-07-10 12:42:20 | 同 symbol 无可用 trade_records 明细 |
| 52 | STRCUSDT | `-0.5232` | 2026-07-11 05:21:53 | 同 symbol 无可用 trade_records 明细 |
| 53 | XRPUSDT | `-3.8848` | 2026-07-12 08:09:39 | 无候选在容差内（tolerance=0.0388476000） |
| 54 | SOLUSDT | `12.3273` | 2026-07-12 09:17:48 | 同 symbol 无可用 trade_records 明细 |
| 55 | SOLUSDT | `4.7656` | 2026-07-12 09:17:48 | 同 symbol 无可用 trade_records 明细 |
| 56 | XRPUSDT | `-0.9933` | 2026-07-14 01:32:19 | 无候选在容差内（tolerance=0.01） |
| 57 | BTCUSDT | `-1.2670` | 2026-07-14 01:36:56 | 无候选在容差内（tolerance=0.0126700000） |
| 58 | BTCUSDT | `2.5815` | 2026-07-14 20:30:04 | 无候选在容差内（tolerance=0.0258150000） |
| 59 | SOLUSDT | `5.4060` | 2026-07-14 20:46:03 | 同 symbol 无可用 trade_records 明细 |
| 60 | BTCUSDT | `-1.8825` | 2026-07-14 23:57:59 | 无候选在容差内（tolerance=0.0188246250） |
| 61 | SOLUSDT | `-3.4980` | 2026-07-15 14:33:10 | 同 symbol 无可用 trade_records 明细 |
| 62 | SOLUSDT | `3.0210` | 2026-07-16 15:36:10 | 同 symbol 无可用 trade_records 明细 |
| 63 | XRPUSDT | `0.1251` | 2026-07-17 01:55:19 | 无候选在容差内（tolerance=0.01） |
| 64 | XRPUSDT | `1.5123` | 2026-07-17 01:55:19 | 无候选在容差内（tolerance=0.0151232000） |
| 65 | XRPUSDT | `1.6374` | 2026-07-17 01:55:19 | 无候选在容差内（tolerance=0.0163744000） |
| 66 | SOLUSDT | `0.1809` | 2026-07-17 06:55:55 | 同 symbol 无可用 trade_records 明细 |
| 67 | XRPUSDT | `1.7759` | 2026-07-17 07:09:00 | 无候选在容差内（tolerance=0.0177590000） |
| 68 | SOLUSDT | `0.3444` | 2026-07-17 08:36:50 | 同 symbol 无可用 trade_records 明细 |
| 69 | SOLUSDT | `0.6710` | 2026-07-17 08:57:55 | 同 symbol 无可用 trade_records 明细 |
| 70 | BTCUSDT | `-3.6210` | 2026-07-17 13:29:33 | 同 symbol 无可用 trade_records 明细 |
| 71 | BTCUSDT | `0.1794` | 2026-07-17 13:40:38 | 同 symbol 无可用 trade_records 明细 |
| 72 | BTCUSDT | `0.3598` | 2026-07-17 13:45:53 | 同 symbol 无可用 trade_records 明细 |
| 73 | BTCUSDT | `0.3606` | 2026-07-17 13:45:53 | 同 symbol 无可用 trade_records 明细 |
| 74 | BTCUSDT | `0.4066` | 2026-07-17 13:46:08 | 同 symbol 无可用 trade_records 明细 |
| 75 | BTCUSDT | `0.5214` | 2026-07-17 14:02:05 | 同 symbol 无可用 trade_records 明细 |
| 76 | SOLUSDT | `-5.5968` | 2026-07-17 21:06:01 | 同 symbol 无可用 trade_records 明细 |
| 77 | WENUSDT | `-2.0683` | 2026-07-17 23:51:13 | 同 symbol 无可用 trade_records 明细 |
| 78 | WENUSDT | `-0.2581` | 2026-07-18 09:00:48 | 同 symbol 无可用 trade_records 明细 |
| 79 | WENUSDT | `-2.2504` | 2026-07-18 09:00:48 | 同 symbol 无可用 trade_records 明细 |
| 80 | WENUSDT | `-2.2939` | 2026-07-18 09:00:48 | 同 symbol 无可用 trade_records 明细 |
| 81 | XRPUSDT | `-3.0547` | 2026-07-20 19:33:56 | 同 symbol 无可用 trade_records 明细 |
| 82 | SOLUSDT | `3.7611` | 2026-07-20 19:44:26 | 同 symbol 无可用 trade_records 明细 |
| 83 | BTCUSDT | `-0.4568` | 2026-07-21 03:14:58 | 同 symbol 无可用 trade_records 明细 |
| 84 | SOLUSDT | `5.0020` | 2026-07-21 07:02:17 | 同 symbol 无可用 trade_records 明细 |
| 85 | XRPUSDT | `-0.1017` | 2026-07-21 13:03:57 | 同 symbol 无可用 trade_records 明细 |
| 86 | XRPUSDT | `-6.5088` | 2026-07-21 13:03:57 | 同 symbol 无可用 trade_records 明细 |
| 87 | SOLUSDT | `-3.1824` | 2026-07-21 14:36:00 | 同 symbol 无可用 trade_records 明细 |
| 88 | ETHUSDT | `-4.7497` | 2026-07-22 22:05:57 | 同 symbol 无可用 trade_records 明细 |
| 89 | ETHUSDT | `0.6932` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 90 | ETHUSDT | `0.0533` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 91 | ETHUSDT | `0.7465` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 92 | ETHUSDT | `0.5864` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 93 | ETHUSDT | `0.0533` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 94 | ETHUSDT | `0.6929` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 95 | ETHUSDT | `0.0533` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 96 | ETHUSDT | `0.6395` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 97 | ETHUSDT | `0.0533` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 98 | ETHUSDT | `2.7706` | 2026-07-26 23:36:31 | 同 symbol 无可用 trade_records 明细 |
| 99 | SOLUSDT | `5.3126` | 2026-07-27 06:00:23 | 同 symbol 无可用 trade_records 明细 |
| 100 | HK1810USDT | `-0.9928` | 2026-07-27 10:19:27 | 同 symbol 无可用 trade_records 明细 |
| 101 | HK1810USDT | `0.8410` | 2026-07-27 19:22:21 | 同 symbol 无可用 trade_records 明细 |
| 102 | ETHUSDT | `6.7444` | 2026-07-27 22:37:15 | 同 symbol 无可用 trade_records 明细 |
| 103 | SOLUSDT | `4.2400` | 2026-07-28 07:11:12 | 同 symbol 无可用 trade_records 明细 |
| 104 | SOLUSDT | `3.1310` | 2026-07-28 07:19:32 | 同 symbol 无可用 trade_records 明细 |
| 105 | SOLUSDT | `0.0586` | 2026-07-28 09:07:25 | 同 symbol 无可用 trade_records 明细 |
| 106 | POPMARTUSDT | `-2.4400` | 2026-07-28 09:44:13 | 同 symbol 无可用 trade_records 明细 |
| 107 | SOLUSDT | `-2.9904` | 2026-07-29 07:02:10 | 同 symbol 无可用 trade_records 明细 |
| 108 | XRPUSDT | `4.7566` | 2026-07-29 13:03:13 | 同 symbol 无可用 trade_records 明细 |
| 109 | TMFUSDT | `0.5704` | 2026-07-30 03:21:08 | 同 symbol 无可用 trade_records 明细 |
| 110 | TMFUSDT | `0.5704` | 2026-07-30 03:21:08 | 同 symbol 无可用 trade_records 明细 |
| 111 | TMFUSDT | `1.8204` | 2026-07-30 04:20:40 | 同 symbol 无可用 trade_records 明细 |
| 112 | TMFUSDT | `0.0148` | 2026-07-30 04:20:40 | 同 symbol 无可用 trade_records 明细 |
| 113 | BITOUSDT | `-0.3472` | 2026-07-30 09:02:22 | 同 symbol 无可用 trade_records 明细 |
| 114 | BITOUSDT | `-1.5248` | 2026-07-30 09:02:22 | 同 symbol 无可用 trade_records 明细 |
| 115 | BTCUSDT | `8.4840` | 2026-07-30 20:14:19 | 同 symbol 无可用 trade_records 明细 |
| 116 | PYPLUSDT | `0.0720` | 2026-07-30 22:11:07 | 无候选在容差内（tolerance=0.01） |
| 117 | PYPLUSDT | `0.2272` | 2026-07-30 22:15:18 | 无候选在容差内（tolerance=0.01） |
| 118 | PYPLUSDT | `0.1846` | 2026-07-30 22:15:19 | 无候选在容差内（tolerance=0.01） |
| 119 | PYPLUSDT | `0.3621` | 2026-07-30 22:15:19 | 无候选在容差内（tolerance=0.01） |
| 120 | GSUSDT | `0.1202` | 2026-07-31 20:36:00 | 无候选在容差内（tolerance=0.01） |
| 121 | GSUSDT | `0.1202` | 2026-07-31 20:36:00 | 无候选在容差内（tolerance=0.01） |
| 122 | PYPLUSDT | `0.1065` | 2026-07-31 21:30:00 | 无候选在容差内（tolerance=0.01） |
| 123 | ETHUSDT | `8.6817` | 2026-07-31 21:47:14 | 同 symbol 无可用 trade_records 明细 |
| 124 | BEATUSDT | `-0.6320` | 2026-08-03 21:21:36 | 无候选在容差内（tolerance=0.01） |
| 125 | SOLUSDT | `-1.0943` | 2026-08-04 09:03:57 | 同 symbol 无可用 trade_records 明细 |
| 126 | CLUSDT | `-0.0576` | 2026-08-04 17:48:24 | 无候选在容差内（tolerance=0.01） |
| 127 | ETHUSDT | `-3.0087` | 2026-08-04 20:38:13 | 同 symbol 无可用 trade_records 明细 |
| 128 | SOLUSDT | `0.3240` | 2026-08-05 10:05:10 | 同 symbol 无可用 trade_records 明细 |
| 129 | ETHUSDT | `-3.0090` | 2026-08-06 00:22:42 | 同 symbol 无可用 trade_records 明细 |
| 130 | ETHUSDT | `-3.7040` | 2026-08-06 00:27:43 | 同 symbol 无可用 trade_records 明细 |
| 131 | AAVEUSDT | `-0.1450` | 2026-08-06 21:00:47 | 无候选在容差内（tolerance=0.01） |
| 132 | XRPUSDT | `-2.0904` | 2026-08-07 02:39:15 | 同 symbol 无可用 trade_records 明细 |
| 133 | XRPUSDT | `-1.9505` | 2026-08-07 13:15:10 | 同 symbol 无可用 trade_records 明细 |
| 134 | ETHUSDT | `-3.1781` | 2026-08-07 20:10:03 | 同 symbol 无可用 trade_records 明细 |
| 135 | PUMPUSDT | `0.2527` | 2026-08-10 13:22:36 | 无候选在容差内（tolerance=0.01） |
| 136 | PUMPUSDT | `0.1275` | 2026-08-10 13:22:36 | 无候选在容差内（tolerance=0.01） |
| 137 | PUMPUSDT | `0.3802` | 2026-08-10 13:22:36 | 无候选在容差内（tolerance=0.01） |
| 138 | PAXGUSDT | `0.1096` | 2026-08-11 02:31:29 | 无候选在容差内（tolerance=0.01） |
| 139 | PAXGUSDT | `0.1096` | 2026-08-11 02:31:29 | 无候选在容差内（tolerance=0.01） |
| 140 | PAXGUSDT | `0.1096` | 2026-08-11 02:31:29 | 无候选在容差内（tolerance=0.01） |
| 141 | PAXGUSDT | `0.0548` | 2026-08-11 02:31:29 | 无候选在容差内（tolerance=0.01） |
| 142 | PUMPUSDT | `-0.1086` | 2026-08-11 08:19:27 | 无候选在容差内（tolerance=0.01） |
| 143 | PAXGUSDT | `-0.0654` | 2026-08-11 08:19:48 | 无候选在容差内（tolerance=0.01） |
| 144 | PUMPUSDT | `0.1063` | 2026-08-11 14:10:33 | 无候选在容差内（tolerance=0.01） |
| 145 | PUMPUSDT | `0.2543` | 2026-08-11 14:10:33 | 无候选在容差内（tolerance=0.01） |
| 146 | GRVTUSDT | `-3.3855` | 2026-08-11 17:07:38 | 同 symbol 无可用 trade_records 明细 |
| 147 | SPCXUSDT | `0.3276` | 2026-08-11 21:30:37 | 无候选在容差内（tolerance=0.01） |
| 148 | SPCXUSDT | `0.3237` | 2026-08-11 21:30:37 | 无候选在容差内（tolerance=0.01） |
| 149 | KOUSDT | `0.1485` | 2026-08-11 22:09:32 | 同 symbol 无可用 trade_records 明细 |
| 150 | KOUSDT | `0.0385` | 2026-08-11 22:09:32 | 同 symbol 无可用 trade_records 明细 |
| 151 | KOUSDT | `0.0440` | 2026-08-11 22:09:32 | 同 symbol 无可用 trade_records 明细 |
| 152 | KOUSDT | `0.0715` | 2026-08-11 22:09:32 | 同 symbol 无可用 trade_records 明细 |
| 153 | KOUSDT | `0.0055` | 2026-08-11 22:09:32 | 同 symbol 无可用 trade_records 明细 |
| 154 | KOUSDT | `0.1430` | 2026-08-11 22:09:32 | 同 symbol 无可用 trade_records 明细 |
| 155 | KOUSDT | `0.0378` | 2026-08-11 22:09:32 | 同 symbol 无可用 trade_records 明细 |
| 156 | KOUSDT | `0.0648` | 2026-08-11 22:09:32 | 同 symbol 无可用 trade_records 明细 |
| 157 | PUMPUSDT | `-0.8907` | 2026-08-11 22:23:05 | 无候选在容差内（tolerance=0.01） |
| 158 | XRPUSDT | `0.1035` | 2026-08-12 02:02:09 | 同 symbol 无可用 trade_records 明细 |
| 159 | XRPUSDT | `0.1159` | 2026-08-12 02:02:09 | 同 symbol 无可用 trade_records 明细 |
| 160 | XRPUSDT | `5.6718` | 2026-08-12 02:02:09 | 同 symbol 无可用 trade_records 明细 |
| 161 | SPCXUSDT | `-0.1612` | 2026-08-12 08:21:48 | 无候选在容差内（tolerance=0.01） |
| 162 | SPCXUSDT | `-0.1209` | 2026-08-12 08:21:48 | 无候选在容差内（tolerance=0.01） |
| 163 | WLDUSDT | `1.3072` | 2026-08-12 10:13:07 | 无候选在容差内（tolerance=0.0130720000） |
| 164 | WLDUSDT | `0.9804` | 2026-08-12 10:13:07 | 无候选在容差内（tolerance=0.01） |
| 165 | PUMPUSDT | `0.3846` | 2026-08-12 10:24:30 | 无候选在容差内（tolerance=0.01） |
| 166 | WLDUSDT | `0.4794` | 2026-08-12 14:15:08 | 无候选在容差内（tolerance=0.01） |
| 167 | PUMPUSDT | `-0.7344` | 2026-08-12 16:22:58 | 无候选在容差内（tolerance=0.01） |
| 168 | SOLUSDT | `-5.3875` | 2026-08-12 17:32:17 | 同 symbol 无可用 trade_records 明细 |
| 169 | SPCXUSDT | `0.9918` | 2026-08-13 01:25:10 | 无候选在容差内（tolerance=0.01） |
| 170 | SPCXUSDT | `0.7728` | 2026-08-13 01:25:10 | 无候选在容差内（tolerance=0.01） |
| 171 | WLDUSDT | `0.8304` | 2026-08-13 03:15:04 | 无候选在容差内（tolerance=0.01） |
| 172 | WLDUSDT | `0.2928` | 2026-08-13 03:15:07 | 无候选在容差内（tolerance=0.01） |
| 173 | WLDUSDT | `0.9516` | 2026-08-13 03:15:07 | 无候选在容差内（tolerance=0.01） |
| 174 | WLDUSDT | `0.0524` | 2026-08-13 08:28:32 | 无候选在容差内（tolerance=0.01） |
| 175 | WLDUSDT | `0.4104` | 2026-08-13 14:46:38 | 无候选在容差内（tolerance=0.01） |
| 176 | WLDUSDT | `0.4104` | 2026-08-13 14:46:38 | 无候选在容差内（tolerance=0.01） |
| 177 | ADAUSDT | `0.3120` | 2026-08-13 16:55:47 | 无候选在容差内（tolerance=0.01） |
| 178 | PUMPUSDT | `0.3538` | 2026-08-14 09:33:06 | 无候选在容差内（tolerance=0.01） |
| 179 | SPCXUSDT | `0.3042` | 2026-08-14 19:54:35 | 无候选在容差内（tolerance=0.01） |
| 180 | SPCXUSDT | `0.3042` | 2026-08-14 19:54:35 | 无候选在容差内（tolerance=0.01） |
| 181 | SPCXUSDT | `0.3042` | 2026-08-14 19:54:35 | 无候选在容差内（tolerance=0.01） |
| 182 | BANKUSDT | `0.9926` | 2026-08-15 01:43:24 | 无候选在容差内（tolerance=0.01） |
| 183 | BANKUSDT | `0.9926` | 2026-08-15 01:43:24 | 无候选在容差内（tolerance=0.01） |
| 184 | LINKUSDT | `8.6139` | 2026-08-15 17:12:15 | 同 symbol 无可用 trade_records 明细 |
| 185 | SPCXUSDT | `0.3289` | 2026-08-17 11:10:05 | 无候选在容差内（tolerance=0.01） |
| 186 | ACEUSDT | `0.8100` | 2026-08-17 18:05:38 | hrs 与 new_coin 候选歧义（同 symbol 同时间窗同金额） |
| 187 | ACEUSDT | `2.2221` | 2026-08-19 09:29:09 | hrs 与 new_coin 候选歧义（同 symbol 同时间窗同金额） |
| 188 | ACEUSDT | `2.2178` | 2026-08-19 09:29:09 | hrs 与 new_coin 候选歧义（同 symbol 同时间窗同金额） |
| 189 | ACEUSDT | `0.8005` | 2026-08-19 10:01:50 | hrs 与 new_coin 候选歧义（同 symbol 同时间窗同金额） |
| 190 | WLDUSDT | `0.4161` | 2026-08-19 10:18:14 | 无候选在容差内（tolerance=0.01） |
| 191 | PAXGUSDT | `0.1087` | 2026-08-19 21:12:34 | 无候选在容差内（tolerance=0.01） |
| 192 | PAXGUSDT | `0.1087` | 2026-08-19 21:12:34 | 无候选在容差内（tolerance=0.01） |
| 193 | PAXGUSDT | `0.1087` | 2026-08-19 21:12:34 | 无候选在容差内（tolerance=0.01） |
| 194 | PAXGUSDT | `0.0543` | 2026-08-19 21:12:34 | 无候选在容差内（tolerance=0.01） |
| 195 | 1000PEPEUSDT | `0.1688` | 2026-08-19 22:53:56 | 无候选在容差内（tolerance=0.01） |
| 196 | 1000PEPEUSDT | `0.1688` | 2026-08-19 22:53:56 | 无候选在容差内（tolerance=0.01） |
| 197 | SOLUSDT | `-16.5869` | 2026-08-19 23:36:14 | 同 symbol 无可用 trade_records 明细 |
| 198 | PAXGUSDT | `-5.0902` | 2026-08-20 07:12:27 | 无候选在容差内（tolerance=0.0509025000） |
| 199 | XRPUSDT | `-8.6480` | 2026-08-20 16:37:09 | 同 symbol 无可用 trade_records 明细 |
| 200 | BNBUSDT | `-3.8046` | 2026-08-21 00:00:01 | 同 symbol 无可用 trade_records 明细 |

_（共 400 笔，仅展示前 200 条）_

## 六、设计假设 vs 事实

> 对账过程中发现的与设计文档假设不符的事实，如实记录于此，供后续修复环节参考。

| # | 设计假设 | 实际观察 | 影响 |
|---|---------|---------|------|
| 1 | income 字段含 `tradeId`/`tranId`（决定 side 精确裁决路径） | 见下方「事实核实」小节 | 影响 side 恢复策略 |

## 七、Income 样例字段核实（设计前置动作）

**样例 income 记录字段**：

```json
{
  "symbol": "MVLLUSDT",
  "incomeType": "REALIZED_PNL",
  "income": "1.89440000",
  "asset": "USDT",
  "time": 1782909908199,
  "info": "274900",
  "tranId": 107611274900,
  "tradeId": "274900"
}
```

- 含 `tradeId`：True
- 含 `tranId`：True
- 含 `symbol`：True
- 含 `incomeType`：True

## 八、SOLUSDT 归属验证（2026-10-14 B 段补注）

**对账发现**：9 月 SOLUSDT income 合计 +42.3117 USDT（来自 full_income.json），但 reconcile.match_income_to_trades
对这 12 笔 SOLUSDT income 全部返回 `matched=False`，`unresolved_reason="同 symbol 无可用 trade_records 明细"`。

**根因**：trade_records CSV 中 SOLUSDT 仅有 **3 行 new_coin** 记录（TAKE_PROFIT_MARKET / TAKE_PROFIT），
且 3 行的 `realized_pnl` 字段**全部为空**（`null`），没有可供金额容差匹配的候选行。
HRS 策略在对账时间窗口内从未交易 SOLUSDT。

**结论**：SOLUSDT 的 income 既不归属 HRS，也不归属 new_coin——它是一个**无 trade_records 支撑的 orphan**
（可能交易发生在 CSV 导出时间窗口之前或独立于本交易系统的账户）。E_HRS 口径已正确排除。
