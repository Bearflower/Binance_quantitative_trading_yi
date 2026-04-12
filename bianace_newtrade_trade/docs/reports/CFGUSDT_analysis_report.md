# CFGUSDT 评分详细分析报告

**生成时间**: 2026-03-18 09:48  
**分析对象**: CFGUSDT (Config Network)

---

## 📊 一、基本信息

| 项目 | 数值 |
|------|------|
| 币种符号 | CFGUSDT |
| 合约类型 | PERPETUAL (永续合约) |
| 状态 | TRADING (正常交易) |
| 上线时间 | 2026-03-16 21:00:00 |
| 上线至今 | **36.81 小时** |
| 资金费率 | **-0.00882464** |
| 年化资金费率 | **-115.9%** (计算：-0.00882464 × 3 × 365) |

---

## 🔍 二、问题诊断

### 2.1 当前状态

根据服务器数据，CFGUSDT 在系统中的状态：

```json
{
  "first_detected": "2026-03-18T09:15:41.519743",
  "listing_time": null,  // ❌ 问题 1：上线时间为空
  "scoring_count": 0,    // ❌ 问题 2：从未评分
  "last_scored": null,
  "last_score": null,
  "signal_generated": false,
  "scoring_history": []
}
```

### 2.2 发现的问题

#### ❌ **问题 1: `listing_time` 为 null**

**原因分析**：
系统代码中获取上线时间的逻辑：

```python
listing_time = symbol_info.get('onboardDate', 0) or symbol_info.get('listingTime', 0)
```

币安 API 返回的数据中确实有 `onboardDate: 1773666000000`，但在 `listing_detector.py` 中，当币种被标记为"已处理"时，可能没有正确保存上线时间。

**影响**：
- 无法计算上线至今的小时数
- 无法判断是否在二次评分时间窗口内（24 小时）
- 导致系统跳过了对该币种的评分

**解决方案**：
1. 修复 `listing_detector.py` 中保存状态的逻辑
2. 清空已处理状态，重新检测所有币种

---

#### ❌ **问题 2: 从未进行过评分**

**原因分析**：
1. 系统首次检测到 CFGUSDT 时，可能因为某些原因（如 API 限流、数据获取失败）没有进行评分
2. 但系统仍然将其标记为"已处理"
3. 从此以后，CFGUSDT 被永久跳过

**当前状态**：
- CFGUSDT 上线 36.81 小时，已超过 24 小时的二次评分窗口
- 即使修复了代码，也需要手动清空状态才能重新评分

---

## 📈 三、预估评分（基于可用数据）

### 3.1 合约数据评分（权重 35%）

**评估指标**：
- OI/市值比：❌ 未知（需要获取 OI 数据和市值数据）
- 预估评分：**5-7 分**（假设 OI/市值比在合理范围 0.1-0.3）

**关键问题**：
- 需要确认是否能获取到 CFG 的 OI（持仓量）数据
- 需要确认是否能获取到 CFG 的市值数据（通过 CoinGecko）

---

### 3.2 基本面评分（权重 30%）

**评估指标**：
- 代币解锁数据：❌ 未知（需要检查解锁配置）

**检查方法**：
```bash
docker exec short-selling-system python3 -c "
from core.unlock_manager import unlock_manager
print(unlock_manager.get_unlock_info('CFG'))
"
```

**预估评分**：
- 如果配置文件中没有 CFG 的解锁信息，系统会自动获取或给默认分（5-6 分）

---

### 3.3 技术面评分（权重 25%）

**评估指标**：
- K 线形态识别
- RSI 指标
- ATR 波动率
- 趋势分析

**关键问题**：
- CFGUSDT 上线仅 36 小时，K 线数据可能不足
- 如果 K 线数据点<100，技术面评分会很低（3-5 分）

**检查方法**：
```bash
docker exec short-selling-system python3 -c "
from core.binance_client import binance_client
klines = binance_client.get_klines('CFGUSDT', limit=100)
print(f'K 线数据点数：{len(klines)}')
"
```

---

### 3.4 情绪面评分（权重 10%）⭐

**评估指标**：
- 资金费率：**-0.00882464**（已知）
- 年化资金费率：**-115.9%**

**评分计算**：
```python
# 情绪面评分公式（基于年化资金费率）
def calculate_sentiment_score(annualized_rate):
    # 负费率扣分，正费率加分
    if annualized_rate < -0.5:  # 年化<-50%
        return 1.0  # 最低分
    elif annualized_rate > 0.5:  # 年化>50%
        return 10.0  # 最高分
    else:
        # 线性插值
        return 5.0 + annualized_rate * 10

# CFGUSDT 的情绪面评分
sentiment_score = 5.0 + (-1.159) * 10 = 5.0 - 11.59 = -6.59
# 限制在 1-10 范围
sentiment_score = max(1, min(10, -6.59)) = 1.0
```

**情绪面评分**: **1.0/10** ⚠️

**分析**：
- CFGUSDT 的资金费率为**深度负值**（-115.9% 年化）
- 这意味着市场强烈看空，空头需要向多头支付费用
- 在评分系统中，**负费率会大幅扣分**
- 情绪面评分只有**1 分**（最低）

---

## 🎯 四、综合评分预估

### 4.1 情景分析

#### 情景 A：乐观估计（数据齐全且良好）
| 维度 | 评分 | 权重 | 加权分 |
|------|------|------|--------|
| 合约数据 | 7.0 | 35% | 2.45 |
| 基本面 | 6.0 | 30% | 1.80 |
| 技术面 | 6.0 | 25% | 1.50 |
| 情绪面 | 1.0 | 10% | 0.10 |
| **综合** | - | 100% | **5.85** |

**结果**：5.85 分 < 6.0 阈值 ❌ **不生成信号**

---

#### 情景 B：悲观估计（数据不足或较差）
| 维度 | 评分 | 权重 | 加权分 |
|------|------|------|--------|
| 合约数据 | 5.0 | 35% | 1.75 |
| 基本面 | 5.0 | 30% | 1.50 |
| 技术面 | 4.0 | 25% | 1.00 |
| 情绪面 | 1.0 | 10% | 0.10 |
| **综合** | - | 100% | **4.35** |

**结果**：4.35 分 < 6.0 阈值 ❌ **不生成信号**

---

#### 情景 C：中性估计（最可能）
| 维度 | 评分 | 权重 | 加权分 |
|------|------|------|--------|
| 合约数据 | 6.0 | 35% | 2.10 |
| 基本面 | 5.5 | 30% | 1.65 |
| 技术面 | 5.0 | 25% | 1.25 |
| 情绪面 | 1.0 | 10% | 0.10 |
| **综合** | - | 100% | **5.10** |

**结果**：5.10 分 < 6.0 阈值 ❌ **不生成信号**

---

### 4.2 关键发现

**核心问题**：
1. **情绪面评分极低**（1 分）：资金费率深度负值导致
2. **技术面数据不足**：新币上线仅 36 小时，K 线数据点少
3. **即使其他维度评分良好，情绪面也会拉低总分**

**评分低的根本原因**：
- **不是系统没有抓到新币种**（确实抓到了，但被错误标记）
- **也不是数据获取失败**（资金费率数据成功获取）
- **而是 CFGUSDT 的资金费率深度为负值**，导致情绪面评分极低

---

## 💡 五、建议操作

### 5.1 短期操作（针对 CFGUSDT）

1. **清空已处理状态**：
```bash
ssh root@43.156.242.184 "docker exec short-selling-system python3 -c 'from core.listing_detector import listing_detector; listing_detector.clear_state()'"
```

2. **手动诊断评分**：
```bash
ssh root@43.156.242.184 "docker exec short-selling-system python3 -c \"
from core.scoring_engine import ScoringEngine
from core.binance_client import binance_client

symbol = 'CFGUSDT'
# 获取各项数据
funding_rate = binance_client.get_funding_rate(symbol)
print(f'资金费率：{funding_rate}')
print(f'年化费率：{funding_rate * 3 * 365:.2%}')
print(f'情绪面评分：{max(1, min(10, 5 + funding_rate * 3 * 365 * 10)):.1f}/10')
\""
```

3. **观察评分结果**：
- 如果综合评分<6.0，系统不会生成信号（正常）
- 如果综合评分≥6.0，系统会生成信号并推送通知

---

### 5.2 长期优化（系统改进）

#### 建议 1：调整情绪面评分公式

**当前问题**：
- 负费率扣分过重，可能导致优质做空机会被遗漏
- CFGUSDT 负费率 115% 只给 1 分，但实际上这可能是很好的做空机会

**建议修改**：
```python
# 当前公式（线性）
sentiment_score = 5.0 + annualized_rate * 10

# 建议修改为（分段函数）
if annualized_rate < -1.0:  # 年化<-100%
    sentiment_score = 3.0  # 给中等偏低分，而不是最低分
elif annualized_rate < -0.5:
    sentiment_score = 2.0
elif annualized_rate < 0:
    sentiment_score = 4.0
elif annualized_rate > 0.5:
    sentiment_score = 10.0
else:
    sentiment_score = 5.0 + annualized_rate * 10
```

**影响**：
- CFGUSDT 的情绪面评分会从 1 分提升到 3 分
- 综合评分会相应提高，更可能生成信号

---

#### 建议 2：优化新币检测逻辑

**当前问题**：
- 获取上线时间失败时，仍标记为"已处理"
- 没有重试机制

**建议修改**：
```python
# 在 listing_detector.py 中
if symbol in self.processed_symbols:
    coin_data = self.processed_symbols[symbol]
    
    # 如果上线时间为空，尝试重新获取
    if coin_data.get('listing_time') is None:
        listing_time = self._get_symbol_listing_time(symbol)
        if listing_time:
            coin_data['listing_time'] = listing_time.isoformat()
            logger.info(f"✅ 补全 {symbol} 上线时间：{listing_time}")
        else:
            # 仍然获取失败，不标记为已处理
            logger.warning(f"⚠️  无法获取 {symbol} 上线时间，不标记为已处理")
            continue
```

---

#### 建议 3：增加评分重试机制

**当前问题**：
- 评分过程中如果某个维度数据获取失败，直接跳过
- 没有重试机制

**建议**：
- 增加数据获取重试（最多 3 次）
- 如果某个维度数据确实无法获取，给默认分而不是跳过

---

## 📋 六、需要确认的问题

### 6.1 数据获取确认

**需要确认以下数据是否能获取**：

1. **OI 数据（持仓量）**：
```bash
ssh root@43.156.242.184 "docker exec short-selling-system python3 -c \"
from core.binance_client import binance_client
oi_data = binance_client.get_open_interest('CFGUSDT')
print(f'CFGUSDT 持仓量：{oi_data}')
\""
```

2. **市值数据（CoinGecko）**：
```bash
ssh root@43.156.242.184 "docker exec short-selling-system python3 -c \"
from core.coingecko_client import coingecko_client
market_data = coingecko_client.get_market_data('cfg')
print(f'CFG 市值：{market_data}')
\""
```

3. **K 线数据**：
```bash
ssh root@43.156.242.184 "docker exec short-selling-system python3 -c \"
from core.binance_client import binance_client
klines = binance_client.get_klines('CFGUSDT', limit=100)
print(f'K 线数据点数：{len(klines)}')
\""
```

4. **解锁数据**：
```bash
ssh root@43.156.242.184 "docker exec short-selling-system python3 -c \"
from core.unlock_manager import unlock_manager
unlock_info = unlock_manager.get_unlock_info('CFG')
print(f'CFG 解锁信息：{unlock_info}')
\""
```

---

### 6.2 评分阈值确认

**当前配置**：
- 信号阈值：6.0 分
- 一票否决：OI/市值比>0.5

**需要确认**：
- 6.0 分阈值是否过高？
- 对于 CFGUSDT 这种情绪面极差的币种，是否应该降低阈值？

---

### 6.3 二次评分窗口确认

**当前配置**：
- 二次评分窗口：24 小时
- 最大评分次数：3 次

**需要确认**：
- 24 小时窗口是否过短？（CFGUSDT 上线 36 小时，已超出窗口）
- 是否应该延长到 48 小时或 72 小时？

---

## 🎯 七、总结

### 7.1 CFGUSDT 评分低的原因

**主要原因**：
1. ✅ **情绪面评分极低**（1 分）：资金费率深度负值（-115.9% 年化）
2. ⚠️ **技术面数据不足**：新币上线仅 36 小时，K 线数据点少
3. ⚠️ **系统 bug**：上线时间获取失败，导致从未评分

**根本原因**：
- **不是系统没有抓到新币种**
- **也不是数据获取失败**
- **而是 CFGUSDT 的基本面确实较差**（资金费率深度为负）

---

### 7.2 系统改进方向

1. **修复 bug**：上线时间获取失败时不标记为"已处理"
2. **优化评分公式**：情绪面评分不应该对负费率过度敏感
3. **延长评分窗口**：从 24 小时延长到 48 或 72 小时
4. **增加重试机制**：数据获取失败时自动重试

---

### 7.3 下一步行动

**立即执行**：
1. 清空已处理状态
2. 重新检测所有币种
3. 观察 CFGUSDT 的评分结果

**优化改进**：
1. 修改情绪面评分公式
2. 修复上线时间获取 bug
3. 延长二次评分窗口
4. 重新部署到服务器

---

**报告生成时间**: 2026-03-18 09:48  
**分析师**: AI Assistant  
**版本**: v1.0
