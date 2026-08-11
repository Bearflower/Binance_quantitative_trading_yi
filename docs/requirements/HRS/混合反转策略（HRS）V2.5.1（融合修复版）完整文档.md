以下是 **HRS V2.5.1（融合修复版）** 完整文档。

---
## Hybrid Reversal Strategy - Candidate Pool Expansion (HRS-CPE) v2.5.1

> **V2.5.1 定位**：基于 V2.5 候选池扩容逻辑的**问题修复与完整性增强版**，修复了硬编码、文档与代码不一致、预期数值不统一等 ERROR/WARNING 级别问题，并补充了风险分析、回退方案、候选池空休眠、代码变更清单等缺失章节。


## 第一部分：V2.5 → V2.5.1 修复与补充清单

| 严重级别 | 位置 | 问题 | 修复方案 |
|----------|------|------|----------|
| **ERROR** | §4.3 代码示例（用户方案） | `abs(price_change) < 8.0` 硬编码 | **已修复**：改为从配置 `lv_rm.max_abs_change_pct` 读取 |
| **WARNING** | §2.2 表格 | V2.4 旧值写 12%/10% 与实际代码 8%/6% 不符 | **已修复**：统一为 8%/6%，以代码为准 |
| **WARNING** | §1.1 / §9 预期数量 | 做多 8-12 vs 8-15 自身不一致 | **已修复**：统一为做多 6-10 / 做空 5-8 |
| **MISSING** | 全局 | 代码变更清单缺失 | **已补充**：新增 §12 |
| **MISSING** | 全局 | 风险分析缺失 | **已补充**：新增 §10 |
| **MISSING** | 全局 | 回退方案缺失 | **已补充**：新增 §11 |
| **MISSING** | §7 数据库 | 字段变更清单不完整 | **已补充**：完整 SQL 变更清单 |
| **MISSING** | §2.2 | 候选池为空休眠处理未提及 | **已补充**：新增“步骤 4：休眠处理” |
| **WARNING** | 全局 | 做多 vs 做空方向预期不均衡 | **已说明**：做多 > 做空是市场结构所致（下跌币种通常多于上涨币种） |


## 第二部分：V2.5.1 设计基础（继承 V2.5）

| 核心变更 | V2.4（旧值） | V2.5.1（新值） | 设计理由 |
|----------|-------------|---------------|----------|
| 候选池逻辑 | 多重 AND | **满足任意 2 个条件入池（4 维度任意组合）** | AND 条件过多导致候选池长期为空 |
| 涨跌幅角色 | 硬性条件 | **作为 4 个维度之一，非硬性** | 低波动市场下涨跌幅不达标但有价值的币种也能入池 |
| 分位数阈值 | 20/70/80 分位 | **50 分位（中位数）** | 降低门槛，与“宽松 OR”协同 |
| 涨跌幅阈值 | 12%/10% | **8%（做空≥8%，做多≤-8%）** | 统一文档与代码 |
| LV-RM 扫描范围 | 候选池落选 + \|涨跌幅\|<5% | **全市场流动性币种直接筛选 \|涨跌幅\|<8%（独立于候选池）** | 候选池为空时 LV-RM 仍可正常工作 |
| 候选池空休眠 | 无 | **连续 3 次空池→指数退避休眠（2h→4h→8h）** | 节省计算资源 |


## 第三部分：HRS V2.5.1 完整文档


## 1. 策略概述

- **目标**：针对币安永续合约中**流动性充足的存量币种**，捕捉**暴涨后的做空机会**与**暴跌后的做多机会**，覆盖全市场状态。
- **核心理念**：采用 **“三轨并行 + 多时间框架”** 机制：

  - **轨道 A（标准反转）**：利用“衰竭形态（三次探底/冲顶）”入场，适用于中高波动趋势市。
  - **轨道 B（极端市场模块 EMM）**：利用“价格极端偏离 + 费率极端 + 仓位拥挤”三重共振入场，适用于高波动单边市。
  - **轨道 C（低波动反转模块 LV-RM）**：利用 **1小时** 布林带触轨 + RSI 超买超卖作为**执行信号**，专门捕捉低波动震荡市中的均值回归机会（V2.5.1：**完全独立于候选池**）。

### 1.1 V2.5.1 核心变化

V2.5.1 在 V2.5 候选池扩容基础上，修复了硬编码、文档与代码不一致等 ERROR 级别问题，并补充了风险分析、回退方案、候选池空休眠、代码变更清单等完整性章节。

### 1.2 预期候选池规模（正式版）

| 指标 | 预期值 | 推算依据 |
|------|--------|----------|
| 做多候选池 | **6-10 个** | 流动性达标币种约 46 个，各维度达标率约 50%，任意 2 条件组合的期望值约 10-15 个，保守取 6-10 |
| 做空候选池 | **5-8 个** | 做空方向触发条件通常比做多更严苛，保守取 5-8 |
| 总计 | **11-18 个** | — |
| 方向特征 | **做多 > 做空** | 正常现象，下跌币种通常多于上涨币种 |


## 2. 币种池与预筛选

### 2.1 币种池范围

#### 通用流动性门槛（所有模块共用）
- 24h 成交额 ≥ **5000 万 USDT**
- 当前持仓量 OI ≥ **1000 万 USDT**

#### LV-RM 扫描范围（V2.5.1：完全独立）
LV-RM **不再依赖候选池落选币种**，改为直接从全市场流动性币种中筛选：
1. 所有满足流动性门槛的币种
2. 且 **|24h 涨跌幅| < 8%**（配置项：`lv_rm.max_abs_change_pct`）

> **代码实现**：
> ```python
> # ✅ 正确：从配置读取，避免硬编码
> max_abs_change = config.get("lv_rm", {}).get("max_abs_change_pct", 0.08)
> if abs(price_change_24h) < max_abs_change:
>     # 进入 LV-RM 扫描
> ```

### 2.2 每日动态筛选（V2.5.1 候选池逻辑）

每天早上 **8:05 (UTC+8)** 执行全市场扫描，动态计算当日阈值。

#### 步骤 1：采集全市场数据
获取所有 USDT 永续合约的以下数据：24h 涨跌幅、资金费率（年化）、OI / 市值比、价格偏离 EMA20（4h）。

#### 步骤 2：计算动态阈值（V2.5.1：中位数）

| 筛选维度 | 做空候选条件 | 做多候选条件 | V2.4 旧值（文档） | V2.4 实际代码值 |
|----------|-------------|-------------|------------------|-----------------|
| **24h 涨跌幅** | ≥ **8%** | ≤ **-8%** | ≥12% / ≤-10% | ≥8% / ≤-6% |
| **资金费率（年化）** | ≥ 市场 **50分位数** | ≤ 市场 **50分位数** | 80分位 / 20分位 | 80分位 / 20分位 |
| **OI / 市值比** | ≥ 市场 **50分位数** | ≤ 市场 **50分位数** | 80分位 / 20分位 | 80分位 / 20分位 |
| **EMA20(4h)偏离** | ≥ 市场 **50分位数** | ≤ 市场 **50分位数** | 70分位 / 30分位 | 70分位 / 30分位 |

> **说明**：V2.5.1 统一以 **实际代码值 8%/6%** 为基础，并将做多阈值对称调整为 8%，使做多与做空逻辑一致。

#### 步骤 3：候选池入池规则（V2.5.1 核心）

**入池规则**：满足以下 4 个条件中 **任意 2 个** 即可进入候选池（不限组合）。

**做多候选池条件**：
| 编号 | 条件 | 阈值 |
|------|------|------|
| A | 24h 涨跌幅 | ≤ -8% |
| B | 资金费率（年化） | ≤ 市场 50 分位数 |
| C | OI / 市值比 | ≤ 市场 50 分位数 |
| D | EMA20(4h) 偏离 | ≤ 市场 50 分位数 |

**做空候选池条件**：
| 编号 | 条件 | 阈值 |
|------|------|------|
| A | 24h 涨跌幅 | ≥ 8% |
| B | 资金费率（年化） | ≥ 市场 50 分位数 |
| C | OI / 市值比 | ≥ 市场 50 分位数 |
| D | EMA20(4h) 偏离 | ≥ 市场 50 分位数 |

> **任意组合示例**：AB、AC、AD、BC、BD、CD 共 6 种组合方式，全放开，不限制组合类型。

#### 步骤 4：候选池为空时的休眠处理（V2.5.1 新增）

若当日候选池为空（0 个币种），执行以下策略：

| 连续空池次数 | 处理方式 | 说明 |
|-------------|----------|------|
| **第 1 次** | 等待下一个整点（1 小时后）重新扫描 | 正常等待 |
| **第 2 次** | 等待下一个整点（1 小时后）重新扫描 | 正常等待 |
| **连续 3 次** | 进入**休眠模式**：2 小时后重试 | 指数退避开始 |
| **连续 4 次** | 休眠 4 小时后重试 | 指数退避 |
| **连续 5 次及以上** | 休眠 8 小时后重试 | 最大间隔 8 小时 |
| **候选池非空后** | 重置空池计数，恢复正常每小时扫描 | 恢复正常 |

> **休眠期间**：止损止盈、移动止盈继续运行；新开仓暂停。

**配置常量**：
```python
CONFIG = {
    "candidate_pool": {
        "logic": "or_any_2",
        "change_threshold": 0.08,          # 做空≥8%，做多≤-8%
        "rate_percentile": 0.50,           # 中位数
        "oi_percentile": 0.50,             # 中位数
        "ema_percentile": 0.50,            # 中位数
        "empty_backoff": {
            "max_consecutive_empty": 3,    # 触发休眠的空池次数
            "base_delay_hours": 2,         # 基础休眠时长
            "max_delay_hours": 8,          # 最大休眠时长
        }
    },
    "emm": {
        "rate_percentile": 0.10,           # 做多 ≤10分位 / 做空 ≥90分位
        "oi_percentile": 0.90,
    },
    "lv_rm": {
        "scan_mode": "full_market",        # 全市场，不依赖候选池
        "max_abs_change_pct": 0.08,        # |涨跌幅| < 8%
    }
}
```


## 3. 信号评分系统（三轨并行）

### 3.1 合约数据（权重 25%）—— OI / 市值比

| OI / 市值比 | 做空得分 | 做多得分 |
|-------------|----------|----------|
| > 0.25 | 10 | 0 |
| 0.20 – 0.25 | 8 | 1 |
| 0.15 – 0.20 | 6 | 3 |
| 0.10 – 0.15 | 4 | 5 |
| 0.05 – 0.10 | 2 | 7 |
| < 0.05 | 0 | 10 |

### 3.2 情绪面（权重 30%）—— 资金费率

| 年化费率 | 做空得分 | 做多得分 |
|----------|----------|----------|
| > 150% | 10 | 0 |
| 100% – 150% | 8 | 2 |
| 50% – 100% | 6 | 4 |
| 0% – 50% | 3 | 6 |
| -20% – 0% | 1 | 8 |
| < -20% | 0 | 10 |


### 3.3 轨道 A：标准模式（权重 45%）

| 条件 | 分值 | 量化标准 |
|------|------|----------|
| **三次冲顶/探底** | 0–4 | 最近 **5根 1h** K线高点/低点依次降低/抬高（差≥0.2%），或同一水平受阻/支撑≥3次 |
| **长影线** | 0–3 | 上/下影线 ≥ 实体 × 2 |
| **放量滞涨/止跌** | 0–3 | 成交量 ≥ 前5根均量 × 1.5 |
| **极端行情加分** | +1.5 | 做多：跌幅≥15%；做空：涨幅≥15% |
| **标准技术总分** | 0–10 | 三项相加 + 加分，封顶10分 |

**入场条件**：标准总分 ≥ 6.0，且技术分 ≥ 4.0，三次形态基础分 ≥ 1.0。


### 3.4 轨道 B：极端市场模块 EMM（权重 45%）

**EMM 阈值保持不变**（仍使用极端分位数）：
- 做多：跌幅≤-15% + 费率≤10分位 + OI/市值≥90分位
- 做空：涨幅≥+15% + 费率≥90分位 + OI/市值≥90分位

**EMM 技术得分 = 5.0（固定值）**，入场条件：EMM 总分 ≥ 6.0。


### 3.5 轨道 C：低波动反转模块 LV-RM（权重 45%）

#### 3.5.1 扫描范围（V2.5.1：完全独立）
LV-RM **不再依赖候选池落选币种**，直接从全市场流动性币种中筛选：
1. 所有满足流动性门槛的币种（24h 成交额 ≥5000万U，OI ≥1000万U）
2. 且 **|24h 涨跌幅| < 8%**（从配置 `lv_rm.max_abs_change_pct` 读取）

#### 3.5.2 LV-RM 触发条件

| 序号 | 条件 | 时间框架 | 做多阈值 | 做空阈值 |
|------|------|----------|----------|----------|
| ① | **布林带位置** | 1h | 价格 ≤ 下轨 | 价格 ≥ 上轨 |
| ② | **RSI(14)** | 1h | ≤ 30 | ≥ 70 |
| ③ | **资金费率** | 实时 | ≤ -10% | ≥ 30% |
| ④ | **K线确认** | 1h | 收盘价 > 前低 | 收盘价 < 前高 |
| ⑤ | **趋势过滤** | 4h | 价格>EMA20且≥EMA20×0.97 | 价格<EMA20且≤EMA20×1.03 |

#### 3.5.3 LV 技术评分
| 条件 | 分值 | 量化标准 |
|------|------|----------|
| **布林带触轨** | 0–4 | 基础 2 分 + 比例加分，封顶 4 分 |
| **RSI 达标** | 0–3 | 达标得 3 分，否则 0 分 |
| **LV 技术总分** | **0–7** | 两项相加 |

**入场条件**：LV总分 ≥ 6.0，且 LV技术分 ≥ 4.0。


## 4. 进场逻辑与执行（四步决策流）

```
1. 检查完整 EMM（3/3 条件）→ 入场 ✅
2. 检查半 EMM（2/3 条件）→ 入场 ✅
3. 检查标准模式（轨道 A）→ 入场 ✅
4. 检查 LV-RM（轨道 C，全市场独立扫描）→ 入场 ✅
5. 均不满足 → 放弃 ❌
```


## 5. 止损与止盈

| 模式 | ATR止损 | 紧急止损 | 最小止损 | 第一目标 | 第二目标 | 移动止盈 | 时间止损 |
|------|---------|---------|---------|---------|---------|---------|----------|
| 标准/EMM | 2.5×ATR | 1.5% / 1.015 | 5% | 1.5×ATR | 3.5×ATR | 1.5×ATR | 72h |
| LV-RM | **1.5×ATR** | **1.0% / 1.01** | **3%** | **1.0×ATR** | **2.0×ATR** | **1.0×ATR** | **48h** |


## 6. 仓位管理与风控

| 规则 | 设定 |
|------|------|
| 每笔最大亏损 | 账户总资金 **2%** |
| 杠杆 | ≤ **2倍** |
| 标准模式 + EMM 单日最多开仓 | **3 个币种** |
| LV-RM 单日最多开仓 | **2 个币种** |
| 连续亏损暂停 | 连续 3 笔亏损，暂停 **2 天** |
| 最大回撤熔断 | 累计亏损 ≥ **15%**，暂停 **一周** |


## 7. 数据库变更（V2.5.1 完整清单）

```sql
-- ============================================================
-- HRS V2.5.1 数据库变更清单
-- ============================================================

-- 1. 候选池表：新增匹配记录字段
ALTER TABLE hrs_candidate_pool 
ADD COLUMN match_conditions TEXT;  -- 记录满足的条件组合，如 "A+B", "B+C", "A+D"

-- 2. LV-RM 日志表：新增扫描来源字段
ALTER TABLE hrs_lv_rm_log 
ADD COLUMN scan_source TEXT DEFAULT 'full_market';  -- 固定值 'full_market'

-- 3. 订单表：新增入池条件组合字段
ALTER TABLE hrs_orders 
ADD COLUMN candidate_match TEXT;  -- 记录该订单入池时满足的条件组合

-- 4. 候选池快照表（新增，用于复盘）
CREATE TABLE hrs_candidate_pool_snapshot (
    snapshot_time INTEGER,        -- 快照时间
    symbol TEXT,                  -- 交易对
    direction TEXT,               -- 'short' / 'long'
    condition_a BOOLEAN,          -- 涨跌幅达标
    condition_b BOOLEAN,          -- 费率达标
    condition_c BOOLEAN,          -- OI/市值达标
    condition_d BOOLEAN,          -- EMA偏离达标
    match_count INTEGER,          -- 满足条件数量 (0-4)
    matched TEXT,                 -- 满足的组合如 "A+B"
    PRIMARY KEY (snapshot_time, symbol, direction)
);

-- 5. 策略状态表：新增空池计数和休眠状态
ALTER TABLE hrs_meta 
ADD COLUMN consecutive_empty INTEGER DEFAULT 0;  -- 连续空池计数

ALTER TABLE hrs_meta 
ADD COLUMN backoff_until INTEGER DEFAULT 0;      -- 休眠结束时间戳
```


## 8. 配置变更（V2.5.1）

```yaml
# config.yaml V2.5.1

# 候选池配置（核心变更）
candidate_pool:
  logic: "or_any_2"           # 满足任意2个条件
  condition_count: 2          # 需要满足的条件数量
  change_threshold: 0.08      # 涨跌幅阈值（做多≤-8%，做空≥8%）
  rate_percentile: 0.50       # 资金费率中位数
  oi_percentile: 0.50         # OI/市值中位数
  ema_percentile: 0.50        # EMA偏离中位数
  empty_backoff:
    max_consecutive_empty: 3
    base_delay_hours: 2
    max_delay_hours: 8

# EMM 配置（不变）
emm:
  rate_percentile_short: 0.90
  rate_percentile_long: 0.10
  oi_percentile: 0.90

# LV-RM 配置（独立化）
lv_rm:
  scan_mode: "full_market"    # 全市场独立扫描
  max_abs_change_pct: 0.08    # |涨跌幅| < 8%（从配置读取，禁止硬编码）
  # ... 其他 LV-RM 参数
```


## 9. 版本记录

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| V1.0–V2.3 | 2026-06-05 ~ 2026-07-15 | 基础框架到动态阈值 |
| V2.4 | 2026-07-30 | 新增 LV-RM 模块 |
| V2.5 | 2026-07-31 | 候选池扩容（OR 逻辑、50 分位、8%阈值、LV-RM 独立） |
| **V2.5.1** | **2026-08-01** | **问题修复与完整性增强**：① 修复 `abs(price_change)<8.0` 硬编码，改为配置读取；② 修正 V2.4 旧值 12%/10% → 8%/6%；③ 统一预期候选池数量为 6-10/5-8；④ 新增风险分析（§10）；⑤ 新增回退方案（§11）；⑥ 新增代码变更清单（§12）；⑦ 新增候选池为空休眠处理（§2.2 步骤4）；⑧ 完善数据库变更清单（§7） |


## 10. 风险分析

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 候选池过多导致评分系统过载 | 低 | 中 | 单日最多开仓限制仍为 3/2 个，候选池扩容不影响开仓数量上限 |
| 低质量信号通过评分系统 | 中 | 高 | 总分 ≥ 6.0 门槛不变，技术分 ≥ 4.0 不变，EMM 阈值不变 |
| LV-RM 全市场扫描计算量增加 | 低 | 低 | 流动性达标币种约 46 个，每小时扫描一次，现代硬件可轻松支撑 |
| 做多候选池远多于做空候选池 | 中 | 低 | 方向偏离是市场结构问题（下跌币种通常多于上涨币种），非策略逻辑缺陷，可接受 |
| 候选池空休眠期间错过信号 | 低 | 中 | 指数退避最大间隔 8 小时，重新激活后立即扫描，错过窗口有限 |
| V2.5.1 上线后 7 天内胜率低于 45% | 低 | 高 | 触发回退方案 Level 3，回退到 V2.4 |


## 11. 回退方案

### 触发条件与回退动作

| 触发条件 | 回退动作 | 回退目标 |
|----------|----------|----------|
| **Level 0**：V2.5.1 上线后 3 天内候选池仍 < 3 个 | 将分位数从 50 降至 **30（做多）/ 70（做空）** | V2.5.1-hotfix-1 |
| **Level 1**：V2.5.1 上线后 7 天内胜率低于 45%（< 20 笔样本） | 将 LV-RM 单日额度从 2 降至 **1** | V2.5.1-hotfix-2 |
| **Level 2**：V2.5.1 上线后 14 天内胜率低于 40%（< 30 笔样本） | 回退到 V2.4 的 AND 逻辑，仅保留 LV-RM 独立扫描 | V2.5.1-hotfix-3 |
| **Level 3**：LV-RM 触发频率过高（>3 次/天，连续 3 天） | 将 `max_abs_change_pct` 从 8% 降至 **5%**，且临时暂停标准模式 24 小时 | 手动干预 |
| **Level 4**：单日总开仓 > 5 个（风控熔断） | 紧急暂停所有新开仓，仅保留持仓管理，持续 24 小时 | 手动干预 |

### 回退决策节点

| 节点 | 条件 | 决策 |
|------|------|------|
| **第 3 天** | 候选池仍 < 3 个，且无改善趋势 | 执行 Level 0 |
| **第 7 天** | 胜率 < 45%，样本 ≥ 20 笔 | 执行 Level 1，评估至第 10 天 |
| **第 14 天** | 胜率 < 40%，样本 ≥ 30 笔 | 执行 Level 2，回退到 V2.4 |


## 12. 代码变更清单（V2.4 → V2.5.1）

### 配置文件（config.yaml）
```diff
  candidate_pool:
+   logic: "or_any_2"                     # 新增
+   condition_count: 2                    # 新增
-   logic: "and_all"
-   change_threshold_short: 0.12
-   change_threshold_long: -0.10
+   change_threshold: 0.08                # 统一为 8%
-   rate_percentile_short: 0.80
-   rate_percentile_long: 0.20
+   rate_percentile: 0.50                 # 改为中位数
-   oi_percentile_short: 0.80
-   oi_percentile_long: 0.20
+   oi_percentile: 0.50                   # 改为中位数
-   ema_percentile_short: 0.70
-   ema_percentile_long: 0.30
+   ema_percentile: 0.50                  # 改为中位数
+   empty_backoff:                        # 新增
+     max_consecutive_empty: 3
+     base_delay_hours: 2
+     max_delay_hours: 8

  lv_rm:
+   scan_mode: "full_market"              # 新增
-   max_abs_change_pct: 0.05
+   max_abs_change_pct: 0.08              # 放宽至 8%
```

### 候选池模块（candidate_pool.py）
```diff
  def check_candidate_pool(symbols, config):
      conditions = [
          check_change(symbol, config.change_threshold),
          check_rate(symbol, config.rate_percentile),
          check_oi(symbol, config.oi_percentile),
          check_ema(symbol, config.ema_percentile),
      ]
-     if all(conditions):  # AND 逻辑
+     if sum(conditions) >= config.condition_count:  # OR 逻辑，满足任意2个
          return True
      return False
```

### LV-RM 模块（lv_rm.py）
```diff
  def get_scan_symbols(all_symbols, config):
-     eliminated = get_eliminated_symbols()          # 依赖候选池落选
-     return [s for s in eliminated if abs(s.change) < config.max_abs_change_pct]
+     eligible = [s for s in all_symbols if s.oi >= config.oi_threshold and s.volume >= config.volume_threshold]
+     # 从配置读取阈值，禁止硬编码
+     max_change = config.get("max_abs_change_pct", 0.08)
+     return [s for s in eligible if abs(s.change) < max_change]
```


## 13. 附录：V2.5.1 与 V2.5 对照速查

| 项目 | V2.5 | V2.5.1 |
|------|------|--------|
| 涨跌幅阈值（文档） | 做空 8% / 做多 -8% | 做空 8% / 做多 -8%（不变） |
| V2.4 旧值描述 | 12%/10%（错误） | **8%/6%（已修正）** |
| LV-RM 代码 | 硬编码 8.0（ERROR） | **从配置读取（已修复）** |
| 预期候选池数量 | 8-12 / 5-8（不一致） | **6-10 / 5-8（已统一）** |
| 候选池空休眠 | 未提及 | **已补充（指数退避）** |
| 风险分析 | 无 | **已补充 §10** |
| 回退方案 | 无 | **已补充 §11（4 级）** |
| 代码变更清单 | 无 | **已补充 §12** |
| 数据库字段 | 不完整 | **完整 SQL 变更清单 §7** |


## 14. PnL 回写逻辑（2026-08-10 新增）

### 14.1 背景

HRS 策略通过 `TradeLogger` 记录订单到 `trading.trade_records` 表，但平仓后从未调用 `update_realized_pnl` 回写已实现盈亏，导致 `realized_pnl` 字段全部为 NULL。AI 调优系统（ai_tuner）无法正确评估策略表现。

### 14.2 回写场景

PnL 回写在 `_monitor_positions` 方法中，覆盖以下三种平仓场景：

| 场景 | 触发条件 | 出场价 | 平仓数量 | 数据来源 |
|------|---------|--------|---------|---------|
| 时间止损 | `check_time_stop()` 返回 True | `current_price` | `entry_quantity` | 限价平仓（`close_position`） |
| 移动止盈 | `check_trailing_stop()` 返回 True | `current_price` | `remaining_quantity` | 限价平仓（`close_position`） |
| TP1/TP2 全部成交 | `detect_take_profit_fills()` 返回 0 | TP1/TP2 目标价 + 当前价 | 各部分对应数量 | 条件单成交（TP1/TP2）+ 估算 |

### 14.3 回写方式

- **时间止损/移动止盈**：通过 `executor.close_position()` 返回的订单结果（含 `orderId`）精确匹配 `trade_records` 记录
- **TP1/TP2 全部成交**：TP1/TP2 通过条件单成交（`place_conditional_order`，不记录到 `trade_records`），使用 fallback 模式按时间范围匹配

### 14.4 盈亏计算公式

使用 `TradeLogger.calculate_pnl()` 静态方法集中计算：

```python
# LONG: (exit_price - entry_price) * quantity
# SHORT: (entry_price - exit_price) * quantity
```

### 14.5 关键代码位置

| 方法 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `_writeback_pnl` | `strategies/hrs/strategy.py` | ~1234 | 时间止损/移动止盈的 PnL 回写（有 orderId） |
| `_get_actual_pnl_from_binance` | `strategies/hrs/strategy.py` | ~1301 | 从币安 API 获取实际已实现盈亏（2026-08-10 新增） |
| `_calculate_theoretical_total_pnl` | `strategies/hrs/strategy.py` | ~1360 | 理论总 PnL 计算（合并 TP1+TP2+剩余，2026-08-10 新增） |
| `_writeback_pnl_for_full_close` | `strategies/hrs/strategy.py` | ~1465 | 全部平仓时回写完整 PnL（优先 API，降级理论，2026-08-10 重写） |
| `close_position` 返回结果 | `strategies/hrs/executor.py` | ~584 | 限价平仓返回的订单结果 |
| `update_realized_pnl` | `shared/trade_logger.py` | ~221 | 写入 trade_records.realized_pnl |
| `calculate_pnl` | `shared/trade_logger.py` | ~383 | PnL 计算静态方法 |

### 14.6 2026-08-10 PnL 回写修复

#### 14.6.1 修复背景

`trade_records` 表中有 3 个币种（BEATUSDT、AAVEUSDT、CLUSDT）的 `realized_pnl` 与 Binance API 实际数据不一致，每个币种均缺失一条 PnL 记录。根因是 `_writeback_pnl_for_full_close` 方法的旧逻辑存在两个缺陷：

1. **使用理论值而非实际值**：旧逻辑根据策略参数（TP1/TP2 百分比、ATR 倍数）计算理论 PnL，但实际条件单触发时市价单的成交价可能存在滑点，导致理论 PnL 与实际 PnL 不一致。
2. **3 次调用只有第 1 次生效**：旧逻辑分别计算 TP1、TP2、剩余部分 3 个 PnL，每个都调用 `update_realized_pnl`，但由于 `order_id=""` 全部走降级匹配模式，降级匹配的 SQL 条件使用 `LIMIT 1`，导致只有第 1 条记录被更新。

#### 14.6.2 新增方法一：`_get_actual_pnl_from_binance`

从币安 API 获取该持仓的实际已实现盈亏，查询该 symbol 从入场时间到当前时间的全部 `REALIZED_PNL` 记录并汇总。

```python
async def _get_actual_pnl_from_binance(
    self,
    symbol: str,
    entry_time: datetime,
) -> Optional[Decimal]:
```

**流程**：
1. 从 `entry_time` 获取入场时间（UTC），转换为毫秒时间戳作为 `start_time`
2. 以当前时间（毫秒时间戳）作为 `end_time`
3. 调用 `binance_client.get_income_history(start_time, end_time, income_type="REALIZED_PNL")`
4. 从返回结果中过滤出 `symbol` 匹配的记录
5. 将所有 `income` 值求和（使用 Decimal 精度计算）
6. 返回总 PnL，API 失败或未找到记录返回 `None`

#### 14.6.3 新增方法二：`_calculate_theoretical_total_pnl`

当无法从币安 API 获取实际 PnL 时，使用理论计算作为降级方案。将原来的 3 次调用（TP1、TP2、剩余部分）合并为 1 个总值，只写入 1 次。

```python
async def _calculate_theoretical_total_pnl(
    self,
    symbol: str,
    direction: str,
    entry_price: float,
    entry_quantity: float,
    atr: float,
    current_price: float,
    pos: Dict[str, Any],
) -> Optional[Decimal]:
```

**流程**：
1. 从 `pos` 中读取 `target1_reached`、`target2_reached` 状态
2. 从 `position_manager` 获取 TP1/TP2 的百分比和 ATR 倍数配置
3. 计算 TP1 部分 PnL（如果已成交）
4. 计算 TP2 部分 PnL（如果已成交）
5. 计算剩余部分 PnL（使用 `current_price` 作为出场价估算）
6. 返回三个部分的总和

#### 14.6.4 重写方法：`_writeback_pnl_for_full_close`

全部平仓时回写完整 PnL，优先从币安 API 获取实际值，降级到理论计算。

```python
async def _writeback_pnl_for_full_close(
    self,
    symbol: str,
    direction: str,
    entry_price: float,
    entry_quantity: float,
    atr: float,
    current_price: float,
    pos: Dict[str, Any],
) -> None:
```

**执行流程**：
```
1. 检查参数有效性（entry_price > 0, entry_quantity > 0）
2. 检查 trade_logger 是否可用
3. 优先从币安 API 获取实际 PnL
   ├── 从 pos 获取 entry_time（UTC datetime）
   ├── 调用 _get_actual_pnl_from_binance(symbol, entry_time)
   └── 成功 → 使用 API 返回的实际 PnL
4. API 获取失败 → 降级到理论计算
   └── 调用 _calculate_theoretical_total_pnl(...)
5. 一次性写入 trade_records（使用 fallback 模式，order_id=""）
   └── 天然防重复（update_realized_pnl 的降级匹配含 realized_pnl IS NULL 条件）
```

#### 14.6.5 容错机制

| 异常场景 | 处理方式 | 影响 |
|---------|---------|------|
| `get_income_history` 返回空列表 | 降级到理论计算 | 无数据丢失 |
| `get_income_history` 抛出异常 | 降级到理论计算 | 无数据丢失 |
| 账户非 PM 模式（API 不可用） | 降级到理论计算 | 无数据丢失 |
| `update_realized_pnl` 写入失败 | 异常被内部捕获，不影响主流程 | 该笔 PnL 丢失 |
| 求和 Decimal 转换异常 | 异常被捕获，降级到理论计算 | 无数据丢失 |

### 14.7 容错（通用）

- 回写失败不影响主流程（异常被 `try/except` 捕获，仅记录 warning 日志）
- `trade_logger` 未设置时跳过回写
- 参数无效（entry_price ≤ 0 或 exit_price ≤ 0）时跳过回写


**核心原则**：
**候选池是入口，评分系统是出口。入口放宽，出口收紧。LV-RM 独立运转，三轨并行，配置驱动，纪律优先。**

祝交易顺利！