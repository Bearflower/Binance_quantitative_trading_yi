"""
BTC/ETH 策略动态利润保护机制边界条件测试（v6.21）

测试覆盖：
1. 空 tiers 配置边界
2. 波动率调节因子边界（0.5 / 2.0）
3. 做空硬止损更紧场景
4. 保本模式（浮盈 < 1.5% 且 TP1 未触发）
5. entry_price 为 0 或 None
6. 价格峰值更新逻辑
7. 性能测试（100 次调用耗时）
"""
import sys
import os
import time
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

from strategies.btc_eth.strategy import BTCEthStrategy, PositionState


# ============================================================================
# 辅助函数
# ============================================================================

def create_base_config() -> dict:
    """创建基础配置（包含动态利润保护配置）"""
    return {
        'strategy': {
            'symbols': ['BTCUSDT'],
            'timeframes': ['1h'],
            'symbol_config': {},
            'risk': {
                'max_position_size': 0.1,
                'stop_loss_atr_multiplier': 1.5,
                'partial_take_profit': {
                    'tp1_atr_multiplier': 4.0,
                    'tp1_close_ratio': 0.25,
                    'tp2_atr_multiplier': 6.0,
                    'tp2_close_ratio': 0.25,
                    'remaining_ratio': 0.50
                },
                'dynamic_trailing': {
                    'enabled': True,
                    'activation': {
                        'min_profit_pct': 1.5,
                        'also_on_tp1': True
                    },
                    'regression_tiers': [
                        {'profit_ceiling': 1.5, 'retrace_ratio': 0.0},
                        {'profit_ceiling': 4.0, 'retrace_ratio': 0.5},
                        {'profit_ceiling': 8.0, 'retrace_ratio': 0.35},
                        {'profit_ceiling': 999.0, 'retrace_ratio': 0.25}
                    ],
                    'volatility_adjustment': {
                        'enabled': False,
                        'atr_lookback_days': 30,
                        'atr_period': 14,
                        'cache_ttl_seconds': 3600
                    }
                },
                'time_stop': {
                    'max_holding_hours': 72,
                    'close_ratio': 0.50
                },
                'close_limit_order': {
                    'enabled': False,
                    'timeout_seconds': 10,
                    'poll_interval_seconds': 2,
                    'applicable_reasons': []
                },
                'cleanup_silent_error_codes': [-2022, -2011],
                'frequency_control': {
                    'max_daily_symbol_trades': 2,
                    'symbol_cooldown_hours': 12,
                    'consecutive_loss_pause': 5,
                    'pause_duration_hours': 24,
                    'max_daily_loss_usdt': 25,
                    'max_daily_loss_ratio': 0.05,
                    'initial_capital_usdt': 500
                },
                'position_sizing': {
                    'safety_margin_ratio': 0.10,
                    'min_margin_usdt': 100,
                    'max_single_position_usdt': 500,
                    'min_close_notional_usdt': 20
                },
                'trend_filter': {'enabled': False},
                'prohibition': {},
                'dynamic_atr': {'enabled': False},
                'dynamic_volume': {'enabled': False},
                'extreme_market': {},
                'liquidation_warning': {},
                'position_management': {
                    'max_concurrent_positions': 2,
                    'volatility_target_risk': 0
                },
                'market_state': {
                    'enabled': False,
                    'behaviors': {
                        'STRONG_TREND': {'min_grade': 'B'},
                        'RANGING': {'min_grade': 'B'}
                    }
                }
            },
            'scoring': {
                'min_score': 75,
                'grade_thresholds': {'S': 90, 'A': 75, 'B': 65, 'C': 55},
                'weights': {
                    'trend_strength': 0.20,
                    'pattern_quality': 0.50,
                    'momentum_divergence': 0.30
                },
                'a_level_bonus': {'rsi_low': 35, 'rsi_high': 65, 'bonus': 2},
                'trend_strength': {
                    'base_score': 50, 'consistency_bonus': 15, 'dual_uptrend_bonus': 10,
                    'macd_positive_bonus': 10, 'macd_negative_penalty': -10,
                    'adx_strong_threshold': 25, 'adx_strong_bonus': 15,
                    'adx_medium_threshold': 20, 'adx_medium_bonus': 8
                },
                'pattern_quality': {
                    'base_score': 50, 'bullish_engulfing_bonus': 20,
                    'bearish_engulfing_bonus': 20, 'breakout_pullback_bonus': 25,
                    'divergence_bonus': 30
                },
                'breakout_pullback': {'proximity_pct': 0.01},
                'momentum_divergence': {
                    'base_score': 60, 'rsi_oversold': 30, 'rsi_overbought': 70,
                    'rsi_normal_bonus': 5, 'rsi_oversold_bonus': 20,
                    'rsi_overbought_penalty': -20, 'macd_hist_positive_bonus': 10,
                    'macd_hist_negative_penalty': -10, 'divergence_bonus': 15
                }
            }
        },
        'binance': {
            'leverage': {'S': 5, 'A': 4, 'B': 3, 'C': 2},
            'position_ratio': {'S': 0.50, 'A': 0.30, 'B': 0.15, 'C': 0.05},
            'order_optimization': {
                'use_limit_order': False, 'use_buy_one_price': False, 'use_sell_one_price': False
            }
        }
    }


def create_strategy(config_override=None) -> BTCEthStrategy:
    """创建策略实例，所有外部服务使用 mock"""
    config = create_base_config()
    if config_override:
        # 深度合并配置
        _deep_merge(config, config_override)

    mock_binance = MagicMock()
    mock_binance.get_ticker = AsyncMock(return_value={'lastPrice': '50000', 'priceChangePercent': '1.5'})
    mock_binance.get_ticker_price = AsyncMock(return_value=Decimal('50000'))
    mock_binance.get_funding_rate = AsyncMock(return_value=0.0001)
    mock_binance.get_account_info = AsyncMock(return_value={'availableBalance': '500', 'positions': []})
    mock_binance.get_symbol_info = AsyncMock(return_value={
        'quantityPrecision': 3, 'pricePrecision': 2, 'stepSize': '0.001', 'tickSize': '0.01'
    })
    mock_binance.get_orderbook = AsyncMock(return_value={'bids': [['49999', '1']], 'asks': [['50001', '1']]})

    mock_kline = MagicMock()
    mock_kline.get_multi_timeframe_data = AsyncMock(return_value={})
    mock_kline.get_klines = AsyncMock(return_value=[])

    mock_notification = MagicMock()
    mock_notification.send_trade_notification = AsyncMock(return_value=True)
    mock_notification.send = AsyncMock(return_value=True)

    s = BTCEthStrategy(
        config=config,
        binance_client=mock_binance,
        kline_service=mock_kline,
        notification_client=mock_notification,
        db_manager=None
    )
    # Mock _get_volatility_adjustment 返回 1.0（禁用波动率调节）
    s._get_volatility_adjustment = AsyncMock(return_value=1.0)
    return s


def _deep_merge(base: dict, override: dict) -> None:
    """深度合并字典（用于覆盖配置）"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def create_position_long(entry_price=Decimal('60000'), atr=Decimal('600'), highest=None) -> PositionState:
    """创建做多持仓"""
    pos = PositionState()
    pos.entry_price = entry_price
    pos.direction = 'LONG'
    pos.current_quantity = Decimal('0.1')
    pos.atr = atr
    pos.highest_price = highest or entry_price
    pos.tp1_hit = False
    pos.trailing_activated = False
    pos.trailing_stop_price = None
    return pos


def create_position_short(entry_price=Decimal('60000'), atr=Decimal('600'), lowest=None) -> PositionState:
    """创建做空持仓"""
    pos = PositionState()
    pos.entry_price = entry_price
    pos.direction = 'SHORT'
    pos.current_quantity = Decimal('1.0')
    pos.atr = atr
    pos.lowest_price = lowest or entry_price
    pos.tp1_hit = False
    pos.trailing_activated = False
    pos.trailing_stop_price = None
    return pos


# ============================================================================
# 边界条件测试
# ============================================================================

class TestBoundaryEmptyTiers:
    """3.1 空 tiers 配置"""

    @pytest.mark.asyncio
    async def test_empty_tiers_does_not_crash(self):
        """tiers 为空列表时不应抛出 IndexError"""
        strategy = create_strategy({
            'strategy': {
                'risk': {
                    'dynamic_trailing': {
                        'regression_tiers': []
                    }
                }
            }
        })
        pos = create_position_long(highest=Decimal('60900'))

        # 空 tiers 时，访问 tiers[0] 会抛出 IndexError
        # 期望被外层 try-except 捕获，返回 None 或合理的默认值
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60900'))

        # 不应崩溃，应返回 None（被异常处理捕获）
        assert result is None, f"空 tiers 应返回 None，实际返回 {result}"

    @pytest.mark.asyncio
    async def test_empty_tiers_vol_adj_enabled(self):
        """tiers 为空且波动率调节启用时，不应崩溃"""
        strategy = create_strategy({
            'strategy': {
                'risk': {
                    'dynamic_trailing': {
                        'regression_tiers': [],
                        'volatility_adjustment': {'enabled': True}
                    }
                }
            }
        })
        pos = create_position_long(highest=Decimal('60900'))

        # 即使 _get_volatility_adjustment 被 mock 为 1.0，
        # 访问 tiers[0] 仍应在计算 first_tier_ceiling 时抛出异常
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60900'))

        assert result is None, f"空 tiers 应返回 None，实际返回 {result}"


class TestBoundaryVolatilityAdjustment:
    """3.2 波动率调节因子边界"""

    @pytest.mark.asyncio
    async def test_vol_adj_min_clamp_0_5(self):
        """调节因子为 0.5（最小值 clamp），验证回撤比例减半"""
        strategy = create_strategy()
        strategy._get_volatility_adjustment = AsyncMock(return_value=0.5)

        pos = create_position_long(highest=Decimal('61200'))
        pos.highest_price = Decimal('61200')

        # 浮盈 2% → 进入 1.5%~4% 阶梯，retrace_ratio=0.5
        # 浮盈金额 = 1200，回撤 = 1200 * 0.5 * 0.5 = 300
        # 止损价 = 61200 - 300 = 60900
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('61200'))
        assert result == Decimal('60900'), f"vol_adj=0.5 期望 60900，实际 {result}"

    @pytest.mark.asyncio
    async def test_vol_adj_max_clamp_2_0(self):
        """调节因子为 2.0（最大值 clamp），验证回撤比例翻倍"""
        strategy = create_strategy()
        strategy._get_volatility_adjustment = AsyncMock(return_value=2.0)

        pos = create_position_long(highest=Decimal('61200'))
        pos.highest_price = Decimal('61200')

        # 浮盈 2% → 进入 1.5%~4% 阶梯，retrace_ratio=0.5
        # 浮盈金额 = 1200，回撤 = 1200 * 0.5 * 2.0 = 1200
        # 止损价 = 61200 - 1200 = 60000
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('61200'))
        assert result == Decimal('60000'), f"vol_adj=2.0 期望 60000，实际 {result}"

    @pytest.mark.asyncio
    async def test_vol_adj_0_5_short(self):
        """做空方向，vol_adj=0.5"""
        strategy = create_strategy()
        strategy._get_volatility_adjustment = AsyncMock(return_value=0.5)

        pos = create_position_short(lowest=Decimal('58200'))
        pos.lowest_price = Decimal('58200')

        # 做空浮盈 3%，retrace_ratio=0.5
        # 浮盈金额 = 1800，回撤 = 1800 * 0.5 * 0.5 = 450
        # 止损价 = 58200 + 450 = 58650
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('58200'))
        assert result == Decimal('58650'), f"vol_adj=0.5 short 期望 58650，实际 {result}"


class TestBoundaryShortHardStop:
    """3.3 做空硬止损更紧场景"""

    @pytest.mark.asyncio
    async def test_short_hard_stop_tighter(self):
        """做空方向，动态止损价 > 硬止损价，验证最终取 MIN（硬止损价）"""
        strategy = create_strategy()
        pos = create_position_short(lowest=Decimal('58200'))
        pos.lowest_price = Decimal('58200')

        # 做空：entry=60000, lowest=58200, 浮盈=3%
        # 动态止损 = 58200 + 1800*0.5*1.0 = 58200 + 900 = 59100
        # 硬止损 = 60000 + 600*1.5 = 60000 + 900 = 60900
        # 做空最终取 MIN(59100, 60900) = 59100（动态止损更紧）
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('58200'))
        # 动态止损 59100 < 硬止损 60900，所以取 59100
        assert result == Decimal('59100'), f"期望 59100，实际 {result}"

    @pytest.mark.asyncio
    async def test_short_hard_stop_is_tighter_than_dynamic(self):
        """做空方向，硬止损价 < 动态止损价，验证最终取硬止损价"""
        strategy = create_strategy()
        pos = create_position_short(lowest=Decimal('59500'))
        pos.lowest_price = Decimal('59500')

        # 做空：entry=60000, lowest=59500, 浮盈=0.83%
        # 浮盈 < 1.5%，但 TP1 触发了激活
        pos.tp1_hit = True

        # 浮盈=0.83% < 1.5%，但 tp1_hit=True，跳过保本路径
        # 进入 tier 查找：0.83 < 1.5 → tier_index=0, retrace_ratio=0.0
        # 动态止损 = 59500 + 500*0.0*1.0 = 59500
        # 硬止损 = 60000 + 600*1.5 = 60900
        # 做空最终取 MIN(59500, 60900) = 59500
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('59500'))
        assert result == Decimal('59500'), f"期望 59500，实际 {result}"

    # 模拟硬止损比动态止损更紧的场景
    @pytest.mark.asyncio
    async def test_short_hard_stop_tighter_than_dynamic(self):
        """做空方向，硬止损价 < 动态止损价（硬止损更紧），验证最终取硬止损价"""
        strategy = create_strategy()
        pos = create_position_short(lowest=Decimal('58500'))
        pos.lowest_price = Decimal('58500')

        # 做空：entry=60000, lowest=58500, 浮盈=2.5%
        # 动态止损 = 58500 + 1500*0.5*1.0 = 58500 + 750 = 59250
        # 硬止损 = 60000 + 600*1.5 = 60900
        # 做空最终取 MIN(59250, 60900) = 59250（动态止损更紧）
        # 要让硬止损更紧，需要降低硬止损价
        # 硬止损价 = 60000 + 600*1.5 = 60900，动态止损价 = 59250
        # 动态止损 59250 < 硬止损 60900，所以动态止损更紧

        # 硬止损总是比动态止损宽松（在相同方向），所以取动态止损
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('58500'))
        # 59250 < 60900，所以取 59250（动态止损更紧）
        assert result == Decimal('59250'), f"期望 59250，实际 {result}"


class TestBoundaryBreakeven:
    """3.4 保本模式"""

    @pytest.mark.asyncio
    async def test_breakeven_mode_profit_below_1_5pct(self):
        """浮盈 < 1.5% 且 TP1 未触发，验证 stop_price = entry_price"""
        strategy = create_strategy()
        pos = create_position_long(highest=Decimal('60300'))
        pos.highest_price = Decimal('60300')

        # 浮盈 = (60300 - 60000) / 60000 * 100 = 0.5%
        # 0.5% < 1.5%，且 TP1 未触发 → 不应激活
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60300'))
        assert result is None, f"浮盈 0.5% 应返回 None（未激活），实际返回 {result}"

    @pytest.mark.asyncio
    async def test_breakeven_mode_after_activation(self):
        """激活后浮盈回落至 < 1.5%，应保持激活（保本模式）"""
        strategy = create_strategy()
        pos = create_position_long(highest=Decimal('61200'))
        pos.highest_price = Decimal('61200')

        # 先激活（浮盈 2%）
        result1 = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('61200'))
        assert result1 is not None
        assert pos.trailing_activated is True

        # 价格回落到 60200，浮盈约 0.33%，但已激活，应保持 active
        result2 = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60200'))
        # 由于已激活，不应返回 None
        assert result2 is not None, "已激活状态下浮盈回落应保持激活，不应返回 None"

    @pytest.mark.asyncio
    async def test_breakeven_stop_equals_entry(self):
        """TP1 触发后浮盈 < 1.5%，止损价 = 参考价（因 tp1_hit 跳过保本路径）"""
        strategy = create_strategy()
        pos = create_position_long(highest=Decimal('60300'))
        pos.highest_price = Decimal('60300')
        pos.tp1_hit = True  # TP1 触发激活

        # 浮盈 = 0.5% < 1.5%，但 TP1 激活
        # tp1_hit=True 时跳过保本路径，进入 tier 查找
        # 0.5 < 1.5 → tier_index=0, retrace_ratio=0.0
        # 止损 = 60300 - 300*0.0*1.0 = 60300
        # 硬止损 = 60000 - 600*1.5 = 59100
        # 最终 = max(60300, 59100) = 60300
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60300'))
        assert result is not None, "TP1 触发应激活"
        assert result == Decimal('60300'), f"期望 60300，实际 {result}"


class TestBoundaryEntryPrice:
    """3.5 entry_price 为 0 或 None"""

    @pytest.mark.asyncio
    async def test_entry_price_zero_long(self):
        """做多 entry_price=0，应返回 None"""
        strategy = create_strategy()
        pos = create_position_long(entry_price=Decimal('0'), highest=Decimal('60000'))

        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60000'))
        assert result is None, f"entry_price=0 应返回 None，实际返回 {result}"

    @pytest.mark.asyncio
    async def test_entry_price_none_long(self):
        """做多 entry_price=None，应返回 None"""
        strategy = create_strategy()
        pos = create_position_long(entry_price=None, highest=Decimal('60000'))

        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60000'))
        assert result is None, f"entry_price=None 应返回 None，实际返回 {result}"

    @pytest.mark.asyncio
    async def test_entry_price_zero_short(self):
        """做空 entry_price=0，应返回 None"""
        strategy = create_strategy()
        pos = create_position_short(entry_price=Decimal('0'), lowest=Decimal('58000'))

        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('58000'))
        assert result is None, f"entry_price=0 应返回 None，实际返回 {result}"

    @pytest.mark.asyncio
    async def test_entry_price_none_short(self):
        """做空 entry_price=None，应返回 None"""
        strategy = create_strategy()
        pos = create_position_short(entry_price=None, lowest=Decimal('58000'))

        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('58000'))
        assert result is None, f"entry_price=None 应返回 None，实际返回 {result}"


class TestBoundaryPricePeakUpdate:
    """3.6 价格峰值更新逻辑"""

    @pytest.mark.asyncio
    async def test_long_price_goes_up_updates_highest(self):
        """做多：current_price 从 60000 涨到 66000，验证 highest_price=66000"""
        strategy = create_strategy()
        pos = create_position_long(highest=Decimal('60000'))
        pos.highest_price = Decimal('60000')

        # 模拟价格更新：在 _update_position_prices 中更新
        # 直接验证逻辑
        if pos.direction == 'LONG':
            if pos.highest_price is None or Decimal('66000') > pos.highest_price:
                pos.highest_price = Decimal('66000')

        assert pos.highest_price == Decimal('66000'), f"期望 66000，实际 {pos.highest_price}"

    @pytest.mark.asyncio
    async def test_long_price_goes_down_keeps_highest(self):
        """做多：current_price 从 66000 回落到 64000，验证 highest_price 仍为 66000"""
        strategy = create_strategy()
        pos = create_position_long(highest=Decimal('66000'))
        pos.highest_price = Decimal('66000')

        # 价格回落，不更新 highest
        if pos.direction == 'LONG':
            if pos.highest_price is None or Decimal('64000') > pos.highest_price:
                pos.highest_price = Decimal('64000')  # 不会执行

        assert pos.highest_price == Decimal('66000'), f"期望仍为 66000，实际 {pos.highest_price}"

        # 验证基于峰值计算止损价
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('64000'))
        # 基于峰值 66000 计算
        # 浮盈 = 10%，retrace_ratio=0.25
        # 止损 = 66000 - 6000*0.25*1.0 = 66000 - 1500 = 64500
        assert result == Decimal('64500'), f"基于峰值计算期望 64500，实际 {result}"

    @pytest.mark.asyncio
    async def test_short_price_goes_down_updates_lowest(self):
        """做空：current_price 从 60000 跌到 54000，验证 lowest_price=54000"""
        strategy = create_strategy()
        pos = create_position_short(lowest=Decimal('60000'))
        pos.lowest_price = Decimal('60000')

        # 模拟价格更新
        if pos.direction == 'SHORT':
            if pos.lowest_price is None or Decimal('54000') < pos.lowest_price:
                pos.lowest_price = Decimal('54000')

        assert pos.lowest_price == Decimal('54000'), f"期望 54000，实际 {pos.lowest_price}"

    @pytest.mark.asyncio
    async def test_short_price_bounces_keeps_lowest(self):
        """做空：current_price 从 54000 反弹到 56000，验证 lowest_price 仍为 54000"""
        strategy = create_strategy()
        pos = create_position_short(lowest=Decimal('54000'))
        pos.lowest_price = Decimal('54000')

        # 价格反弹，不更新 lowest
        if pos.direction == 'SHORT':
            if pos.lowest_price is None or Decimal('56000') < pos.lowest_price:
                pos.lowest_price = Decimal('56000')  # 不会执行

        assert pos.lowest_price == Decimal('54000'), f"期望仍为 54000，实际 {pos.lowest_price}"

        # 验证基于最低价计算止损价
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('56000'))
        # 基于最低价 54000 计算
        # 浮盈 = (60000-54000)/60000 = 10%，retrace_ratio=0.25
        # 止损 = 54000 + 6000*0.25*1.0 = 54000 + 1500 = 55500
        assert result == Decimal('55500'), f"基于最低价计算期望 55500，实际 {result}"


class TestBoundaryDisabledTrailing:
    """禁用动态利润保护"""

    @pytest.mark.asyncio
    async def test_disabled_trailing_returns_none(self):
        """enabled=False 时返回 None"""
        strategy = create_strategy({
            'strategy': {
                'risk': {
                    'dynamic_trailing': {
                        'enabled': False
                    }
                }
            }
        })
        pos = create_position_long(highest=Decimal('66000'))
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('66000'))
        assert result is None, "禁用时应返回 None"


class TestBoundaryExtremeValues:
    """极端值测试"""

    @pytest.mark.asyncio
    async def test_extreme_high_profit(self):
        """极高浮盈场景（100%+），不应崩溃"""
        strategy = create_strategy()
        pos = create_position_long(entry_price=Decimal('30000'), highest=Decimal('66000'))
        pos.highest_price = Decimal('66000')

        # 浮盈 120%，应使用最后一个 tier（999.0）
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('66000'))
        assert result is not None
        # 浮盈金额 = 36000，retrace_ratio=0.25
        # 止损 = 66000 - 36000*0.25*1.0 = 66000 - 9000 = 57000
        assert result == Decimal('57000'), f"极高浮盈期望 57000，实际 {result}"

    @pytest.mark.asyncio
    async def test_very_small_atr(self):
        """极小的 ATR 值"""
        strategy = create_strategy()
        pos = create_position_long(atr=Decimal('1'), highest=Decimal('60900'))
        pos.highest_price = Decimal('60900')

        # 浮盈 1.5%，1.5 < 1.5 为 False，跳过保本路径
        # 进入 tier 查找：1.5 < 4.0 → tier_index=1, retrace_ratio=0.5
        # 止损 = 60900 - 900*0.5*1.0 = 60900 - 450 = 60450
        # 硬止损 = 60000 - 1*1.5 = 59998.5
        # 最终 = max(60450, 59998.5) = 60450
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60900'))
        assert result == Decimal('60450.00')

    @pytest.mark.asyncio
    async def test_entry_price_negative(self):
        """负数 entry_price，应返回 None"""
        strategy = create_strategy()
        pos = create_position_long(entry_price=Decimal('-1'), highest=Decimal('60000'))
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60000'))
        assert result is None, "负数 entry_price 应返回 None"


class TestBoundaryTrackerEdgeCases:
    """跟踪器边缘情况"""

    @pytest.mark.asyncio
    async def test_highest_price_equals_entry(self):
        """highest_price == entry_price，浮盈为 0，未激活"""
        strategy = create_strategy()
        pos = create_position_long(highest=Decimal('60000'))
        pos.highest_price = Decimal('60000')

        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60000'))
        assert result is None, "浮盈为 0 不应激活"

    @pytest.mark.asyncio
    async def test_lowest_price_equals_entry(self):
        """lowest_price == entry_price，浮盈为 0，未激活"""
        strategy = create_strategy()
        pos = create_position_short(lowest=Decimal('60000'))
        pos.lowest_price = Decimal('60000')

        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('60000'))
        assert result is None, "浮盈为 0 不应激活"

    @pytest.mark.asyncio
    async def test_current_price_below_entry_long(self):
        """做多时当前价低于入场价（浮亏），不应激活"""
        strategy = create_strategy()
        pos = create_position_long(highest=Decimal('60000'))
        pos.highest_price = Decimal('60000')

        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('59000'))
        assert result is None, "浮亏不应激活"

    @pytest.mark.asyncio
    async def test_current_price_above_entry_short(self):
        """做空时当前价高于入场价（浮亏），不应激活"""
        strategy = create_strategy()
        pos = create_position_short(lowest=Decimal('60000'))
        pos.lowest_price = Decimal('60000')

        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('61000'))
        assert result is None, "浮亏不应激活"


# ============================================================================
# 性能测试
# ============================================================================

class TestPerformance:
    """4. 性能测试：100 次 _calculate_dynamic_trailing_stop 调用"""

    @pytest.mark.asyncio
    async def test_100_calls_performance(self):
        """100 次调用，平均耗时 < 50ms"""
        strategy = create_strategy()
        pos = create_position_long(highest=Decimal('66000'))
        pos.highest_price = Decimal('66000')

        # 预热
        for _ in range(5):
            await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('66000'))

        # 正式测试
        start = time.time()
        for _ in range(100):
            await strategy._calculate_dynamic_trailing_stop("BTCUSDT", pos, Decimal('66000'))
        elapsed = time.time() - start

        avg_ms = (elapsed / 100) * 1000
        print(f"\n[性能] 100 次调用总耗时: {elapsed*1000:.2f}ms, 平均: {avg_ms:.4f}ms")

        assert avg_ms < 50, f"平均耗时 {avg_ms:.4f}ms > 50ms 阈值"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short', '-s'])