# btc_eth 策略 v6.16.10 系统性重构计划

## 概要

将 `strategies/btc_eth/` 策略从当前类 v6.16.7 参数体系，对齐至 [v6.16.10 最终交易策略规范](file:///Users/yl/vscode/Binance_quantitative_trading/docs/requirements/btc_eth/v6.16.10 最终交易策略规范（全仓自动化版 · 500U 阶段一专用）.md)，共涉及 **28 个功能点**。

## 当前状态分析

### 不改动的模块（确认）

| 模块 | 原因 |
|------|------|
| `execute_signal()` | 下单流程（杠杆设置→限价单→止损单→止盈单）不变 |
| `_close_position()` | 平仓逻辑（限价/市价选择、轮询、超时）不变 |
| `_cleanup_position_orders()` | 条件单清理三层防护不变 |
| `_cleanup_residual_orders()` | 兜底扫描不变 |
| `_get_optimized_price()` | 限价单优化（买一/卖一价）不变 |
| `_get_symbol_precision()` | 精度获取不变 |
| `main.py` 调度逻辑 | 定时任务、初始化流程不变 |
| `FrequencyController` 核心逻辑 | 频率控制框架不变，仅参数调整 |

### 需改动的文件

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `config.yaml` | 重写 | 新增 SYMBOL_CONFIG、更新所有参数 |
| `strategy.py` | 大改 | 新增过滤器、重构评分、新增风控 |

---

## 重构方案

### 阶段一：配置文件更新（config.yaml）

**改动范围：** 纯配置，无代码逻辑变更。

#### 1.1 评分系统参数

```yaml
scoring:
  min_score: 75
  grade_thresholds:
    S: 90   # 将被 SYMBOL_CONFIG 中 s_min_score 覆盖
    A: 75
    B: 65
    C: 55
  weights:
    trend_strength: 0.20      # 40% → 20%
    pattern_quality: 0.50     # 35% → 50%
    momentum_divergence: 0.30 # 25% → 30%
  a_level_bonus:
    rsi_low: 35
    rsi_high: 65
    bonus: 2
```

#### 1.2 新增币种差异化配置表

```yaml
symbol_config:
  SOLUSDT:
    adx_min: 22
    vol_ratio_base: {S: 2.0, A: 1.8, B: 0, C: 0}
    atr_abs_min: 0.006
    atr_percentile: 35
    atr_factor_strong: 0.85
    s_min_score: 88
    max_daily_trades: 1
    position_ratio_s: 0.25
  BTCUSDT:
    adx_min: 12
    vol_ratio_base: {S: 1.2, A: 1.0, B: 0, C: 0}
    atr_abs_min: 0.005
    atr_percentile: 30
    atr_factor_strong: 0.7
    s_min_score: 82
    max_daily_trades: 2
    position_ratio_s: 0.50
  ETHUSDT:
    adx_min: 12
    vol_ratio_base: {S: 1.2, A: 1.0, B: 0, C: 0}
    atr_abs_min: 0.005
    atr_percentile: 30
    atr_factor_strong: 0.7
    s_min_score: 82
    max_daily_trades: 2
    position_ratio_s: 0.50
  BNBUSDT:
    adx_min: 15
    vol_ratio_base: {S: 1.0, A: 1.0, B: 0, C: 0}
    atr_abs_min: 0.004
    atr_percentile: 30
    atr_factor_strong: 0.8
    s_min_score: 80
    max_daily_trades: 2
    position_ratio_s: 0.50
  XRPUSDT:
    adx_min: 15
    vol_ratio_base: {S: 1.0, A: 1.0, B: 0, C: 0}
    atr_abs_min: 0.004
    atr_percentile: 30
    atr_factor_strong: 0.8
    s_min_score: 80
    max_daily_trades: 2
    position_ratio_s: 0.50
  TRXUSDT:
    adx_min: 15
    vol_ratio_base: {S: 1.0, A: 1.0, B: 0, C: 0}
    atr_abs_min: 0.003
    atr_percentile: 25
    atr_factor_strong: 0.7
    s_min_score: 78
    max_daily_trades: 2
    position_ratio_s: 0.50
```

#### 1.3 止盈止损参数

```yaml
risk:
  stop_loss_atr_multiplier: 1.5        # 2.0 → 1.5
  partial_take_profit:
    tp1_atr_multiplier: 4.0            # 2.5 → 4.0
    tp1_close_ratio: 0.25
    tp2_atr_multiplier: 6.0            # 4.0 → 6.0
    tp2_close_ratio: 0.25
  chandelier_stop:
    activation_atr: 2.5                # 1.8 → 2.5
    trailing_atr: 1.5                  # 1.2 → 1.5
```

#### 1.4 频率控制参数

```yaml
  frequency_control:
    max_daily_total_trades: 6          # 4 → 6
    max_daily_symbol_trades: 2
    symbol_cooldown_hours: 12
    consecutive_loss_pause: 5
    pause_duration_hours: 24
    max_daily_loss_usdt: 25
    max_daily_loss_ratio: 0.05         # 新增：5%
    initial_capital_usdt: 500
```

#### 1.5 新增过滤器配置

```yaml
  # 趋势过滤器
  trend_filter:
    enabled: true
    ema_slope_min: 0.0005              # 0.05%
    ema_slope_flat: 0.0003             # 0.03%（禁止入场阈值）
    ema21_proximity_atr_mult: 1.5      # 4h价格距EMA21 ≤ 1.5×ATR

  # 禁止入场条件
  prohibition:
    atr_price_max: 0.045               # ATR/价格 > 4.5%
    atr_price_min: 0.010               # ATR/价格 < 1.0%
    kline_spike_6h_pct: 0.05           # 6h内单根1h K线涨跌幅 > 5%
    funding_rate_max_abs: 0.0005       # 资金费率绝对值 > 0.05%
    daily_change_long_max: 0.25        # 24h涨幅 > 25%（做多禁止）
    daily_change_short_max: -0.20      # 24h跌幅 < -20%（做空禁止）
    spread_max: 0.003                  # 买卖价差 > 0.3%

  # 动态ATR过滤器
  dynamic_atr:
    enabled: true
    lookback_hours: 720
    min_samples: 30

  # 动态成交量过滤器
  dynamic_volume:
    enabled: true

  # 极端行情
  extreme_market:
    reverse_pct: 0.05                  # 瞬间反向5%
    close_ratio: 0.50                  # 平仓50%
    tighten_stop_atr: 1.0              # 收紧止损至1.0×ATR

  # 强平预警
  liquidation_warning:
    margin_ratio_reduce: 1.5           # 保证金率 ≤1.5 减仓50%
    margin_ratio_close: 1.2            # 保证金率 ≤1.2 全部平仓
    reduce_ratio: 0.50

  # 新仓位管理
  position_management:
    max_concurrent_positions: 2        # 同时持仓 ≤ 2个品种
    volatility_target_risk: 10         # 波动率目标风险 10U
    volatility_target_min: 5           # 下限 5U
    volatility_target_max: 15          # 上限 15U
```

---

### 阶段二：评分系统重构（strategy.py）

#### 2.1 修改 `_determine_grade()` — 支持币种差异化 S 级阈值

**差距 #2**

```python
def _determine_grade(self, score: float, symbol: str = None) -> str:
    """
    确定信号等级，支持币种差异化 S 级阈值
    
    v6.16.10: S ≥ symbol_config.s_min_score, A ≥ 75, B ≥ 65, C ≥ 55
    """
    thresholds = self.scoring_config['grade_thresholds']
    
    # 币种差异化 S 级阈值
    if symbol and symbol in self.symbol_config:
        s_threshold = self.symbol_config[symbol].get('s_min_score', thresholds['S'])
    else:
        s_threshold = thresholds['S']
    
    if score >= s_threshold:
        return 'S'
    elif score >= thresholds['A']:
        return 'A'
    elif score >= thresholds['B']:
        return 'B'
    else:
        return 'C'
```

#### 2.2 修改 `_calculate_score()` — A 级额外加分

**差距 #3**

在 `_calculate_score()` 末尾，等级判定前增加：

```python
# A级额外加分：4h RSI 在 35~65 之间 +2 分
if '4h' in indicators:
    rsi_4h = indicators['4h']['RSI'].iloc[-1]
    bonus_config = self.scoring_config.get('a_level_bonus', {})
    if pd.notna(rsi_4h):
        rsi_low = bonus_config.get('rsi_low', 35)
        rsi_high = bonus_config.get('rsi_high', 65)
        if rsi_low <= rsi_4h <= rsi_high:
            score += bonus_config.get('bonus', 2)
```

---

### 阶段三：趋势过滤器实现（strategy.py）

**差距 #5-#10** — 在 `analyze()` 中步骤 3（计算指标）之后、步骤 4（计算评分）之前插入。

#### 3.1 新增方法 `_check_trend_filter()`

```python
def _check_trend_filter(
    self, 
    symbol: str, 
    indicators: Dict, 
    klines: Dict
) -> Tuple[bool, str]:
    """
    v6.16.10 趋势过滤器（硬性条件）
    
    多头方向：
    - 日线收盘价 > 日线 EMA55
    - 日线 EMA21 斜率 > 0.05%（最近5根日线线性回归）
    - 4h 价格回调至 EMA21 附近（≤ 1.5×ATR）
    - 1h 收盘价 > 1h EMA21
    
    禁止入场：
    - 日线 EMA21 斜率绝对值 < 0.03%
    - ATR/价格 > 4.5% 或 < 1.0%
    
    Returns:
        (是否通过, 失败原因)
    """
    config = self.risk_config.get('trend_filter', {})
    if not config.get('enabled', True):
        return True, ""
    
    # 1. 日线趋势判断
    if '1d' not in indicators:
        return False, "日线数据缺失"
    
    df_1d = pd.DataFrame(klines['1d'])
    close_1d = Decimal(str(df_1d['close'].iloc[-1]))
    ema55_1d = indicators['1d']['EMA55'].iloc[-1]
    
    # 日线 EMA21 斜率（最近5根线性回归）
    ema21_series = indicators['1d']['MA21'].iloc[-5:]
    if len(ema21_series) >= 5:
        x = np.arange(5)
        slope, _ = np.polyfit(x, ema21_series.values, 1)
        slope_pct = slope / ema21_series.iloc[-1]
    else:
        slope_pct = 0
    
    # 禁止：斜率过平
    if abs(slope_pct) < config['ema_slope_flat']:
        return False, f"日线EMA21斜率过平({slope_pct*100:.2f}%)"
    
    # 确定方向
    if slope_pct > 0:
        direction = 'LONG'
    else:
        direction = 'SHORT'
    
    # 做多硬性条件
    if direction == 'LONG':
        if close_1d <= ema55_1d:
            return False, "日线收盘价未站上EMA55"
        if slope_pct < config['ema_slope_min']:
            return False, f"日线EMA21斜率不足({slope_pct*100:.2f}% < 0.05%)"
    else:
        if close_1d >= ema55_1d:
            return False, "日线收盘价未跌破EMA55"
        if slope_pct > -config['ema_slope_min']:
            return False, f"日线EMA21斜率不足({slope_pct*100:.2f}% > -0.05%)"
    
    # 2. 4h 价格回调检查
    if '4h' in indicators:
        atr_4h = indicators['4h']['ATR'].iloc[-1]
        ema21_4h = indicators['4h']['MA21'].iloc[-1]
        close_4h = Decimal(str(klines['4h'][-1]['close']))
        
        proximity = abs(close_4h - Decimal(str(ema21_4h)))
        max_proximity = Decimal(str(atr_4h)) * Decimal(str(config['ema21_proximity_atr_mult']))
        
        if proximity > max_proximity:
            return False, f"4h价格距EMA21过远({float(proximity):.2f} > {float(max_proximity):.2f})"
    
    # 3. 1h 收盘价检查
    if '1h' in indicators:
        ema21_1h = indicators['1h']['MA21'].iloc[-1]
        close_1h = Decimal(str(klines['1h'][-1]['close']))
        
        if direction == 'LONG' and close_1h <= Decimal(str(ema21_1h)):
            return False, "1h收盘价未站上EMA21"
        elif direction == 'SHORT' and close_1h >= Decimal(str(ema21_1h)):
            return False, "1h收盘价未跌破EMA21"
    
    # 4. ATR/价格检查
    if '1h' in indicators:
        atr_1h = Decimal(str(indicators['1h']['ATR'].iloc[-1]))
        atr_ratio = float(atr_1h / close_1h)
        prohibition = self.risk_config.get('prohibition', {})
        
        if atr_ratio > prohibition.get('atr_price_max', 0.045):
            return False, f"波动率过高(ATR/价格={atr_ratio*100:.1f}% > 4.5%)"
        if atr_ratio < prohibition.get('atr_price_min', 0.010):
            return False, f"波动率过低(ATR/价格={atr_ratio*100:.1f}% < 1.0%)"
    
    return True, direction
```

---

### 阶段四：禁止入场条件实现（strategy.py）

**差距 #11-#14** — 在 `analyze()` 中评分之前调用。

#### 4.1 新增方法 `_check_prohibited_conditions()`

```python
async def _check_prohibited_conditions(
    self, 
    symbol: str, 
    klines: Dict
) -> Tuple[bool, str]:
    """
    v6.16.10 禁止入场条件
    
    Returns:
        (是否允许入场, 禁止原因)
    """
    config = self.risk_config.get('prohibition', {})
    
    # 1. 最近6h内单根1h K线涨跌幅 > 5%
    if '1h' in klines:
        df_1h = pd.DataFrame(klines['1h'])
        recent_6 = df_1h.tail(6)
        for _, row in recent_6.iterrows():
            pct = abs((row['close'] - row['open']) / row['open'])
            if pct > config.get('kline_spike_6h_pct', 0.05):
                return False, f"6h内出现单根K线涨跌幅{pct*100:.1f}% > 5%"
    
    # 2. 资金费率绝对值 > 0.05%
    try:
        funding_rate = await self.binance.get_funding_rate(symbol)
        if abs(funding_rate) > config.get('funding_rate_max_abs', 0.0005):
            return False, f"资金费率{funding_rate*100:.2f}% > 0.05%"
    except Exception as e:
        logger.warning(f"{symbol} 获取资金费率失败: {e}")
    
    # 3. 24h涨跌幅
    try:
        ticker_24h = await self.binance.get_24h_ticker(symbol)
        price_change_pct = float(ticker_24h.get('priceChangePercent', 0)) / 100
        
        if price_change_pct > config.get('daily_change_long_max', 0.25):
            return False, f"24h涨幅{price_change_pct*100:.1f}% > 25%"
        if price_change_pct < config.get('daily_change_short_max', -0.20):
            return False, f"24h跌幅{price_change_pct*100:.1f}% < -20%"
    except Exception as e:
        logger.warning(f"{symbol} 获取24h涨跌幅失败: {e}")
    
    # 4. 买卖价差 > 0.3%
    try:
        orderbook = await self.binance.get_orderbook(symbol, limit=1)
        best_bid = Decimal(str(orderbook['bids'][0][0]))
        best_ask = Decimal(str(orderbook['asks'][0][0]))
        spread = float((best_ask - best_bid) / best_bid)
        
        if spread > config.get('spread_max', 0.003):
            return False, f"买卖价差{spread*100:.2f}% > 0.3%"
    except Exception as e:
        logger.warning(f"{symbol} 获取orderbook失败: {e}")
    
    return True, ""
```

---

### 阶段五：动态 ATR 过滤器集成（strategy.py）

**差距 #15** — 在 `__init__()` 中实例化，在 `analyze()` 中调用。

#### 5.1 构造函数中初始化

```python
from shared.dynamic_atr_filter import DynamicATRFilter

# 在 __init__() 中添加：
self.atr_filter = DynamicATRFilter(
    self.risk_config.get('dynamic_atr', {})
)
```

#### 5.2 analyze() 中调用

在评分计算之前，增加 ATR 过滤：

```python
# 动态 ATR 过滤器
if self.atr_filter.enabled:
    # 更新 ATR 历史
    atr_pct = float(atr / current_price)
    self.atr_filter.update_history(symbol, float(atr), float(current_price))
    
    # 过滤低波动
    adx_1d = indicators.get('1d', {}).get('ADX', pd.Series([0])).iloc[-1]
    should_filter, filter_reason = self.atr_filter.should_filter(
        symbol, atr_pct * 100, float(adx_1d) if pd.notna(adx_1d) else 0
    )
    if should_filter:
        analysis_result['reason'] = f"ATR过滤: {filter_reason}"
        return analysis_result
```

#### 5.3 启动时初始化历史

在 `main.py` 的 `initialize()` 中，为每个币种初始化 ATR 历史数据（从 K 线服务获取历史数据填充）。

---

### 阶段六：动态成交量过滤器（strategy.py）

**差距 #16**

#### 6.1 新增方法 `_check_volume_filter()`

```python
def _check_volume_filter(
    self, 
    symbol: str, 
    grade: str, 
    klines: Dict
) -> Tuple[bool, str]:
    """
    v6.16.10 动态成交量过滤器
    
    对 SOLUSDT 使用严格倍数，其他币种使用上表阈值。
    B/C 级不检查成交量。
    """
    config = self.risk_config.get('dynamic_volume', {})
    if not config.get('enabled', True):
        return True, ""
    
    # B/C 级不检查成交量
    if grade in ('B', 'C'):
        return True, ""
    
    # 获取币种配置
    symbol_cfg = self.symbol_config.get(symbol, {})
    vol_ratio = symbol_cfg.get('vol_ratio_base', {})
    required_mult = vol_ratio.get(grade, 0)
    
    if required_mult == 0:
        return True, ""
    
    # 计算当前1h成交量 / 过去20h均量
    if '1h' not in klines:
        return True, ""
    
    df_1h = pd.DataFrame(klines['1h'])
    current_vol = df_1h['volume'].iloc[-1]
    avg_vol_20h = df_1h['volume'].iloc[-21:-1].mean()
    
    if pd.isna(avg_vol_20h) or avg_vol_20h == 0:
        return True, ""
    
    vol_ratio_actual = current_vol / avg_vol_20h
    
    if vol_ratio_actual < required_mult:
        return False, f"成交量不足({vol_ratio_actual:.1f}x < {required_mult}x)"
    
    return True, ""
```

---

### 阶段七：波动率目标仓位（strategy.py）

**差距 #22** — 修改 `_calculate_position_size()`。

#### 7.1 修改仓位计算逻辑

```python
async def _calculate_position_size(
    self,
    grade: str,
    current_price: Decimal,
    symbol: str = None
) -> Optional[Decimal]:
    """
    v6.16.10 波动率目标仓位
    
    单笔风险 = 10U × (历史中位ATR% / 当前ATR%)，限制 [5U, 15U]
    """
    try:
        account_info = await self.binance.get_account_info()
        available_balance = Decimal(str(account_info['availableBalance']))
        
        pm_config = self.risk_config.get('position_management', {})
        max_concurrent = pm_config.get('max_concurrent_positions', 2)
        
        # 同时持仓检查
        active_positions = sum(
            1 for p in self.positions.values() if p.current_quantity > 0
        )
        if active_positions >= max_concurrent:
            return None
        
        # 获取币种差异化仓位比例
        if symbol and symbol in self.symbol_config:
            if grade == 'S':
                position_ratio = Decimal(str(
                    self.symbol_config[symbol].get('position_ratio_s', 0.50)
                ))
            else:
                position_ratio = Decimal(str(
                    self.binance_config['position_ratio'][grade]
                ))
        else:
            position_ratio = Decimal(str(
                self.binance_config['position_ratio'][grade]
            ))
        
        # 波动率目标仓位
        vol_config = self.risk_config.get('position_management', {})
        target_risk = Decimal(str(vol_config.get('volatility_target_risk', 10)))
        min_risk = Decimal(str(vol_config.get('volatility_target_min', 5)))
        max_risk = Decimal(str(vol_config.get('volatility_target_max', 15)))
        
        # 获取当前ATR%和历史中位ATR%
        if hasattr(self, 'atr_filter') and self.atr_filter.enabled:
            stats = self.atr_filter.get_statistics(symbol)
            median_atr_pct = Decimal(str(stats.get('percentile_35', 0.01)))
            current_atr_pct = Decimal(str(stats.get('current_atr_pct', 0.01)))
            
            if current_atr_pct > 0 and median_atr_pct > 0:
                risk_amount = target_risk * (median_atr_pct / current_atr_pct)
                risk_amount = max(min_risk, min(max_risk, risk_amount))
            else:
                risk_amount = target_risk
        else:
            risk_amount = target_risk
        
        # 安全垫
        safety_margin_ratio = Decimal(str(
            self.risk_config['position_sizing']['safety_margin_ratio']
        ))
        usable_balance = available_balance * (Decimal('1') - safety_margin_ratio)
        
        # 计算仓位 = 可用资金 × 等级比例
        position_size = usable_balance * position_ratio
        
        max_position = Decimal(str(
            self.risk_config['position_sizing']['max_single_position_usdt']
        ))
        position_size = min(position_size, max_position)
        
        return position_size
        
    except Exception as e:
        logger.error("计算仓位大小失败", error=str(e))
        return None
```

---

### 阶段八：新增风控功能（strategy.py）

**差距 #25, #27, #28**

#### 8.1 每日最大亏损百分比

在 `FrequencyController.can_trade()` 中：

```python
# 每日最大亏损检查（百分比版本）
max_loss_ratio = Decimal(str(self.config.get('max_daily_loss_ratio', 0.05)))
max_loss_absolute = Decimal(str(self.config.get('max_daily_loss_usdt', 25)))
initial_capital = Decimal(str(self.config.get('initial_capital_usdt', 500)))

loss_limit = max(max_loss_absolute, initial_capital * max_loss_ratio)
if daily_pnl[today] <= -loss_limit:
    return False, f"每日亏损已达上限({-float(daily_pnl[today]):.1f}U)"
```

#### 8.2 极端行情处理

在 `update_positions()` 中，每个持仓检查前增加：

```python
async def _check_extreme_market(
    self, 
    symbol: str, 
    position: PositionState, 
    current_price: Decimal
) -> bool:
    """
    v6.16.10 极端行情处理
    
    瞬间反向5% → 平仓50%，止损收紧至1.0×ATR
    """
    config = self.risk_config.get('extreme_market', {})
    reverse_pct = Decimal(str(config.get('reverse_pct', 0.05)))
    
    if position.direction == 'LONG':
        loss_pct = (position.entry_price - current_price) / position.entry_price
    else:
        loss_pct = (current_price - position.entry_price) / position.entry_price
    
    if loss_pct >= reverse_pct:
        logger.warning(f"{symbol} 触发极端行情，反向{float(loss_pct)*100:.1f}%")
        
        # 平仓50%
        close_qty = position.current_quantity * Decimal('0.5')
        await self._close_position(symbol, position, close_qty, "EXTREME")
        
        # 收紧止损至1.0×ATR
        tighten_atr = Decimal(str(config.get('tighten_stop_atr', 1.0)))
        if position.direction == 'LONG':
            new_stop = current_price - position.atr * tighten_atr
        else:
            new_stop = current_price + position.atr * tighten_atr
        
        # 取消旧止损单，下新止损单
        if position.stop_loss_order_id:
            await self.binance.cancel_order(symbol, position.stop_loss_order_id)
        new_stop_order = await self.binance.place_conditional_order(
            symbol, 'SELL' if position.direction == 'LONG' else 'BUY',
            new_stop, position.current_quantity, 'STOP_MARKET'
        )
        position.stop_loss_order_id = new_stop_order.get('orderId')
        return True
    
    return False
```

#### 8.3 强平预警

在 `update_positions()` 中，每个持仓检查前增加：

```python
async def _check_liquidation_warning(
    self, 
    symbol: str, 
    position: PositionState
) -> bool:
    """
    v6.16.10 强平预警
    
    保证金率 ≤ 1.5 减仓50%，≤ 1.2 全部平仓
    """
    config = self.risk_config.get('liquidation_warning', {})
    
    try:
        positions = await self.binance.get_position_risk(symbol)
        for p in positions:
            if p['symbol'] == symbol:
                margin_ratio = float(p.get('marginRatio', 999))
                
                if margin_ratio <= config.get('margin_ratio_close', 1.2):
                    logger.error(f"{symbol} 强平预警：保证金率{margin_ratio}，全部平仓")
                    await self._close_position(
                        symbol, position, position.current_quantity, "LIQUIDATION"
                    )
                    return True
                
                elif margin_ratio <= config.get('margin_ratio_reduce', 1.5):
                    logger.warning(f"{symbol} 强平预警：保证金率{margin_ratio}，减仓50%")
                    close_qty = position.current_quantity * Decimal(
                        str(config.get('reduce_ratio', 0.5))
                    )
                    await self._close_position(
                        symbol, position, close_qty, "LIQUIDATION_REDUCE"
                    )
                    return True
    except Exception as e:
        logger.error(f"{symbol} 强平检查失败: {e}")
    
    return False
```

---

### 阶段九：analyze() 流程整合

重构后的 `analyze()` 流程：

```
1. 频率控制检查 (can_trade)
2. 获取K线数据 (get_multi_timeframe_data)
3. 计算技术指标 (calculate_all)
4. 【新增】趋势过滤器检查 (_check_trend_filter) → 返回方向
5. 【新增】禁止入场条件检查 (_check_prohibited_conditions)
6. 【新增】动态ATR过滤器 (should_filter)
7. 计算综合评分 (_calculate_score) → 含A级额外加分
8. 评分阈值判断
9. 确定信号等级 (_determine_grade) → 含币种差异化
10. 【新增】动态成交量过滤器 (_check_volume_filter)
11. 计算入场价和风险参数
12. 计算动态仓位 (_calculate_position_size) → 含波动率目标
13. 获取优化限价单价格
14. 生成信号
```

`update_positions()` 流程增加：

```
1. 获取当前价
2. 更新最高/最低价
3. 【新增】强平预警检查 (_check_liquidation_warning)
4. 【新增】极端行情检查 (_check_extreme_market)
5. 检查分批止盈 (_check_partial_take_profit)
6. 检查吊灯止损 (_check_chandelier_stop)
7. 检查时间止损 (_check_time_stop)
8. 兜底清理 (_cleanup_residual_orders)
```

---

## 不改动的模块清单

| 模块 | 方法 | 原因 |
|------|------|------|
| 下单 | `execute_signal()` | 开仓逻辑不变 |
| 平仓 | `_close_position()` | 平仓逻辑不变 |
| 限价优化 | `_get_optimized_price()` | 不变 |
| 精度 | `_get_symbol_precision()`, `_adjust_quantity_precision()`, `_adjust_price_precision()` | 不变 |
| 条件单清理 | `_cleanup_position_orders()`, `_cleanup_residual_orders()` | 不变 |
| 止盈价格 | `_calculate_tp_price()` | 通过 config 参数调整 |
| 调度 | `main.py` 的 `run_strategy()` | 不变 |
| 频率控制 | `FrequencyController` 核心框架 | 仅参数调整 |

---

## 实现顺序

| 顺序 | 阶段 | 改动文件 | 行数估计 |
|------|------|---------|---------|
| 1 | 配置文件更新 | `config.yaml` | ~80行 |
| 2 | 评分系统重构 | `strategy.py` | ~30行 |
| 3 | 趋势过滤器 | `strategy.py` | ~80行 |
| 4 | 禁止入场条件 | `strategy.py` | ~60行 |
| 5 | 动态ATR集成 | `strategy.py` + `main.py` | ~40行 |
| 6 | 动态成交量 | `strategy.py` | ~40行 |
| 7 | 波动率目标仓位 | `strategy.py` | ~50行 |
| 8 | 极端行情+强平预警 | `strategy.py` | ~80行 |
| 9 | analyze()流程整合 | `strategy.py` | ~20行 |

**总计：约 460 行新增/修改代码**

---

## 验证方案

1. **配置验证**：检查 `config.yaml` 语法正确性
2. **代码规范检查**：调用 `code-specification-inspector` 检查硬编码、命名规范
3. **单元测试**：对各新增方法编写单元测试（趋势过滤器、禁止条件、评分计算）
4. **集成测试**：在回测环境运行，验证信号生成逻辑与 v6.16.10 回测绩效一致
5. **代码审查**：调用 `TRAE-code-review` 进行审查
6. **部署验证**：部署到服务器后观察 24 小时，确认无异常

---

## 风险与注意事项

1. **资金费率 API 调用**：当前代码未调用 `get_funding_rate`，需确认 `BinanceClient` 是否有此方法
2. **24h 涨跌幅 API**：需确认 `BinanceClient` 是否有 `get_24h_ticker` 方法
3. **强平预警 API**：需确认 `BinanceClient` 是否有 `get_position_risk` 方法
4. **ATR 历史初始化**：启动时需要从 K 线服务获取历史数据填充 `DynamicATRFilter`，需设计初始化流程
5. **参数回退兼容**：建议保留旧配置的备份，出问题可快速回滚