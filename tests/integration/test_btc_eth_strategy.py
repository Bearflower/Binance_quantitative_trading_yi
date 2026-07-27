"""
BTC/ETH策略集成测试
测试策略的完整功能
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from strategies.btc_eth.strategy import BTCEthStrategy
from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from shared.notification import NotificationClient


def generate_mock_klines(count: int = 100, base_price: float = 50000.0):
    """
    生成模拟K线数据
    
    Args:
        count: K线数量
        base_price: 基础价格
    
    Returns:
        K线数据列表
    """
    klines = []
    price = base_price
    
    for i in range(count):
        # 模拟价格波动
        change = np.random.uniform(-0.02, 0.02)
        price = price * (1 + change)
        
        high = price * (1 + np.random.uniform(0, 0.01))
        low = price * (1 - np.random.uniform(0, 0.01))
        open_price = price * (1 + np.random.uniform(-0.005, 0.005))
        close_price = price
        volume = np.random.uniform(100, 1000)
        
        klines.append({
            'timestamp': int(datetime.now().timestamp() * 1000) - (count - i) * 3600000,
            'open': open_price,
            'high': high,
            'low': low,
            'close': close_price,
            'volume': volume
        })
    
    return klines


@pytest.fixture
def config():
    """测试配置（v6.16.10）"""
    return {
        'strategy': {
            'symbols': ['BTCUSDT', 'ETHUSDT'],
            'timeframes': ['1h', '4h', '1d'],
            'symbol_config': {},
            'risk': {
                'max_position_size': 0.1,
                'stop_loss_atr_multiplier': 1.5,
                'take_profit_atr_multiplier': 4.0,
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
                    'max_daily_total_trades': 6,
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
                'trend_filter': {
                    'enabled': False
                },
                'prohibition': {},
                'dynamic_atr': {
                    'enabled': False
                },
                'dynamic_volume': {
                    'enabled': False
                },
                'extreme_market': {},
                'liquidation_warning': {},
                'position_management': {
                    'max_concurrent_positions': 2,
                    'volatility_target_risk': 0
                },
                'market_state': {
                    'enabled': False,  # 禁用市场状态判断，让默认分析通过
                    'behaviors': {
                        'STRONG_TREND': {'min_grade': 'B'},
                        'RANGING': {'min_grade': 'B'}
                    }
                }
            },
            'scoring': {
                'min_score': 75,
                'grade_thresholds': {
                    'S': 90,
                    'A': 75,
                    'B': 65,
                    'C': 55
                },
                'weights': {
                    'trend_strength': 0.20,
                    'pattern_quality': 0.50,
                    'momentum_divergence': 0.30
                },
                'a_level_bonus': {
                    'rsi_low': 35,
                    'rsi_high': 65,
                    'bonus': 2
                },
                'trend_strength': {
                    'base_score': 50,
                    'consistency_bonus': 15,
                    'dual_uptrend_bonus': 10,
                    'macd_positive_bonus': 10,
                    'macd_negative_penalty': -10,
                    'adx_strong_threshold': 25,
                    'adx_strong_bonus': 15,
                    'adx_medium_threshold': 20,
                    'adx_medium_bonus': 8
                },
                'pattern_quality': {
                    'base_score': 50,
                    'bullish_engulfing_bonus': 20,
                    'bearish_engulfing_bonus': 20,
                    'breakout_pullback_bonus': 25,
                    'divergence_bonus': 30
                },
                'breakout_pullback': {
                    'proximity_pct': 0.01
                },
                'momentum_divergence': {
                    'base_score': 60,
                    'rsi_oversold': 30,
                    'rsi_overbought': 70,
                    'rsi_normal_bonus': 5,
                    'rsi_oversold_bonus': 20,
                    'rsi_overbought_penalty': -20,
                    'macd_hist_positive_bonus': 10,
                    'macd_hist_negative_penalty': -10,
                    'divergence_bonus': 15
                }
            }
        },
        'binance': {
            'leverage': {
                'S': 5,
                'A': 4,
                'B': 3,
                'C': 2
            },
            'position_ratio': {
                'S': 0.50,
                'A': 0.30,
                'B': 0.15,
                'C': 0.05
            },
            'order_optimization': {
                'use_limit_order': False,
                'use_buy_one_price': False,
                'use_sell_one_price': False
            }
        }
    }


@pytest.fixture
def mock_binance_client():
    """模拟币安客户端（v6.16.10）"""
    client = MagicMock(spec=BinanceClient)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    # 新增 mock：禁止入场条件所需的 API
    client.get_funding_rate = AsyncMock(return_value=0.0001)  # 正常资金费率
    client.get_ticker = AsyncMock(return_value={'lastPrice': '50000', 'priceChangePercent': '1.5'})
    client.get_orderbook = AsyncMock(return_value={'bids': [['49999', '1']], 'asks': [['50001', '1']]})
    client.get_account_info = AsyncMock(return_value={'availableBalance': '500', 'positions': []})
    client.get_ticker_price = AsyncMock(return_value='50000')
    client.get_symbol_info = AsyncMock(return_value={
        'quantityPrecision': 3,
        'pricePrecision': 2,
        'stepSize': '0.001',
        'tickSize': '0.01'
    })
    return client


@pytest.fixture
def mock_kline_service():
    """模拟K线服务"""
    service = MagicMock(spec=KLineService)
    service.__aenter__ = AsyncMock(return_value=service)
    service.__aexit__ = AsyncMock(return_value=None)
    
    # 模拟多时间框架数据
    async def get_multi_timeframe_data(symbol, intervals):
        data = {}
        for interval in intervals:
            # 生成上升趋势的K线数据
            klines = []
            base_price = 50000.0 if symbol == 'BTCUSDT' else 3000.0
            
            for i in range(100):
                # 模拟上升趋势
                price = base_price * (1 + i * 0.001)
                klines.append({
                    'timestamp': int(datetime.now().timestamp() * 1000) - (100 - i) * 3600000,
                    'open': price * 0.999,
                    'high': price * 1.001,
                    'low': price * 0.998,
                    'close': price,
                    'volume': 1000.0
                })
            
            data[interval] = klines
        
        return data
    
    service.get_multi_timeframe_data = get_multi_timeframe_data
    return service


@pytest.fixture
def mock_notification_client():
    """模拟通知客户端"""
    client = MagicMock(spec=NotificationClient)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.send_trade_notification = AsyncMock(return_value=True)
    client.send = AsyncMock(return_value=True)
    return client


@pytest.mark.asyncio
async def test_strategy_initialization(config, mock_binance_client, mock_kline_service, mock_notification_client):
    """测试策略初始化"""
    strategy = BTCEthStrategy(
        config=config,
        binance_client=mock_binance_client,
        kline_service=mock_kline_service,
        notification_client=mock_notification_client
    )
    
    assert strategy.symbols == ['BTCUSDT', 'ETHUSDT']
    assert strategy.timeframes == ['1h', '4h', '1d']
    assert strategy.risk_config == config['strategy']['risk']
    assert strategy.scoring_config == config['strategy']['scoring']


@pytest.mark.asyncio
async def test_strategy_analyze(config, mock_binance_client, mock_kline_service, mock_notification_client):
    """测试策略分析功能"""
    # 降低最低评分阈值以确保生成信号
    config['strategy']['scoring']['min_score'] = 50
    
    strategy = BTCEthStrategy(
        config=config,
        binance_client=mock_binance_client,
        kline_service=mock_kline_service,
        notification_client=mock_notification_client
    )
    
    signal = await strategy.analyze('BTCUSDT')
    
    # 验证信号格式
    assert signal is not None
    assert 'symbol' in signal
    assert 'direction' in signal
    assert 'grade' in signal
    assert 'score' in signal
    assert 'entry_price' in signal
    assert 'initial_stop_loss' in signal
    assert 'tp1_price' in signal
    assert 'leverage' in signal
    assert 'position_ratio' in signal
    
    # 验证信号值
    assert signal['symbol'] == 'BTCUSDT'
    assert signal['direction'] in ['LONG', 'SHORT']
    assert signal['grade'] in ['S', 'A', 'B', 'C']
    assert 0 <= signal['score'] <= 100
    assert signal['leverage'] > 0
    assert 0 < signal['position_ratio'] <= 1


@pytest.mark.asyncio
async def test_strategy_execute_signal(config, mock_binance_client, mock_kline_service, mock_notification_client):
    """测试策略执行信号"""
    strategy = BTCEthStrategy(
        config=config,
        binance_client=mock_binance_client,
        kline_service=mock_kline_service,
        notification_client=mock_notification_client
    )
    
    # 创建测试信号
    signal = {
        'symbol': 'BTCUSDT',
        'direction': 'LONG',
        'grade': 'A',
        'score': 85.0,
        'entry_price': 50000.0,
        'initial_stop_loss': 49000.0,
        'tp1_price': 52000.0,
        'leverage': 4,
        'position_ratio': 0.25,
        'quantity': 0.002,
        'atr': 500.0,
        'timestamp': datetime.now()
    }
    
    # 执行信号
    success = await strategy.execute_signal(signal)
    
    # 验证执行成功（交易通知已禁用，不再检查）
    assert success is True


@pytest.mark.asyncio
async def test_strategy_scoring_system(config, mock_binance_client, mock_kline_service, mock_notification_client):
    """测试评分系统"""
    strategy = BTCEthStrategy(
        config=config,
        binance_client=mock_binance_client,
        kline_service=mock_kline_service,
        notification_client=mock_notification_client
    )
    
    # 生成测试数据
    klines_data = {
        '1h': generate_mock_klines(100, 50000.0),
        '4h': generate_mock_klines(100, 50000.0)
    }
    
    # 计算技术指标
    from shared.indicators import TechnicalIndicators
    indicators = {}
    for timeframe, data in klines_data.items():
        df = pd.DataFrame(data)
        df['open'] = pd.to_numeric(df['open'], errors='coerce')
        df['high'] = pd.to_numeric(df['high'], errors='coerce')
        df['low'] = pd.to_numeric(df['low'], errors='coerce')
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        indicators[timeframe] = TechnicalIndicators.calculate_all(df)
    
    # 计算评分
    score = strategy._calculate_score(indicators, klines_data)
    
    # 验证评分范围
    assert 0 <= score <= 100
    
    # 验证各部分评分
    trend_score = strategy._calculate_trend_strength_score(indicators)
    assert 0 <= trend_score <= 100
    
    pattern_score = strategy._calculate_pattern_quality_score(indicators, klines_data)
    assert 0 <= pattern_score <= 100
    
    momentum_score = strategy._calculate_momentum_divergence_score(indicators, klines_data)
    assert 0 <= momentum_score <= 100


@pytest.mark.asyncio
async def test_strategy_grade_determination(config, mock_binance_client, mock_kline_service, mock_notification_client):
    """测试等级判定"""
    strategy = BTCEthStrategy(
        config=config,
        binance_client=mock_binance_client,
        kline_service=mock_kline_service,
        notification_client=mock_notification_client
    )
    
    # 测试各等级阈值（v6.16.10: S≥90, A≥75, B≥65, C≥55）
    assert strategy._determine_grade(95) == 'S'
    assert strategy._determine_grade(85) == 'A'
    assert strategy._determine_grade(75) == 'A'
    assert strategy._determine_grade(65) == 'B'


@pytest.mark.asyncio
async def test_strategy_direction_determination(config, mock_binance_client, mock_kline_service, mock_notification_client):
    """测试方向判定"""
    strategy = BTCEthStrategy(
        config=config,
        binance_client=mock_binance_client,
        kline_service=mock_kline_service,
        notification_client=mock_notification_client
    )
    
    # 生成上升趋势数据
    klines_data = {
        '1h': generate_mock_klines(100, 50000.0),
        '4h': generate_mock_klines(100, 50000.0)
    }
    
    # 计算技术指标
    from shared.indicators import TechnicalIndicators
    indicators = {}
    for timeframe, data in klines_data.items():
        df = pd.DataFrame(data)
        df['open'] = pd.to_numeric(df['open'], errors='coerce')
        df['high'] = pd.to_numeric(df['high'], errors='coerce')
        df['low'] = pd.to_numeric(df['low'], errors='coerce')
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        indicators[timeframe] = TechnicalIndicators.calculate_all(df)
    
    # 判定方向
    direction = strategy._determine_direction(indicators)
    
    # 验证方向
    assert direction in ['LONG', 'SHORT']


@pytest.mark.asyncio
async def test_strategy_error_handling(config, mock_binance_client, mock_notification_client):
    """测试错误处理"""
    # 创建会抛出异常的K线服务
    mock_kline_service = MagicMock(spec=KLineService)
    mock_kline_service.get_multi_timeframe_data = AsyncMock(side_effect=Exception("K线服务异常"))
    
    strategy = BTCEthStrategy(
        config=config,
        binance_client=mock_binance_client,
        kline_service=mock_kline_service,
        notification_client=mock_notification_client
    )
    
    # 分析应该返回结果字典而不是抛出异常（v6.16.10: analyze 始终返回 dict）
    result = await strategy.analyze('BTCUSDT')
    assert result is not None
    assert 'reason' in result
    assert '执行异常' in result['reason']


@pytest.mark.asyncio
async def test_strategy_min_score_filter(config, mock_binance_client, mock_kline_service, mock_notification_client):
    """测试最低评分过滤"""
    # 修改配置，提高最低评分阈值
    config['strategy']['scoring']['min_score'] = 95
    
    strategy = BTCEthStrategy(
        config=config,
        binance_client=mock_binance_client,
        kline_service=mock_kline_service,
        notification_client=mock_notification_client
    )
    
    # 分析市场
    result = await strategy.analyze('BTCUSDT')
    
    # v6.16.10: analyze 始终返回 dict，检查是否因评分不足被过滤
    assert result is not None
    if 'direction' in result:
        assert result['score'] >= 95
