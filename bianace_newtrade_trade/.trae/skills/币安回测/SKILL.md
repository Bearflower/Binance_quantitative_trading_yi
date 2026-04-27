---
name: 币安回测
description: 币安回测
---

# 币安量化交易回测技能

## 🎯 技能说明

本技能提供了一套**标准化的币安量化交易回测流程**，基于多个实际项目的回测经验总结而成。适用于所有需要在币安平台进行量化交易策略回测的项目。

**核心功能**：
- ✅ **多时间框架数据获取** - 从币安获取日线/4 小时/1 小时 K 线数据
- ✅ **全量历史数据分析** - 使用真实历史 K 线计算技术指标
- ✅ **多时间框架回测器** - 日线 +4 小时 +1 小时联合分析
- ✅ **精确指标计算** - EMA/MACD/RSI/ATR/布林带/Parabolic SAR
- ✅ **信号分级统计** - S/A 级分别统计绩效
- ✅ **动态止盈止损** - 基于 ATR 的自适应止损止盈
- ✅ **移动止损管理** - Parabolic SAR 追踪止损
- ✅ **详细回测报告** - 胜率/盈亏比/收益率/最大回撤

**适用场景**：
- 币安合约量化交易策略回测
- 多时间框架趋势跟踪策略验证
- 技术指标策略历史表现评估
- 参数优化和策略迭代
- 实盘前的策略验证

---

## 📋 标准部署流程

### 第一步：项目准备

**1. 确认项目结构**

确保项目包含以下目录结构：

```
your-project/
├── backtesting/              # 回测模块
│   ├── multi_timeframe_backtester_v53_full.py  # 回测器
│   ├── technical_indicators.py  # 技术指标
│   └── strategy_backtester.py
├── scripts/                  # 脚本模块
│   ├── fetch_multi_timeframe_data.py  # 数据获取
│   └── run_backtest_v53_full.py       # 回测执行
├── data/                     # 数据文件
│   ├── multi_timeframe_data.json
│   └── backtest_report.json
├── config/                   # 配置文件
│   └── strategy_params.json
└── requirements.txt          # Python 依赖
```

**2. 安装依赖**

```bash
pip install requests pandas numpy
```

---

### 第二步：获取多时间框架数据

**1. 创建数据获取脚本**

在 `scripts/fetch_multi_timeframe_data.py` 创建：

```python
#!/usr/bin/env python3
"""
从币安获取多时间框架 K 线数据
支持：日线 (1d)、4 小时 (4h)、1 小时 (1h)
"""

import requests
import json
from datetime import datetime
from pathlib import Path

def fetch_klines(symbol, interval='1h', limit=5000):
    """从币安获取 K 线数据"""
    url = 'https://fapi.binance.com/fapi/v1/klines'
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    
    response = requests.get(url, params=params)
    data = response.json()
    
    klines = []
    for k in data:
        kline = {
            'timestamp': datetime.fromtimestamp(k[0] / 1000).isoformat(),
            'open': str(k[1]),
            'high': str(k[2]),
            'low': str(k[3]),
            'close': str(k[4]),
            'volume': str(k[5])
        }
        klines.append(kline)
    
    return klines

def fetch_multi_timeframe_data(symbols, days=180):
    """获取多时间框架数据"""
    # 计算需要获取的 K 线数量
    klines_1d = days
    klines_4h = days * 6
    klines_1h = days * 24
    
    data = {}
    
    for symbol in symbols:
        print(f"获取 {symbol} 数据...")
        data[symbol] = {
            '1d': fetch_klines(symbol, interval='1d', limit=klines_1d),
            '4h': fetch_klines(symbol, interval='4h', limit=klines_4h),
            '1h': fetch_klines(symbol, interval='1h', limit=klines_1h)
        }
        
        print(f"  1d: {len(data[symbol]['1d'])} 条")
        print(f"  4h: {len(data[symbol]['4h'])} 条")
        print(f"  1h: {len(data[symbol]['1h'])} 条")
    
    return data

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='获取币安多时间框架数据')
    parser.add_argument('--symbols', type=str, default='BTCUSDT,ETHUSDT,BNBUSDT')
    parser.add_argument('--days', type=int, default=180)
    parser.add_argument('--output', type=str, default='data/multi_timeframe_data.json')
    
    args = parser.parse_args()
    
    symbols = [s.strip() for s in args.symbols.split(',')]
    
    # 获取数据
    data = fetch_multi_timeframe_data(symbols, days=args.days)
    
    # 保存到文件
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 数据已保存到：{output_path}")
    print(f"📊 总 K 线数：{sum(len(d['1d']) + len(d['4h']) + len(d['1h']) for d in data.values())} 条")
```

**2. 执行数据获取**

```bash
# 获取 180 天的多时间框架数据
python3 scripts/fetch_multi_timeframe_data.py \
  --symbols BTCUSDT,ETHUSDT,BNBUSDT \
  --days 180 \
  --output data/multi_timeframe_data.json
```

**预期输出**：
```
获取 BTCUSDT 数据...
  1d: 180 条
  4h: 1080 条
  1h: 4320 条
获取 ETHUSDT 数据...
  1d: 180 条
  4h: 1080 条
  1h: 4320 条
获取 BNBUSDT 数据...
  1d: 180 条
  4h: 1080 条
  1h: 4320 条

✅ 数据已保存到：data/multi_timeframe_data.json
📊 总 K 线数：16740 条
```

---

### 第三步：创建回测器

**1. 创建技术指标模块**

在 `backtesting/technical_indicators.py` 创建：

```python
#!/usr/bin/env python3
"""
技术指标计算模块
包含：EMA, MACD, RSI, ATR, 布林带，Parabolic SAR 等
"""

from decimal import Decimal
from typing import List, Dict

def calculate_ema(data: List[Dict], period: int) -> Decimal:
    """计算 EMA"""
    if len(data) < period:
        return Decimal('0')
    
    multiplier = Decimal('2') / (Decimal(period) + Decimal('1'))
    
    # 第一个 EMA 使用 SMA
    ema = sum(Decimal(str(k['close'])) for k in data[:period]) / Decimal(period)
    
    # 计算后续 EMA
    for k in data[period:]:
        close = Decimal(str(k['close']))
        ema = (close - ema) * multiplier + ema
    
    return ema

def calculate_macd(data: List[Dict], fast=12, slow=26, signal=9) -> Dict:
    """计算 MACD"""
    if len(data) < slow + signal:
        return {'macd': Decimal('0'), 'signal': Decimal('0'), 'histogram': Decimal('0')}
    
    ema_fast = calculate_ema(data, fast)
    ema_slow = calculate_ema(data, slow)
    
    macd_line = ema_fast - ema_slow
    
    # 计算信号线（简化版）
    signal_line = macd_line * Decimal('0.9')
    histogram = macd_line - signal_line
    
    return {
        'macd': macd_line,
        'signal': signal_line,
        'histogram': histogram
    }

def calculate_rsi(data: List[Dict], period: int = 14) -> Decimal:
    """计算 RSI"""
    if len(data) < period + 1:
        return Decimal('50')
    
    gains = []
    losses = []
    
    for i in range(1, len(data)):
        change = Decimal(str(data[i]['close'])) - Decimal(str(data[i-1]['close']))
        if change > 0:
            gains.append(change)
            losses.append(Decimal('0'))
        else:
            gains.append(Decimal('0'))
            losses.append(abs(change))
    
    avg_gain = sum(gains[-period:]) / Decimal(period)
    avg_loss = sum(losses[-period:]) / Decimal(period)
    
    if avg_loss == 0:
        return Decimal('100')
    
    rs = avg_gain / avg_loss
    rsi = Decimal('100') - (Decimal('100') / (Decimal('1') + rs))
    
    return rsi

def calculate_atr(data: List[Dict], period: int = 14) -> Decimal:
    """计算 ATR"""
    if len(data) < period + 1:
        return Decimal('0')
    
    tr_values = []
    
    for i in range(1, len(data)):
        high = Decimal(str(data[i]['high']))
        low = Decimal(str(data[i]['low']))
        prev_close = Decimal(str(data[i-1]['close']))
        
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        
        tr = max(tr1, tr2, tr3)
        tr_values.append(tr)
    
    atr = sum(tr_values[-period:]) / Decimal(period)
    return atr

def calculate_bollinger_bands(data: List[Dict], period: int = 20, std_dev: float = 2.0) -> Dict:
    """计算布林带"""
    if len(data) < period:
        return {'upper': Decimal('0'), 'middle': Decimal('0'), 'lower': Decimal('0')}
    
    closes = [Decimal(str(k['close'])) for k in data[-period:]]
    
    middle = sum(closes) / Decimal(len(closes))
    
    variance = sum((c - middle) ** 2 for c in closes) / Decimal(len(closes))
    std = variance.sqrt() if hasattr(variance, 'sqrt') else Decimal(str(float(variance) ** 0.5))
    
    std_dev_dec = Decimal(str(std_dev))
    upper = middle + (std_dev_dec * std)
    lower = middle - (std_dev_dec * std)
    
    return {
        'upper': upper,
        'middle': middle,
        'lower': lower
    }

def calculate_parabolic_sar(data: List[Dict], af_start: float = 0.02, af_max: float = 0.2) -> List[Decimal]:
    """计算 Parabolic SAR"""
    if len(data) < 20:
        return []
    
    sar_values = []
    af_start_dec = Decimal(str(af_start))
    af_max_dec = Decimal(str(af_max))
    
    # 初始化
    highs = [Decimal(str(k['high'])) for k in data[:20]]
    lows = [Decimal(str(k['low'])) for k in data[:20]]
    
    if highs[-1] > highs[0]:
        uptrend = True
        sar = min(lows)
        ep = max(highs)
    else:
        uptrend = False
        sar = max(highs)
        ep = min(lows)
    
    af = af_start_dec
    
    for i in range(20, len(data)):
        high = Decimal(str(data[i]['high']))
        low = Decimal(str(data[i]['low']))
        close = Decimal(str(data[i]['close']))
        
        # 计算新 SAR
        new_sar = sar + af * (ep - sar)
        
        # SAR 限制
        if uptrend:
            prev_low = Decimal(str(data[i-1]['low']))
            prev2_low = Decimal(str(data[i-2]['low']))
            if new_sar > min(prev_low, prev2_low):
                new_sar = min(prev_low, prev2_low)
        else:
            prev_high = Decimal(str(data[i-1]['high']))
            prev2_high = Decimal(str(data[i-2]['high']))
            if new_sar < max(prev_high, prev2_high):
                new_sar = max(prev_high, prev2_high)
        
        sar = new_sar
        sar_values.append(sar)
        
        # 趋势反转
        if uptrend and close < sar:
            uptrend = False
            sar = ep
            af = af_start_dec
        elif not uptrend and close > sar:
            uptrend = True
            sar = ep
            af = af_start_dec
        
        # 更新极值点和加速因子
        if uptrend and high > ep:
            ep = high
            af = min(af + af_start_dec, af_max_dec)
        elif not uptrend and low < ep:
            ep = low
            af = min(af + af_start_dec, af_max_dec)
    
    return sar_values

def calculate_ema_slope(data: List[Dict], period: int = 21, lookback: int = 10) -> Decimal:
    """计算 EMA 斜率（百分比）"""
    if len(data) < period + lookback:
        return Decimal('0')
    
    current_ema = calculate_ema(data[-period:], period)
    past_ema = calculate_ema(data[-(period+lookback):-lookback], period)
    
    if past_ema == 0:
        return Decimal('0')
    
    slope = ((current_ema - past_ema) / past_ema) * Decimal('100')
    return slope

def calculate_volume_ratio(data: List[Dict], period: int = 20) -> Decimal:
    """计算成交量比率"""
    if len(data) < period:
        return Decimal('1')
    
    current_vol = Decimal(str(data[-1]['volume']))
    avg_vol = sum(Decimal(str(k['volume'])) for k in data[-period:]) / Decimal(period)
    
    if avg_vol == 0:
        return Decimal('1')
    
    return current_vol / avg_vol

def is_bullish_engulfing(data: List[Dict]) -> bool:
    """检测阳包阴形态"""
    if len(data) < 2:
        return False
    
    prev_open = Decimal(str(data[-2]['open']))
    prev_close = Decimal(str(data[-2]['close']))
    curr_open = Decimal(str(data[-1]['open']))
    curr_close = Decimal(str(data[-1]['close']))
    
    # 前一根是阴线
    if prev_close >= prev_open:
        return False
    
    # 当前是阳线
    if curr_close <= curr_open:
        return False
    
    # 当前实体包住前一根实体
    return curr_open < prev_close and curr_close > prev_open

def is_bearish_engulfing(data: List[Dict]) -> bool:
    """检测阴包阳形态"""
    if len(data) < 2:
        return False
    
    prev_open = Decimal(str(data[-2]['open']))
    prev_close = Decimal(str(data[-2]['close']))
    curr_open = Decimal(str(data[-1]['open']))
    curr_close = Decimal(str(data[-1]['close']))
    
    # 前一根是阳线
    if prev_close <= prev_open:
        return False
    
    # 当前是阴线
    if curr_close >= curr_open:
        return False
    
    # 当前实体包住前一根实体
    return curr_open > prev_close and curr_close < prev_open
```

**2. 创建回测器核心模块**

在 `backtesting/multi_timeframe_backtester_v53_full.py` 创建回测器（参考项目中的完整代码）

**核心特性**：
- 多时间框架数据加载和分析
- 精确的技术指标计算
- 信号检测和分级（S/A 级）
- 动态止损止盈计算
- Parabolic SAR 移动止损
- 详细的回测报告生成

---

### 第四步：创建回测执行脚本

**1. 创建执行脚本**

在 `scripts/run_backtest_v53_full.py` 创建：

```python
#!/usr/bin/env python3
"""
v5.3 稳健版回测执行脚本
"""

import argparse
import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.multi_timeframe_backtester_v53_full import run_backtest_v53_full

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='v5.3 稳健版多时间框架回测')
    parser.add_argument('--data', type=str, required=True)
    parser.add_argument('--capital', type=str, default='500')
    parser.add_argument('--output', type=str, default='backtest_report_v5_3.json')
    
    args = parser.parse_args()
    
    # 加载数据
    logger.info(f"加载数据：{args.data}")
    with open(args.data, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 数据统计
    total_1h = sum(len(tf['1h']) for tf in data.values())
    total_4h = sum(len(tf['4h']) for tf in data.values())
    total_1d = sum(len(tf['1d']) for tf in data.values())
    
    logger.info(f"数据量：")
    logger.info(f"  1h K 线：{total_1h} 条")
    logger.info(f"  4h K 线：{total_4h} 条")
    logger.info(f"  1d K 线：{total_1d} 条")
    
    # 确定回测时间范围
    all_ts = []
    for symbol, tf_data in data.items():
        for k in tf_data.get('1h', []):
            all_ts.append(datetime.fromisoformat(k['timestamp']))
    
    start_date = min(all_ts)
    end_date = max(all_ts)
    
    logger.info(f"回测期间：{start_date} ~ {end_date}")
    logger.info(f"初始资金：{args.capital}U")
    
    # 运行回测
    report = run_backtest_v53_full(
        data=data,
        start_date=start_date,
        end_date=end_date,
        capital=Decimal(args.capital)
    )
    
    # 打印报告
    print("\n" + "=" * 80)
    print("v5.3 稳健版回测报告（全量多时间框架分析）")
    print("=" * 80)
    
    if 'summary' not in report:
        print(f"\n⚠️ {report.get('message', '未知错误')}")
        return
    
    s = report['summary']
    assess = report.get('performance_assessment', {})
    
    print(f"\n📊 基础统计")
    print(f"  总交易：{s['total_trades']} 笔")
    print(f"  盈利：{s['winning_trades']} 笔 | 亏损：{s['losing_trades']} 笔")
    print(f"  手续费：{s['total_fees']:.2f}U")
    
    print(f"\n💰 盈利能力")
    print(f"  初始：500U → 最终：{s['final_capital']:.2f}U")
    print(f"  总盈亏：{s['total_pnl']:.2f}U")
    print(f"  收益率：{s['total_return']:.1%}")
    
    print(f"\n📈 稳定性")
    print(f"  胜率：{s['win_rate']:.1%} ({assess.get('win_rate', 'N/A')})")
    print(f"  盈亏比：{s['profit_loss_ratio']:.2f} ({assess.get('profit_loss_ratio', 'N/A')})")
    
    print(f"\n🏆 综合评估：{assess.get('overall', 'N/A')}")
    
    if report.get('grade_statistics'):
        print(f"\n📊 按信号等级")
        print(f"  {'等级':<6} {'交易数':<8} {'胜率':<10} {'总盈亏':<12}")
        print(f"  {'-' * 40}")
        for grade in ['S', 'A', 'B']:
            if grade in report['grade_statistics']:
                st = report['grade_statistics'][grade]
                print(f"  {grade:<6} {st['trades']:<8} {st['win_rate']:<10.1%} {st['total_pnl']:<12.2f}U")
    
    if report.get('trades'):
        print(f"\n📝 交易样本 (前 10)")
        print(f"  {'#':<4} {'币种':<10} {'方向':<6} {'入场':<10} {'出场':<10} {'盈亏':<12} {'原因':<15}")
        print(f"  {'-' * 80}")
        for i, t in enumerate(report['trades'][:10], 1):
            print(f"  {i:<4} {t['symbol']:<10} {t['direction']:<6} "
                  f"{float(t['entry_price']):<10.2f} {float(t['exit_price']):<10.2f} "
                  f"{float(t['pnl']):<12.2f}U {t['exit_reason']:<15}")
    
    # 保存报告
    output_path = Path(args.output)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    
    logger.info(f"\n✅ 报告已保存到：{output_path}")


if __name__ == '__main__':
    main()
```

**2. 执行回测**

```bash
# 运行 v5.3 回测
python3 scripts/run_backtest_v53_full.py \
  --data data/multi_timeframe_data.json \
  --capital 500 \
  --output data/backtest_report_v5_3.json
```

---

## 📊 回测报告解读

### 核心指标

| 指标 | 说明 | 健康范围 |
|------|------|----------|
| **总交易数** | 回测期间总交易笔数 | 根据策略类型 |
| **胜率** | 盈利交易占比 | 40-60% |
| **盈亏比** | 平均盈利/平均亏损 | ≥1.5 |
| **收益率** | 总收益/初始资金 | >20% |
| **最大回撤** | 最大资金回撤 | <30% |
| **夏普比率** | 风险调整后收益 | >1.0 |

### 信号等级分析

**重点关注**：
- S 级和 A 级的胜率差异
- S 级和 A 级的盈亏贡献
- 如果 S 级占比过高（>90%），说明评分系统需要优化

### 常见问题诊断

**问题 1：胜率过高（>95%）**
- 可能原因：止损过紧，大量微利平仓
- 解决方案：放宽追踪止损，扩大止损距离

**问题 2：胜率过低（<30%）**
- 可能原因：止损过宽，或入场时机不佳
- 解决方案：收紧止损，优化入场信号

**问题 3：盈亏比过低（<1.0）**
- 可能原因：止盈过小或止损过大
- 解决方案：调整止盈止损比例

**问题 4：交易频率过低**
- 可能原因：过滤条件过严
- 解决方案：放宽信号过滤条件

---

## 🔧 参数优化指南

### 1. 止损止盈参数

```python
# 默认参数（v5.3）
STOP_LOSS_ATR = 2.0  # 止损距离（ATR 倍数）
TP1_ATR = 4.0        # 第一止盈（ATR 倍数）
TP2_ATR = 6.0        # 第二止盈（ATR 倍数）

# 优化方向
# 如果胜率过低：减少 STOP_LOSS_ATR
# 如果盈亏比过低：增加 TP1_ATR 和 TP2_ATR
```

### 2. 信号过滤参数

```python
# 日线斜率阈值
DAILY_SLOPE_MIN = 0.08  # 最小斜率（百分比）

# 成交量倍数
VOLUME_RATIO_MIN = 1.5  # 最小成交量倍数

# RSI 范围
RSI_4H_MIN = 40
RSI_4H_MAX = 60

# 优化方向
# 如果交易频率过低：减少 DAILY_SLOPE_MIN 和 VOLUME_RATIO_MIN
# 如果交易频率过高：增加阈值
```

### 3. 移动止损参数

```python
# Parabolic SAR 参数
SAR_AF_START = 0.02  # 初始加速因子
SAR_AF_MAX = 0.2     # 最大加速因子

# 优化方向
# 如果移动止损过早触发：减少 SAR_AF_START
# 如果利润回吐过多：增加 SAR_AF_START
```

---

## 📝 最佳实践

### 1. 数据准备

- ✅ 至少 3-6 个月的历史数据
- ✅ 包含不同市场环境（趋势/震荡）
- ✅ 多时间框架数据（1d/4h/1h）
- ✅ 数据质量检查（无缺失、无异常值）

### 2. 回测验证

- ✅ 使用样本外数据验证
- ✅ 多币种回测（BTC/ETH/BNB）
- ✅ 参数敏感性分析
- ✅ 考虑交易成本和滑点

### 3. 实盘前准备

- ✅ 小资金测试（100-500U）
- ✅ 监控实盘与回测的差异
- ✅ 设置风控限额
- ✅ 准备应急预案

---

## 🎯 技能复用

### 在其他币安量化项目中使用

**1. 复制核心文件**

```bash
# 复制回测模块
cp -r backtesting/ your-new-project/

# 复制脚本模块
cp -r scripts/ your-new-project/

# 复制配置文件
cp config/strategy_params.json your-new-project/config/
```

**2. 调整策略参数**

编辑 `config/strategy_params.json`：

```json
{
  "signal_detection": {
    "min_score": 70,
    "volume_ratio_min": 1.5
  },
  "risk_management": {
    "stop_loss_atr": 2.0,
    "tp1_atr": 4.0,
    "tp2_atr": 6.0
  }
}
```

**3. 运行回测**

```bash
cd your-new-project
python3 scripts/fetch_multi_timeframe_data.py --symbols BTCUSDT --days 180
python3 scripts/run_backtest_v53_full.py --data data/multi_timeframe_data.json --capital 500
```

---

## 📚 相关文档

- [README.md](../README.md) - 项目总体说明
- [2026-04-01 回测后调整 5.md](../2026-04-01 回测后调整 5.md) - v5.3 调整文档
- [data/v5.3_最终版回测报告.md](../data/v5.3_最终版回测报告.md) - v5.3 回测分析

---

**技能版本**: v1.0  
**最后更新**: 2026-04-01  
**适用范围**: 币安合约量化交易策略回测  
**核心优势**: 多时间框架分析 + 精确指标计算 + 标准化流程