"""
动态利润保护（移动止损）纯计算逻辑

从 btc_eth 策略中抽取的核心计算函数，供 btc_eth 和 new_coin 两策略共用。
所有函数均为纯计算逻辑，不依赖任何策略类实例。

函数列表：
- calculate_dynamic_trailing_stop() — 核心计算函数
- get_volatility_adjustment() — 波动率调节因子计算（异步）
- calculate_retrace_stop_price() — 阶梯回撤止损价计算
- calculate_hard_stop_price() — 硬止损价计算
- apply_one_way_protection() — 单向移动保护
"""

from typing import Optional, Dict, Any
from decimal import Decimal
import time
import structlog
import pandas as pd

from shared.indicators import TechnicalIndicators

logger = structlog.get_logger()


class TrailingStopResult:
    """计算动态利润保护止损价的结果"""

    def __init__(
        self,
        trailing_stop_price: Decimal,
        trailing_activated: bool,
        pending_profit_pct: float,
        current_tier_index: int,
        triggered: bool,
        tier_retrace_ratio: float,
        vol_adj: float,
    ):
        self.trailing_stop_price = trailing_stop_price
        self.trailing_activated = trailing_activated
        self.pending_profit_pct = pending_profit_pct
        self.current_tier_index = current_tier_index
        self.triggered = triggered
        self.tier_retrace_ratio = tier_retrace_ratio
        self.vol_adj = vol_adj


def calculate_retrace_stop_price(
    direction: str,
    reference_price: Decimal,
    entry_price: Decimal,
    retrace_ratio: float,
    vol_adj: float,
) -> Decimal:
    """
    计算阶梯回撤止损价

    公式：
    - LONG:  profit_per_unit = reference_price - entry_price
             allowed_retrace = profit_per_unit * retrace_ratio * vol_adj
             stop_price = reference_price - allowed_retrace
    - SHORT: profit_per_unit = entry_price - reference_price
             allowed_retrace = profit_per_unit * retrace_ratio * vol_adj
             stop_price = reference_price + allowed_retrace

    Args:
        direction: 方向 ('LONG' / 'SHORT')
        reference_price: 参考价（做多传最高价，做空传最低价）
        entry_price: 入场价
        retrace_ratio: 回撤比例
        vol_adj: 波动率调节因子

    Returns:
        Decimal: 回撤止损价
    """
    if direction == 'LONG':
        profit_per_unit = reference_price - entry_price
        allowed_retrace = profit_per_unit * Decimal(str(retrace_ratio)) * Decimal(str(vol_adj))
        stop_price = reference_price - allowed_retrace
    else:
        profit_per_unit = entry_price - reference_price
        allowed_retrace = profit_per_unit * Decimal(str(retrace_ratio)) * Decimal(str(vol_adj))
        stop_price = reference_price + allowed_retrace

    return stop_price


def calculate_hard_stop_price(
    direction: str,
    entry_price: Decimal,
    atr: Decimal,
    stop_loss_atr_multiplier: Decimal,
) -> Decimal:
    """
    计算硬止损价（兜底）

    公式：
    - LONG:  hard_stop_price = entry_price - atr * stop_loss_atr_multiplier
    - SHORT: hard_stop_price = entry_price + atr * stop_loss_atr_multiplier

    Args:
        direction: 方向 ('LONG' / 'SHORT')
        entry_price: 入场价
        atr: 入场时的 ATR 值
        stop_loss_atr_multiplier: 硬止损 ATR 倍数

    Returns:
        Decimal: 硬止损价
    """
    if direction == 'LONG':
        return entry_price - atr * stop_loss_atr_multiplier
    else:
        return entry_price + atr * stop_loss_atr_multiplier


def apply_one_way_protection(
    direction: str,
    new_stop_price: Decimal,
    current_stop_price: Optional[Decimal],
) -> Decimal:
    """
    单向移动保护：止损价只能向有利方向移动

    - LONG:  new >= current → new, 否则 → current
    - SHORT: new <= current → new, 否则 → current

    如果 current_stop_price 为 None（首次设置），直接返回 new_stop_price。

    Args:
        direction: 方向 ('LONG' / 'SHORT')
        new_stop_price: 计算出的新止损价
        current_stop_price: 当前生效的止损价

    Returns:
        Decimal: 最终止损价
    """
    if current_stop_price is None:
        return new_stop_price

    if direction == 'LONG' and new_stop_price <= current_stop_price:
        return current_stop_price
    elif direction == 'SHORT' and new_stop_price >= current_stop_price:
        return current_stop_price

    return new_stop_price


def calculate_dynamic_trailing_stop(
    *,
    direction: str,
    entry_price: Decimal,
    current_price: Decimal,
    highest_price: Optional[Decimal],
    lowest_price: Optional[Decimal],
    trailing_activated: bool,
    tp1_hit: bool,
    tp2_hit: bool,
    pending_profit_pct: Optional[float],
    current_tier_index: int,
    current_trailing_stop_price: Optional[Decimal],
    config: Dict[str, Any],
    atr: Decimal,
    stop_loss_atr_multiplier: Decimal,
    volatility_adj: float = 1.0,
) -> Optional[TrailingStopResult]:
    """
    计算动态利润保护止损价（纯函数，无副作用）

    核心逻辑：
    1. 基于参考价（最高价/最低价）计算浮盈百分比
    2. 检查是否应激活（配置项：also_on_tp1, also_on_tp2, min_profit_pct）
    3. 确定回撤阶梯，计算允许回撤
    4. 计算硬止损价（兜底）
    5. 单向移动保护
    6. 返回计算结果或 None（未激活时）

    Args:
        direction: 方向 ('LONG' / 'SHORT')
        entry_price: 入场价格
        current_price: 当前价格（仅用于参考价回退和触发检查）
        highest_price: 做多时的最高价（做空时传 None）
        lowest_price: 做空时的最低价（做多时传 None）
        trailing_activated: 是否已激活
        tp1_hit: TP1 是否触发
        tp2_hit: TP2 是否触发
        pending_profit_pct: 上次计算的浮盈%
        current_tier_index: 当前回撤阶梯索引
        current_trailing_stop_price: 当前生效的止损价
        config: dynamic_trailing 配置节（完整字典）
        atr: 入场时的 ATR 值
        stop_loss_atr_multiplier: 硬止损 ATR 倍数
        volatility_adj: 波动率调节因子（由调用方传入，默认 1.0）

    Returns:
        None: 未激活
        TrailingStopResult: 计算结果
    """
    # 1. 配置检查
    if not config.get('enabled', True):
        return None

    activation_config = config.get('activation', {})
    tiers = config.get('regression_tiers', [])

    if entry_price is None or entry_price <= 0:
        return None

    # 2. 计算浮盈百分比（基于参考价，而非当前价）
    # 设计依据：用峰值计算允许回撤，才能在价格回落时锁住利润
    if direction == 'LONG':
        # 取最高价作为参考价（若无历史最高价，回退到当前价）
        reference_price = highest_price if highest_price and highest_price > entry_price else current_price
        profit_pct = float((reference_price - entry_price) / entry_price) * 100
    else:
        # 取最低价作为参考价（若无历史最低价，回退到当前价）
        reference_price = lowest_price if lowest_price and lowest_price > 0 and lowest_price < entry_price else current_price
        profit_pct = float((entry_price - reference_price) / entry_price) * 100

    # 浮亏不计入
    if profit_pct < 0:
        profit_pct = 0.0

    # 3. 激活判断
    min_profit = activation_config.get('min_profit_pct', 1.5)
    profit_activated = profit_pct >= min_profit
    tp1_activated = activation_config.get('also_on_tp1', True) and tp1_hit
    tp2_activated = activation_config.get('also_on_tp2', False) and tp2_hit

    if not profit_activated and not tp1_activated and not tp2_activated:
        # 如果已激活但浮盈回落，保持激活状态不退出
        if trailing_activated:
            pass
        else:
            return None

    # 4. 确定阶梯索引
    if not tiers:
        logger.warning("动态利润保护配置错误：regression_tiers 为空")
        return None

    first_tier_ceiling = float(tiers[0]['profit_ceiling'])

    # 保本模式：浮盈 < 第一阶梯上限 且 TP1 未触发
    if profit_pct < first_tier_ceiling and not tp1_hit:
        stop_price = entry_price
        tier_index = 0
        retrace_ratio = 0.0
    else:
        tier_index = -1
        for i, tier in enumerate(tiers):
            if profit_pct < float(tier['profit_ceiling']):
                tier_index = i
                break
        if tier_index == -1:
            tier_index = len(tiers) - 1

        retrace_ratio = float(tiers[tier_index]['retrace_ratio'])

        # 5. 计算阶梯回撤止损价
        stop_price = calculate_retrace_stop_price(
            direction, reference_price, entry_price, retrace_ratio, volatility_adj
        )

    # 6. 计算硬止损价（兜底）
    hard_stop_price = calculate_hard_stop_price(
        direction, entry_price, atr, stop_loss_atr_multiplier
    )

    # 最终止损价：做多取 MAX，做空取 MIN
    if direction == 'LONG':
        final_stop = max(stop_price, hard_stop_price)
    else:
        final_stop = min(stop_price, hard_stop_price)

    # 7. 单向移动保护
    final_stop = apply_one_way_protection(
        direction, final_stop, current_trailing_stop_price
    )

    # 8. 触发检查
    if direction == 'LONG':
        triggered = current_price <= final_stop
    else:
        triggered = current_price >= final_stop

    # 9. 返回结果
    return TrailingStopResult(
        trailing_stop_price=final_stop,
        trailing_activated=True,
        pending_profit_pct=profit_pct,
        current_tier_index=tier_index,
        triggered=triggered,
        tier_retrace_ratio=retrace_ratio,
        vol_adj=volatility_adj,
    )


async def get_volatility_adjustment(
    *,
    symbol: str,
    entry_price: Decimal,
    atr: Decimal,
    kline_service: Any,
    config: Dict[str, Any],
    cache: Dict[str, Any],
    extrinsic_logger: Any = None,
) -> float:
    """
    计算波动率调节因子

    基于历史日线 ATR 中位数，衡量当前币种的相对波动水平。
    波动率越高，调节因子越大，允许回撤比例越高。

    公式：
        当前 ATR% = 当前 ATR / 当前价格
        基准 ATR% 中位数 = 历史 N 日日线 ATR% 中位数
        波动率调节因子 = 当前 ATR% / 基准 ATR% 历史中位数

    Args:
        symbol: 交易对
        entry_price: 入场价格
        atr: 入场时的 ATR 值
        kline_service: KLineService 实例
        config: volatility_adjustment 配置节
        cache: 外部传入的缓存字典（按引用传递）
        extrinsic_logger: 可选的 logger 实例

    Returns:
        float: 波动率调节因子，clamp 到 [0.5, 2.0]，失败或关闭时返回 1.0
    """
    log = extrinsic_logger or logger

    if not config.get('enabled', True):
        return 1.0

    # 检查缓存
    cache_key = f"base_atr_pct_{symbol}"
    cache_ttl = config.get('cache_ttl_seconds', 3600)
    cached = cache.get(cache_key)
    if cached and (time.time() - cached['time'] < cache_ttl):
        return cached['value']

    try:
        lookback_days = config.get('atr_lookback_days', 30)
        atr_period = config.get('atr_period', 14)

        # 获取历史日线数据
        klines = await kline_service.get_klines(symbol, '1d', limit=lookback_days + atr_period + 10)
        if klines is None or len(klines) < lookback_days + atr_period:
            log.warning(f"{symbol} 历史日线数据不足，使用默认波动率调节因子 1.0")
            return 1.0

        # 计算日线 ATR 和 ATR%
        df = pd.DataFrame(klines)

        # 兼容 close/close_price、high/high_price、low/low_price 字段名
        close_col = 'close' if 'close' in df.columns else 'close_price'
        if close_col in df.columns:
            df['close'] = pd.to_numeric(df[close_col], errors='coerce')
        if 'high' in df.columns:
            df['high'] = pd.to_numeric(df['high'], errors='coerce')
        elif 'high_price' in df.columns:
            df['high'] = pd.to_numeric(df['high_price'], errors='coerce')
        if 'low' in df.columns:
            df['low'] = pd.to_numeric(df['low'], errors='coerce')
        elif 'low_price' in df.columns:
            df['low'] = pd.to_numeric(df['low_price'], errors='coerce')

        atr_series = TechnicalIndicators.calculate_atr(df, period=atr_period)
        atr_pct_series = atr_series / df['close']
        base_atr_pct = float(atr_pct_series.median())

        # 当前 ATR%
        current_price = entry_price if entry_price and entry_price > 0 else Decimal('1')
        current_atr_pct = float(atr / current_price)

        # 计算波动率调节因子
        min_adj = config.get('min_vol_adj', 0.5)
        max_adj = config.get('max_vol_adj', 2.0)
        vol_adj = current_atr_pct / base_atr_pct if base_atr_pct > 0 else 1.0
        vol_adj = max(min_adj, min(max_adj, vol_adj))

        # 缓存
        cache[cache_key] = {
            'value': vol_adj,
            'time': time.time(),
            'base_atr_pct': base_atr_pct,
            'current_atr_pct': current_atr_pct,
        }

        log.debug(
            f"{symbol} 波动率调节因子",
            base_atr_pct=round(base_atr_pct, 6),
            current_atr_pct=round(current_atr_pct, 6),
            vol_adj=round(vol_adj, 4)
        )

        return vol_adj

    except Exception as e:
        log.error(
            f"{symbol} 计算波动率调节因子失败",
            error=str(e),
            exc_info=True
        )
        return 1.0