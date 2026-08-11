# 混合反转策略 HRS V2.5（最终版）
## 候选池扩容（OR-2 + 50分位）+ LV-RM 独立

---

## 第一部分：V2.4 → V2.5 修改点清单

| 修改项 | V2.4（旧值） | V2.5（新值） | 修改理由 |
|--------|-------------|-------------|----------|
| **候选池筛选逻辑** | 4 条件 AND（全部必须满足） | **4 条件中满足任意 2 个即可入池（OR-2）** | V2.4 候选池长期为空，策略停摆 |
| **动态阈值分位数** | 做空 80 分位 / 做多 20 分位 | **做空/做多统一 50 分位（中位数）** | 高分位数门槛过高，候选池几乎无币种达标 |
| **涨跌幅阈值** | 做空 ≥ 8%，做多 ≤ -6%（文档曾写 12%/10%，已与代码对齐） | **对称统一为 8%（绝对值）** | 消除文档与代码不一致，对称逻辑更清晰 |
| **涨跌幅角色** | 硬性条件（必须满足） | **作为"任意 2 条件"之一，非硬性** | 低波动市场下涨跌幅不达标但有价值的币种也能入池 |
| **LV-RM 扫描范围** | 候选池落选币种中筛选 | **全市场流动性币种中直接筛选，完全独立** | 候选池为空时 LV-RM 也停摆 |
| **LV-RM 涨跌幅过滤** | \|涨跌幅\| < 5% | **\|涨跌幅\| < 8%** | 与价格变化阈值统一，扩大扫描范围 |
| **候选池为空处理** | 整个策略休眠到下次扫描 | **标准模式/EMM 跳过，LV-RM 继续运行；候选池扫描频率指数退避** | 保持 LV-RM 独立运行，同时节省计算资源 |
| **版本号** | V2.4 | **V2.5** | 候选池逻辑重构 + LV-RM 独立，架构级变更 |

---

## 第二部分：V2.4 候选池为什么长期为空

### 2.1 根因公式

```
V2.4 做多候选池 = 涨跌幅≤-6% AND 费率≤20分位 AND OI/市值≤20分位 AND EMA偏离≤30分位
V2.4 做空候选池 = 涨跌幅≥8%  AND 费率≥80分位 AND OI/市值≥80分位 AND EMA偏离≥70分位
```

**4 个 AND 条件叠加**，每个条件筛选掉大部分币种，最终交集几乎为空：

| 维度 | 做多要求 | 市场实际 | 结果 |
|------|---------|----------|------|
| 涨跌幅（固定） | ≤ -6% | 低波动市场，大部分在 -3%~+3% | 大部分不达标 |
| 费率（动态） | ≤ 市场 20 分位 | 正费率为主，BOTTOM 20% 约 -3.38% | 极少数达标 |
| OI/市值（动态） | ≤ 市场 20 分位 | 极严格 | 极少数达标 |
| EMA20 偏离（动态） | ≤ 市场 30 分位 | 约 -0.58% | 极少数达标 |

**实际数据验证**（2026-07-31 扫描，67 个样本）：做空 0 个 + 做多 0 个 = **0 个候选币种**。

### 2.2 问题根因

> **候选池承担了"评分系统"的职责，条件过于严格，导致入口被堵死，整个策略（EMM、半 EMM、标准模式、LV-RM）全部停摆。**

---

## 第三部分：V2.5 核心设计

### 3.1 设计哲学变更

```
V2.4（旧）：候选池 = 精筛入口 → 评分系统做二次精筛（重复筛选）
V2.5（新）：候选池 = 粗筛入口 → 评分系统做唯一精筛（职责单一）
```

**候选池只负责"粗筛"**，把明显不合适的币种排除（流动性不足、稳定币、杠杆代币），**让评分系统来决定最终是否入场**。

### 3.2 候选池筛选逻辑（OR-2）

**4 个条件维度**：

| 编号 | 维度 | 做空达标条件 | 做多达标条件 | 来源 |
|------|------|-------------|-------------|------|
| A | **24h 涨跌幅** | ≥ 8% | ≤ -8% | 固定阈值 |
| B | **资金费率（年化）** | ≥ 市场 **50 分位** | ≤ 市场 **50 分位** | 动态阈值（中位数） |
| C | **OI / 市值比** | ≥ 市场 **50 分位** | ≤ 市场 **50 分位** | 动态阈值（中位数） |
| D | **EMA20(4h) 偏离** | ≥ 市场 **50 分位** | ≤ 市场 **50 分位** | 动态阈值（中位数） |

**入池规则**：
- 4 个条件中，**任意 2 个及以上达标**（不限组合），即可进入候选池
- 进入候选池后，由评分系统按标准流程评分（总分 ≥ 6.0 才入场）
- 做空和做多独立计算，一个币种可同时进入双向候选池

### 3.3 动态阈值调整：分位 → 50 分位（中位数）

| 参数 | V2.4 | V2.5 | 调整理由 |
|------|------|------|----------|
| 费率做空分位数 | 80 分位 | **50 分位** | 高分位数导致候选池为空 |
| 费率做多分位数 | 20 分位 | **50 分位** | 低分位数导致候选池为空 |
| OI/市值做空分位数 | 80 分位 | **50 分位** | 同上 |
| OI/市值做多分位数 | 20 分位 | **50 分位** | 同上 |
| EMA20 做空分位数 | 70 分位 | **50 分位** | 同上 |
| EMA20 做多分位数 | 30 分位 | **50 分位** | 同上 |

**EMM 分位数保持不变**（EMM 需要极端条件）：
| 参数 | 值 | 说明 |
|------|-----|------|
| 费率做多 EMM | 10 分位 | 不变 |
| 费率做空 EMM | 90 分位 | 不变 |
| OI/市值 EMM | 90 分位 | 不变 |

### 3.4 预期候选池扩容效果

| 指标 | V2.4（AND） | V2.5（OR-2 + 50 分位） | 估算依据 |
|------|-----------|----------------------|----------|
| 做多候选 | **0 个** | **6-10 个** | 流动性币种 ~46 个，各维度独立达标率 ~50%，OR-2 组合期望 ≈ 46 × (1 - 0.5^4 - 4×0.5^4) ≈ 46 × 0.6875 ≈ 31.6，但涨跌幅维度受市场状态影响，保守估计 6-10 个 |
| 做空候选 | **0 个** | **5-8 个** | 做空通常比做多更难触发，保守估计 |
| 总计 | **0 个** | **11-18 个** | — |
| 做多 vs 做空 | — | **做多 > 做空** | 正常，下跌币种通常多于上涨币种 |

---

## 第四部分：LV-RM 独立化

### 4.1 V2.4 的问题链

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
1. 获取所有流动性达标币种（24h 成交额 ≥ 5000 万 U，OI ≥ 1000 万 U）→ 约 46 个
2. 过滤出 |24h 涨跌幅| < 8% 的币种 → 约 30-35 个
3. 对这些币种逐个检查 LV-RM 触发条件（布林带触轨、RSI、费率、K 线确认、4h 趋势过滤）

### 4.3 实现代码

```python
async def get_lv_rm_scan_range(self) -> List[str]:
    """
    V2.5: 获取 LV-RM 独立扫描范围
    从全市场流动性币种中筛选 |涨跌幅| < 8% 的币种
    """
    # 从配置读取阈值，禁止硬编码
    lv_rm_config = self.config.get("lv_rm", {}).get("scan", {})
    max_price_change = lv_rm_config.get("max_price_change_24h", 0.08)
    
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
        
        # 涨跌幅过滤（从配置读取）
        price_change = float(ticker.get("priceChangePercent", 0))
        if abs(price_change) < max_price_change * 100:
            lv_rm_symbols.append(symbol)
    
    return lv_rm_symbols
```

### 4.4 候选池为空时的处理（双轨策略）

| 层面 | 策略周期（主循环） | 候选池扫描 |
|------|------------------|-----------|
| **V2.4 行为** | 整个策略休眠到下次扫描 | 每天 08:05 扫描一次 |
| **V2.5 行为** | **标准模式/EMM 跳过，LV-RM 继续运行** | **指数退避：空池 → 2h → 4h → 8h → 24h 上限** |
| **目的** | 保持 LV-RM 独立运行 | 节省计算资源 |

```python
# V2.5 主循环逻辑
if not self.candidate_pool.has_candidates():
    # 标准模式/EMM 跳过，但 LV-RM 继续
    await self._run_lv_rm_only_cycle()
else:
    await self._run_full_cycle()
```

```python
# V2.5 候选池扫描退避逻辑（从配置读取阈值，禁止硬编码）
def check_candidate_pool_with_backoff(self):
    # 从配置读取退避参数
    backoff_config = self.config.get("candidate_pool", {}).get("empty_backoff", {})
    max_empty_count = backoff_config.get("max_empty_count_before_backoff", 3)
    backoff_hours = backoff_config.get("backoff_hours", 2)
    normal_interval = backoff_config.get("normal_interval_minutes", 60)
    
    candidates = check_candidate_pool()
    
    if len(candidates) == 0:
        empty_count = get_empty_count()
        if empty_count >= max_empty_count:
            # 连续 max_empty_count 次空池 → 退避 backoff_hours 小时
            set_backoff_mode(True)
            schedule_next_check(hours=backoff_hours)
        else:
            schedule_next_check(minutes=normal_interval)
    else:
        # 候选池非空，重置空池计数
        reset_empty_count()
        schedule_next_check(minutes=normal_interval)
```

---

## 第五部分：配置变更

### 5.1 config.yaml 完整变更

```yaml
# V2.5 候选池配置变更
candidate_pool:
  # 新增：候选池逻辑模式
  logic: "or_any_2"               # V2.5 新增：or_any_2 = 满足任意2条件
  
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

  # V2.5 新增：候选池空池退避逻辑
  empty_backoff:
    enabled: true
    max_empty_count_before_backoff: 3  # 连续空池次数达到此值后触发退避
    backoff_hours: 2                   # 退避初始休眠时长
    max_backoff_hours: 24              # 退避最长休眠时长
    normal_interval_minutes: 60        # 正常（非退避）扫描间隔
```

### 5.2 配置项完整变更清单

| 配置项 | 路径 | V2.4 | V2.5 |
|--------|------|------|------|
| 候选池逻辑模式 | candidate_pool.logic | 不存在 | **"or_any_2"** |
| 做多涨跌幅阈值 | candidate_pool.long.price_change_24h | -0.06 | **-0.08** |
| 费率做空分位数 | candidate_pool.dynamic_thresholds.funding_rate_percentile_short | 0.80 | **0.50** |
| 费率做多分位数 | candidate_pool.dynamic_thresholds.funding_rate_percentile_long | 0.20 | **0.50** |
| OI/市值做空分位数 | candidate_pool.dynamic_thresholds.oi_market_cap_percentile_short | 0.80 | **0.50** |
| OI/市值做多分位数 | candidate_pool.dynamic_thresholds.oi_market_cap_percentile_long | 0.20 | **0.50** |
| EMA20 做空分位数 | candidate_pool.dynamic_thresholds.ema20_deviation_percentile_short | 0.70 | **0.50** |
| EMA20 做多分位数 | candidate_pool.dynamic_thresholds.ema20_deviation_percentile_long | 0.30 | **0.50** |
| LV-RM 扫描来源 | lv_rm.scan.scan_source | eliminated_candidates | **all_liquid** |
| LV-RM 涨跌幅阈值 | lv_rm.scan.max_price_change_24h | 0.05 | **0.08** |
| 空池退避启停 | candidate_pool.empty_backoff.enabled | 不存在 | **true** |
| 退避触发次数 | candidate_pool.empty_backoff.max_empty_count_before_backoff | 不存在 | **3** |
| 退避初始休眠 | candidate_pool.empty_backoff.backoff_hours | 不存在 | **2** |
| 退避最大休眠 | candidate_pool.empty_backoff.max_backoff_hours | 不存在 | **24** |
| 正常扫描间隔 | candidate_pool.empty_backoff.normal_interval_minutes | 不存在 | **60** |

---

## 第六部分：代码变更清单

### 6.1 candidate_pool.py

| 方法/逻辑 | 变更内容 |
|-----------|----------|
| `scan_and_update()` | 筛选逻辑从 AND 改为 OR-2：遍历所有流动性币种，对每个币种分别评估 4 个维度的达标情况，计数 ≥ 2 则入池 |
| `_validate_short_candidate()` | 重构为返回达标条件数（int 0-4），而非 boolean |
| `_validate_long_candidate()` | 同上 |
| `get_lv_rm_scan_range()` | **新增**：独立于候选池的 LV-RM 扫描范围获取方法 |

### 6.2 strategy.py

| 方法/逻辑 | 变更内容 |
|-----------|----------|
| `_execute_cycle()` | 候选池为空时不再休眠，仅跳过标准模式/EMM，LV-RM 继续运行 |
| `_check_lv_rm_entries()` | 扫描来源改为 `candidate_pool.get_lv_rm_scan_range()` |
| `_is_sleep_on_empty()` | 移除或修改为仅影响标准模式，不影响 LV-RM |

### 6.3 scoring_engine.py

**无变更**。评分逻辑不变，候选池只负责输出候选币种，评分由评分引擎独立完成。

### 6.4 database.py

| 新增字段 | 表 | 类型 | 用途 |
|----------|-----|------|------|
| `match_conditions` | hrs_candidate_pool | TEXT | 记录入池时满足的条件组合（如 "A+B", "B+C"） |
| `scan_source` | hrs_lv_rm_log | TEXT DEFAULT 'full_market' | LV-RM 扫描来源，固定值 'full_market' |
| `candidate_match` | hrs_orders | TEXT | 记录该订单入池时满足的条件组合 |

---

## 第七部分：风险分析

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 候选池币种过多（>20） | 低 | 中 | 评分系统有 6.0 总分门槛 + 4.0 技术分门槛，自然过滤低质量信号 |
| 低质量信号通过评分系统 | 中 | 高 | 总分 ≥ 6.0 门槛不变，技术分 ≥ 4.0 不变，EMM 阈值不变，4h 趋势过滤一票否决 |
| LV-RM 全市场扫描计算量增加 | 低 | 低 | 流动性达标币种约 46 个，每小时扫描一次，并发限制 10，现代硬件可轻松支撑 |
| 做多/做空候选池方向失衡 | 中 | 低 | 方向偏离是市场结构问题，非策略逻辑缺陷，可接受 |
| 同一币种双向候选 | 高 | 低 | 冲突解决逻辑已存在，评分系统决定最终方向，决策流顺序（EMM→半EMM→标准→LV-RM）确保唯一性 |
| 50 分位导致候选池过大 | 中 | 中 | 可通过逐步降低分位数调整（60 → 50 → 40），无需代码修改 |

---

## 第八部分：回退方案

| 触发条件 | 回退动作 | 回退目标 |
|----------|----------|----------|
| V2.5 上线后 3 天内候选池仍 < 3 个 | 将分位数从 50 降至 30（做多）/ 70（做空） | 无需代码修改 |
| V2.5 上线后 7 天内胜率低于 50% | 恢复分位数到 V2.4 值（80/20/70/30），OR-2 近似等效 AND | 无需代码修改 |
| LV-RM 触发频率过高（>3 次/天） | 将 max_price_change_24h 从 8% 降至 5% | 无需代码修改 |
| 单日总开仓 > 5 个（风控熔断） | 临时将 LV-RM 额度从 2 降至 1，暂停标准模式 24 小时 | 手动干预 |

**核心优势**：所有 V2.5 配置变更均可通过修改 config.yaml 回退，**无需代码回滚**。

---

## 第九部分：版本记录

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| V1.0–V1.9 | 2026-06-05/06 | 基础框架搭建与常规优化 |
| V2.0 | 2026-06-25 | 软化技术门槛，新增±15%极端加分 |
| V2.0-C | 2026-06-25 | 新增双轨 EMM 机制 |
| V2.1 | 2026-07-02 | 全面下调 EMM 阈值 |
| V2.2 | 2026-07-02 | 半 EMM：合约分保底 3.0 + 总分阈值 5.0 |
| V2.3 | 2026-07-15 | 固定阈值 → 动态相对阈值 |
| V2.4 | 2026-07-30 | 新增低波动反转模块（LV-RM），三轨并行 |
| **V2.5（最终版）** | **2026-08-03** | **候选池扩容（AND→OR-2 + 50 分位）+ LV-RM 独立化 + 空池退避 + 数据库字段补充** |

---

**核心原则**：
**入口宽松（候选池粗筛），出口严格（评分系统精筛）。候选池不再堵死策略的入口，让评分系统做最终的入场决策。LV-RM 独立运转，三轨并行，纪律优先。**