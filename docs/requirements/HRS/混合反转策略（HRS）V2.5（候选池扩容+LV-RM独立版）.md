# 混合反转策略 HRS V2.5（候选池扩容 + LV-RM 独立版）
## Hybrid Reversal Strategy - Candidate Pool Expansion & LV-RM Independence

---

## 第一部分：V2.4 → V2.5 修改点清单（变更日志）

| 修改项 | V2.4（旧值） | V2.5（新值） | 修改理由 |
|--------|-------------|-------------|----------|
| **候选池筛选逻辑** | 4 条件 AND（必须全部满足） | **4 条件中满足任意 2 个即可入池** | V2.4 候选池长期为空，策略停摆 |
| **动态阈值分位数** | 做空：80 分位 / 做多：20 分位 | **做空/做多均使用 50 分位（中位数）** | 高分位数门槛过高，候选池几乎无币种达标 |
| **涨跌幅阈值** | 做空 ≥ 8%，做多 ≤ -6%（文档写 12%/-10%） | **对称统一为 8%（绝对值），文档同步修正** | 消除文档与代码不一致，对称逻辑更清晰 |
| **涨跌幅角色** | 硬性条件（必须满足） | **作为"任意 2 条件"之一** | 不是硬性门槛，但仍是候选池的重要维度之一 |
| **LV-RM 扫描范围** | 候选池落选币种中筛选（依赖候选池） | **全市场流动性币种中直接筛选** | 候选池为空时 LV-RM 也停摆，独立后保证 LV-RM 始终可用 |
| **LV-RM 涨跌幅过滤** | \|涨跌幅\| < 5% | **\|涨跌幅\| < 8%** | 与价格变化阈值统一，扩大 LV-RM 扫描范围 |
| **候选池为空休眠** | 候选池为空时休眠，不评分 | **候选池为空时不再休眠，仅 LV-RM 模块继续工作** | 保持 LV-RM 独立运行，避免策略完全停摆 |
| **版本号** | V2.4 | **V2.5** | 候选池逻辑重构 + LV-RM 独立，架构级变更 |

---

## 第二部分：根本问题回顾

### 2.1 V2.4 候选池为什么长期为空

```
V2.4 做多候选池 = 涨跌幅≤-6% AND 费率≤20分位 AND OI/市值≤20分位 AND EMA偏离≤30分位
V2.4 做空候选池 = 涨跌幅≥8%  AND 费率≥80分位 AND OI/市值≥80分位 AND EMA偏离≥70分位
```

**4 个 AND 条件叠加**，每个条件筛选掉大部分币种，最终交集几乎为空：

| 维度 | 做多要求 | 市场实际 | 结果 |
|------|---------|----------|------|
| 涨跌幅（固定） | ≤ -6% | 当前低波动，大部分币种在 -3%~+3% 之间 | 大部分币种不达标 |
| 费率（动态） | ≤ 市场 20 分位 | 当前市场正费率为主，BOTTOM 20% 约 -3.38% | 极少数币种达标 |
| OI/市值（动态） | ≤ 市场 20 分位 | 极严格 | 极少数币种达标 |
| EMA20 偏离（动态） | ≤ 市场 30 分位 | 约 -0.58% | 极少数币种达标 |

**实际数据验证**（2026-07-31 扫描，67 个样本）：
- 做空候选：0 个
- 做多候选：0 个
- 总计：0 个

### 2.2 问题根因一句话总结

> **候选池承担了"评分系统"的职责，条件过于严格，导致入口被堵死，整个策略（包括 EMM、半EMM、标准模式、LV-RM）全部停摆。**

---

## 第三部分：V2.5 核心设计

### 3.1 设计哲学变更

```
V2.4（旧）：候选池 = 精筛入口 → 评分系统做二次精筛（重复筛选）
V2.5（新）：候选池 = 粗筛入口 → 评分系统做唯一精筛（职责单一）
```

**候选池只负责"粗筛"**，把明显不合适的币种排除（流动性不足、稳定币、杠杆代币），**让评分系统来决定最终是否入场**。

### 3.2 候选池筛选逻辑（核心变更）

**旧逻辑（AND）**：
```
入池 = 条件A达标 AND 条件B达标 AND 条件C达标 AND 条件D达标
```

**新逻辑（OR-2）**：
```
入池 = 条件A/条件B/条件C/条件D 中，满足任意 2 个即可
```

**4 个条件维度**：

| 编号 | 维度 | 做空达标条件 | 做多达标条件 | 来源 |
|------|------|-------------|-------------|------|
| A | **24h 涨跌幅** | ≥ 8% | ≤ -8% | 固定阈值（与代码一致） |
| B | **资金费率（年化）** | ≥ 市场 **50 分位** | ≤ 市场 **50 分位** | 动态阈值（中位数） |
| C | **OI / 市值比** | ≥ 市场 **50 分位** | ≤ 市场 **50 分位** | 动态阈值（中位数） |
| D | **EMA20(4h) 偏离** | ≥ 市场 **50 分位** | ≤ 市场 **50 分位** | 动态阈值（中位数） |

**入池规则**：
- 4 个条件中，**任意 2 个及以上达标**，即可进入候选池
- 进入候选池后，由评分系统按标准流程评分（总分 ≥ 6.0 才入场）
- 做空和做多独立计算，一个币种可同时进入做空和做多候选池

### 3.3 动态阈值调整：70/80 分位 → 50 分位（中位数）

| 参数 | V2.4 | V2.5 | 调整理由 |
|------|------|------|----------|
| 费率做空分位数 | 80 分位 | **50 分位** | 高分位数导致候选池为空 |
| 费率做多分位数 | 20 分位 | **50 分位** | 低分位数导致候选池为空 |
| OI/市值做空分位数 | 80 分位 | **50 分位** | 同上 |
| OI/市值做多分位数 | 20 分位 | **50 分位** | 同上 |
| EMA20 做空分位数 | 70 分位 | **50 分位** | 同上 |
| EMA20 做多分位数 | 30 分位 | **50 分位** | 同上 |

**EMM 分位数保持不变**（EMM 极端市场模块需要极端条件）：
| 参数 | 值 | 说明 |
|------|-----|------|
| 费率做多 EMM 分位数 | 10 分位 | 不变 |
| 费率做空 EMM 分位数 | 90 分位 | 不变 |
| OI/市值 EMM 分位数 | 90 分位 | 不变 |

### 3.4 预期候选池扩容效果

基于 2026-07-31 的 67 个样本数据估算：

| 场景 | V2.4（AND 4条件） | V2.5（OR-2 + 50分位） |
|------|------------------|---------------------|
| 做空候选 | ~0 个 | **~5-8 个** |
| 做多候选 | ~0 个 | **~3-5 个** |
| 总计 | ~0 个 | **~8-13 个** |

**估算依据**：
- 50 分位意味着约 50% 的币种在费率、OI/市值、EMA20 三个维度上各有一半达标
- 涨跌幅 8% 阈值下约 15-20% 的币种达标
- 任意 2 条件组合：概率约为 C(4,2) × 0.5 × 0.5 = 6 种组合，总覆盖率约 15-20%
- 67 个流动性币种 × 15-20% ≈ 10-13 个候选币种

---

## 第四部分：LV-RM 独立化

### 4.1 V2.4 的问题

```
V2.4 LV-RM 扫描范围 = 候选池落选币种（_eliminated_symbols）
                        ↓
    候选池为空 → 落选币种为空 → LV-RM 扫描范围为空 → LV-RM 停摆
```

### 4.2 V2.5 独立方案

```
V2.5 LV-RM 扫描范围 = 全市场流动性币种中 |涨跌幅| < 8%
                        ↓
    候选池无论是否为空，LV-RM 始终正常扫描
```

**流程**：

```
1. 获取所有流动性达标币种（24h成交额≥5000万U，OI≥1000万U）
   → 约 46 个币种
   
2. 过滤出 |24h涨跌幅| < 8% 的币种
   → 约 30-35 个币种（低波动市场下占比更高）
   
3. 对这些币种逐个检查 LV-RM 触发条件：
   a. 布林带触轨（1h）
   b. RSI 超买/超卖（1h）
   c. 资金费率验证
   d. K线形态确认（止跌/滞涨）
   e. 4h 趋势过滤（一票否决）
```

### 4.3 实现方式

新增 `get_lv_rm_scan_range()` 方法，独立于候选池逻辑：

```python
async def get_lv_rm_scan_range(self) -> List[str]:
    """
    V2.5: 获取 LV-RM 独立扫描范围
    从全市场流动性币种中筛选 |涨跌幅| < 8% 的币种
    """
    tickers = await self.market_data.get_all_tickers()
    lv_rm_symbols = []
    
    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if self._should_exclude(symbol):
            continue
        if not self._check_liquidity(ticker):
            continue
        
        # 涨跌幅过滤
        price_change = float(ticker.get("priceChangePercent", 0))
        if abs(price_change) < 8.0:
            lv_rm_symbols.append(symbol)
    
    return lv_rm_symbols
```

### 4.4 候选池为空时不再休眠

V2.4 中，候选池为空时策略进入休眠（不评分、不发心跳）。V2.5 中，**候选池为空时仅跳过标准模式/EMM 检查，LV-RM 模块继续运行**：

```python
# V2.5 逻辑
if not self.candidate_pool.has_candidates():
    # 标准模式/EMM跳过，但 LV-RM 继续
    await self._run_lv_rm_only_cycle()
else:
    await self._run_full_cycle()
```

---

## 第五部分：配置变更

### 5.1 config.yaml 变更

```yaml
# V2.5 候选池配置变更
candidate_pool:
  # ...（其他配置不变）
  
  # V2.5：动态阈值分位数调整为 50 分位（中位数）
  dynamic_thresholds:
    enabled: true
    min_sample_size: 10
    # 做空/做多统一使用 50 分位（中位数）
    funding_rate_percentile_short: 0.50     # V2.4: 0.80
    oi_market_cap_percentile_short: 0.50    # V2.4: 0.80
    ema20_deviation_percentile_short: 0.50  # V2.4: 0.70
    funding_rate_percentile_long: 0.50      # V2.4: 0.20
    oi_market_cap_percentile_long: 0.50     # V2.4: 0.20
    ema20_deviation_percentile_long: 0.50   # V2.4: 0.30
    # EMM 分位数保持不变
    funding_rate_percentile_emm_long: 0.10
    funding_rate_percentile_emm_short: 0.90
    oi_market_cap_percentile_emm: 0.90

  # V2.5：涨跌幅对称统一为 8%
  short:
    price_change_24h: 0.08        # 不变（V2.4 已是 8%）
  long:
    price_change_24h: -0.08       # V2.4: -0.06，统一为 -8%

# V2.5：LV-RM 配置变更
lv_rm:
  enabled: true
  scan:
    # V2.5：LV-RM 扫描范围改为全市场流动性币种
    scan_source: "all_liquid"      # V2.4: "eliminated_candidates"
    max_price_change_24h: 0.08     # V2.4: 0.05，放宽到 8%
    api_concurrency_limit: 10
  # ...（其他评分、止损止盈配置不变）
```

### 5.2 配置项完整变更清单

| 配置项 | 路径 | V2.4 | V2.5 |
|--------|------|------|------|
| 做多涨跌幅阈值 | candidate_pool.long.price_change_24h | -0.06 | **-0.08** |
| 费率做空分位数 | candidate_pool.dynamic_thresholds.funding_rate_percentile_short | 0.80 | **0.50** |
| 费率做多分位数 | candidate_pool.dynamic_thresholds.funding_rate_percentile_long | 0.20 | **0.50** |
| OI/市值做空分位数 | candidate_pool.dynamic_thresholds.oi_market_cap_percentile_short | 0.80 | **0.50** |
| OI/市值做多分位数 | candidate_pool.dynamic_thresholds.oi_market_cap_percentile_long | 0.20 | **0.50** |
| EMA20 做空分位数 | candidate_pool.dynamic_thresholds.ema20_deviation_percentile_short | 0.70 | **0.50** |
| EMA20 做多分位数 | candidate_pool.dynamic_thresholds.ema20_deviation_percentile_long | 0.30 | **0.50** |
| LV-RM 扫描来源 | lv_rm.scan.scan_source | eliminated_candidates | **all_liquid** |
| LV-RM 涨跌幅阈值 | lv_rm.scan.max_price_change_24h | 0.05 | **0.08** |

---

## 第六部分：代码变更清单

### 6.1 candidate_pool.py

| 方法/逻辑 | 变更内容 |
|-----------|----------|
| `scan_and_update()` | 候选池筛选逻辑从 AND 改为 OR-2（任意 2 条件达标即入池） |
| `_compute_dynamic_thresholds()` | 分位数参数从 config 读取，无需修改代码 |
| `get_lv_rm_scan_range()` | **新增**：独立于候选池的 LV-RM 扫描范围获取方法 |
| `_validate_short_candidate()` | 降级为评分引擎的辅助方法，不再阻断候选池入池 |
| `_validate_long_candidate()` | 同上 |

### 6.2 strategy.py

| 方法/逻辑 | 变更内容 |
|-----------|----------|
| `_execute_cycle()` | 候选池为空时不再休眠，只跳过标准模式/EMM，LV-RM 继续运行 |
| `_check_lv_rm_entries()` | 扫描来源改为 `candidate_pool.get_lv_rm_scan_range()` |
| `_is_sleep_on_empty()` | 移除或修改：候选池为空时不再休眠 |

### 6.3 scoring_engine.py

| 方法/逻辑 | 变更内容 |
|-----------|----------|
| 无变更 | 评分逻辑不变，候选池只负责输出候选币种，评分由评分引擎独立完成 |

---

## 第七部分：风险与注意事项

### 7.1 候选池扩容后可能的风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 候选池币种过多 | 每个小时评分计算量增加 | 评分系统本身有技术分门槛（≥4.0）和总分门槛（≥6.0），自然过滤低质量信号 |
| 波动率不足的币种入池 | 入场后涨幅空间小 | 评分系统中有形态检测和 ATR 止损，空间不足的币种不会触发入场 |
| 误判"无人问津"为"反转机会" | 资金费率低的币种可能长期横盘 | 4h 趋势过滤（一票否决）和 K 线确认机制防止逆势入场 |
| 做空与做多候选池重叠 | 同一币种同时做多和做空 | 评分系统会打分决定方向，且入场决策流有明确的顺序（EMM→半EMM→标准→LV-RM） |

### 7.2 回退方案

如果 V2.5 的候选池扩容导致信号质量下降，可快速回退到 V2.4 配置：

```yaml
# 回退到 V2.4 配置（只需修改分位数）
dynamic_thresholds:
  funding_rate_percentile_short: 0.80
  funding_rate_percentile_long: 0.20
  oi_market_cap_percentile_short: 0.80
  oi_market_cap_percentile_long: 0.20
  ema20_deviation_percentile_short: 0.70
  ema20_deviation_percentile_long: 0.30
```

**无需修改代码**，只需修改 config.yaml 中的分位数参数即可回退。

---

## 第八部分：版本记录

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| V1.0–V1.9 | 2026-06-05/06 | 基础框架搭建与常规优化 |
| V2.0 | 2026-06-25 | 软化技术门槛，新增±15%极端加分 |
| V2.0-C | 2026-06-25 | 新增双轨 EMM 机制 |
| V2.1 | 2026-07-02 | 全面下调 EMM 阈值 |
| V2.2 | 2026-07-02 | 半 EMM：合约分保底3.0 + 总分阈值5.0 |
| V2.3 | 2026-07-15 | 固定阈值 → 动态相对阈值 |
| V2.4 | 2026-07-30 | 新增低波动反转模块（LV-RM），三轨并行 |
| **V2.5** | **2026-07-31** | **候选池扩容：AND→OR-2，分位数降为50分位，LV-RM独立** |

---

**核心原则**：
**入口宽松（候选池粗筛），出口严格（评分系统精筛）。候选池不再堵死策略的入口，让评分系统做最终的入场决策。**