"""
新币做空策略 K线服务相关逻辑测试
测试策略中与K线服务交互的核心逻辑：
1. 策略初始化时K线服务验证
2. 自动注册新币种到K线服务
3. 获取K线数据进行分析
4. ATR计算（executor.py中的_calculate_atr）
5. 从K线服务注销过期/已入场币种
6. 无效币种缓存
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, PropertyMock
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from strategies.new_coin.strategy import NewCoinStrategy
from strategies.new_coin.executor import TradingExecutor
from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.database import DatabaseManager


# ── 测试数据 ──────────────────────────────────────────────────────────

# 20根K线，每根 high-low=6，确保ATR可稳定计算
MOCK_KLINE_DATA = [
    {"open_time": 1700000000000 + i * 3600000, "open": "100.0", "high": "106.0", "low": "100.0",
     "close": "103.0", "volume": "1000.0", "close_time": 1700003600000 + i * 3600000,
     "quote_volume": "100000.0", "trade_count": 500}
    for i in range(20)
]

# 少量K线数据（不足ATR计算所需）
MOCK_KLINE_FEW = MOCK_KLINE_DATA[:3]

# 空K线数据
MOCK_KLINE_EMPTY = []

# 用于analyze测试的K线数据（含价格变化）
MOCK_KLINE_ANALYZE = [
    {"open_time": 1700000000000 + i * 3600000, "open": str(100 + i * 0.5), "high": str(105 + i * 0.5),
     "low": str(95 + i * 0.5), "close": str(102 + i * 0.5), "volume": "1000.0",
     "close_time": 1700003600000 + i * 3600000, "quote_volume": "100000.0", "trade_count": 500}
    for i in range(200)
]


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def config():
    """测试配置"""
    return {
        'strategy': {
            'name': 'new_coin_test',
            'version': '1.0.0',
            'db_strategy_name': '新币做空策略测试'
        },
        'scoring': {
            'weights': {
                'contract': 0.45,
                'oi_volume_ratio': 0.30,
                'oi_rank': 0.15,
                'technical': 0.35,
                'sentiment': 0.20
            },
            'entry_threshold': 5.0,
            'veto_thresholds': {
                'listing_hours': 48
            },
            'rebound_check': {
                'enabled': False
            },
            'sentiment': {
                'oi_change': {'lookback_hours': 3},
                'degraded_mode': {'enabled': True, 'listing_hours_threshold': 3}
            }
        },
        'trading': {
            'leverage': 2,
            'max_positions': 3,
            'single_position_margin': 50,
            'stop_loss_percent': 0.05,
            'take_profit_percent': 0.10,
            'batch_take_profit': {
                'enabled': True,
                'target1_atr_multiplier': 1.5,
                'target1_close_percent': 0.30,
                'target2_atr_multiplier': 3.5,
                'target2_close_percent': 0.40,
                'trailing_stop_atr_multiplier': 1.5
            },
            'time_stop': {'enabled': True, 'max_holding_hours': 72},
            'emergency_stop': {'enabled': True, 'check_minutes': 15, 'trigger_percent': 0.015},
            'atr_stop': {'multiplier': 2.5},
            'limit_order_slippage': 0.001,
            'default_precision': {'tick_size': '0.01', 'step_size': '0.001'},
            'close_position': {'close_percent': 1.0},
            'consecutive_loss': {'enabled': True, 'max_consecutive_losses': 3, 'pause_hours': 48},
            'max_drawdown': {'threshold': 0.15, 'pause_days': 7},
            'blacklist': {'enabled': False}
        },
        'pattern': {
            'three_tops': {'max_deviation': 0.02, 'score_high': 4.0, 'score_medium': 2.0},
            'long_upper_shadow': {'ratio_threshold': 2.0, 'score_high': 3.0, 'score_medium': 2.0},
            'volume_divergence': {'volume_ratio_threshold': 1.5, 'price_change_threshold': 0.02, 'score_high': 3.0, 'score_medium': 2.0}
        },
        'detector': {
            'check_interval': 300,
            'min_listing_hours': 1,
            'max_listing_hours': 48
        },
        'kline': {
            'interval': '1h',
            'limit': 200,
            'min_klines_for_analysis': 14,
            'atr_period': 14,
            'data_delay_seconds': 10
        }
    }


@pytest.fixture
def mock_binance_client():
    """模拟币安客户端"""
    client = Mock(spec=BinanceClient)
    client._request = AsyncMock(return_value={'symbols': []})
    client.get_account_balance = AsyncMock(return_value={'USDT': Decimal('1000')})
    client.get_position = AsyncMock(return_value=[])
    client.place_order = AsyncMock(return_value={'orderId': '12345', 'status': 'FILLED'})
    client.get_ticker = AsyncMock(return_value={'lastPrice': '100.0'})
    client.get_orderbook = AsyncMock(return_value={'bids': [['100.0', '1.0']]})
    client.get_open_orders = AsyncMock(return_value=[])
    client.cancel_order = AsyncMock()
    client.cancel_algo_order = AsyncMock()
    client.place_conditional_order = AsyncMock(return_value={'algoId': 'algo_test_001'})
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_kline_service():
    """模拟K线服务"""
    service = Mock(spec=KLineService)
    service.get_klines = AsyncMock(return_value=MOCK_KLINE_DATA)
    service.register_symbol = AsyncMock(return_value=True)
    service.unregister_symbol = AsyncMock(return_value=True)
    service.close = AsyncMock()
    return service


@pytest.fixture
def mock_notification_client():
    """模拟通知客户端"""
    client = Mock(spec=NotificationClient)
    client.send = AsyncMock(return_value=True)
    client.send_alert = AsyncMock(return_value=True)
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_db():
    """模拟数据库管理器"""
    db = Mock(spec=DatabaseManager)
    db.connect = AsyncMock()
    db.disconnect = AsyncMock()
    db.execute = AsyncMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_empty_kline_service():
    """模拟K线服务（返回空数据）"""
    service = Mock(spec=KLineService)
    service.get_klines = AsyncMock(return_value=[])
    service.register_symbol = AsyncMock(return_value=True)
    service.unregister_symbol = AsyncMock(return_value=True)
    service.close = AsyncMock()
    return service


@pytest.fixture
def strategy(config, mock_binance_client, mock_kline_service, mock_notification_client, mock_db):
    """已初始化的策略实例"""
    s = NewCoinStrategy(config)
    # 直接设置属性，绕过类型检查（避免构造真实实例）
    s.binance_client = mock_binance_client
    s.kline_service = mock_kline_service
    s.notification_client = mock_notification_client
    s.db = mock_db
    # 手动初始化内部模块
    from strategies.new_coin.scoring_engine import ScoringEngine
    from strategies.new_coin.pattern import PatternRecognizer
    from strategies.new_coin.detector import ListingDetector
    s.scoring_engine = ScoringEngine(config)
    s.pattern_recognizer = PatternRecognizer(config)
    s.listing_detector = ListingDetector(
        binance_api=mock_binance_client, db=mock_db, config=config
    )
    s.trading_executor = TradingExecutor(
        binance_api=mock_binance_client, db=mock_db,
        notification=mock_notification_client, config=config,
        kline_service=mock_kline_service
    )
    s._running = False
    return s


# ── 1. 策略初始化时K线服务验证 ──────────────────────────────────────

class TestStrategyInitialization:
    """测试策略初始化时K线服务验证"""

    @pytest.mark.asyncio
    async def test_initialize_success(self, config, mock_binance_client, mock_kline_service,
                                       mock_notification_client, mock_db):
        """正确创建NewCoinStrategy并设置kline_service后初始化成功"""
        s = NewCoinStrategy(config)
        s.binance_client = mock_binance_client
        s.kline_service = mock_kline_service
        s.notification_client = mock_notification_client
        s.db = mock_db

        await s.initialize()

        assert s.scoring_engine is not None
        assert s.pattern_recognizer is not None
        assert s.listing_detector is not None
        assert s.trading_executor is not None
        assert s.trading_executor.kline_service is not None

    @pytest.mark.asyncio
    async def test_initialize_without_kline_service_raises(self, config, mock_binance_client,
                                                            mock_notification_client, mock_db):
        """kline_service未设置时initialize应抛出异常"""
        s = NewCoinStrategy(config)
        s.binance_client = mock_binance_client
        s.kline_service = None
        s.notification_client = mock_notification_client
        s.db = mock_db

        with pytest.raises(ValueError, match="K线服务未设置"):
            await s.initialize()

    @pytest.mark.asyncio
    async def test_initialize_without_binance_client_raises(self, config, mock_kline_service,
                                                             mock_notification_client, mock_db):
        """binance_client未设置时initialize应抛出异常"""
        s = NewCoinStrategy(config)
        s.binance_client = None
        s.kline_service = mock_kline_service
        s.notification_client = mock_notification_client
        s.db = mock_db

        with pytest.raises(ValueError, match="币安客户端未设置"):
            await s.initialize()


# ── 2. 自动注册新币种到K线服务 ──────────────────────────────────────

class TestAutoRegisterSymbol:
    """测试自动注册新币种到K线服务"""

    @pytest.mark.asyncio
    async def test_register_new_coin_success(self, strategy):
        """发现新币种后成功注册到K线服务"""
        # 模拟 listing_detector 返回新币
        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'NEWCOINUSDT'}
        ])
        # 模拟其他内部方法为空
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        # 确保没有注册过
        strategy._registered_symbols = set()
        # 确保drawdown没有熔断
        strategy.drawdown_pause_until = None

        await strategy._execute_cycle()

        strategy.kline_service.register_symbol.assert_awaited_once_with(
            'NEWCOINUSDT', intervals=['1h']
        )
        assert 'NEWCOINUSDT' in strategy._registered_symbols

    @pytest.mark.asyncio
    async def test_register_failure_continues(self, strategy):
        """注册失败（K线服务不可用），策略应能继续运行但标记"""
        strategy.kline_service.register_symbol = AsyncMock(return_value=False)
        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'FAILCOINUSDT'}
        ])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy._registered_symbols = set()
        strategy.drawdown_pause_until = None

        # 不应抛出异常
        await strategy._execute_cycle()

        strategy.kline_service.register_symbol.assert_awaited_once_with(
            'FAILCOINUSDT', intervals=['1h']
        )
        # 注册失败，不应加入_registered_symbols
        assert 'FAILCOINUSDT' not in strategy._registered_symbols

    @pytest.mark.asyncio
    async def test_register_duplicate_skipped(self, strategy):
        """已注册的标的跳过重复注册"""
        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'DUPLICATEUSDT'},
            {'symbol': 'DUPLICATEUSDT'},  # 重复出现
        ])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy._registered_symbols = {'DUPLICATEUSDT'}  # 已注册
        strategy.drawdown_pause_until = None

        await strategy._execute_cycle()

        # register_symbol 不应被调用（已注册）
        strategy.kline_service.register_symbol.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_multiple_coins(self, strategy):
        """注册多个币种"""
        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'COIN1USDT'},
            {'symbol': 'COIN2USDT'},
            {'symbol': 'COIN3USDT'},
        ])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy._registered_symbols = set()
        strategy.drawdown_pause_until = None

        await strategy._execute_cycle()

        assert strategy.kline_service.register_symbol.await_count == 3
        assert strategy._registered_symbols == {'COIN1USDT', 'COIN2USDT', 'COIN3USDT'}

    @pytest.mark.asyncio
    async def test_register_exception_handled(self, strategy):
        """注册时K线服务抛异常，策略应能继续运行"""
        strategy.kline_service.register_symbol = AsyncMock(
            side_effect=ConnectionError("服务不可用")
        )
        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'ERRCOINUSDT'}
        ])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy._registered_symbols = set()
        strategy.drawdown_pause_until = None

        # 不应抛出异常
        await strategy._execute_cycle()

        # 注册异常，不应加入_registered_symbols
        assert 'ERRCOINUSDT' not in strategy._registered_symbols


# ── 3. 获取K线数据进行分析 ──────────────────────────────────────────

class TestFetchKlinesForAnalysis:
    """测试获取K线数据进行分析"""

    @pytest.mark.asyncio
    async def test_analyze_with_klines_success(self, strategy, mock_binance_client):
        """从K线服务成功获取K线数据进行分析"""
        # 准备K线数据
        strategy.kline_service.get_klines = AsyncMock(return_value=MOCK_KLINE_ANALYZE)

        # Mock exchangeInfo 返回币种信息
        mock_binance_client._request = AsyncMock()
        async def mock_request(method, endpoint, params=None, signed=False):
            if endpoint == '/fapi/v1/exchangeInfo':
                return {
                    'symbols': [{
                        'symbol': 'ANALYZEUSDT',
                        'baseAsset': 'ANALYZE',
                        'quoteAsset': 'USDT',
                        'onboardDate': int(datetime.now().timestamp() * 1000) - 7200000,  # 2小时前
                        'status': 'TRADING'
                    }]
                }
            elif endpoint == '/fapi/v1/openInterest':
                return {'openInterest': '1000000'}
            elif endpoint == '/fapi/v1/ticker/24hr':
                return {'quoteAssetVolume': '20000000'}
            elif endpoint == '/fapi/v1/fundingRate':
                return [{'fundingRate': '0.0001'}]
            elif endpoint == '/futures/data/openInterestHist':
                return [{'sumOpenInterest': '500000'}]
            return {}
        mock_binance_client._request.side_effect = mock_request

        result = await strategy.analyze('ANALYZEUSDT')

        # 验证K线服务被正确调用
        strategy.kline_service.get_klines.assert_awaited_once_with(
            symbol='ANALYZEUSDT', interval='1h', limit=200
        )
        # 验证分析结果
        assert result['symbol'] == 'ANALYZEUSDT'
        assert 'score_result' in result
        assert 'patterns' in result
        assert 'market_data' in result

    @pytest.mark.asyncio
    async def test_analyze_empty_klines_skipped(self, strategy, mock_binance_client):
        """K线服务返回空数据，策略应能跳过该币种"""
        strategy.kline_service.get_klines = AsyncMock(return_value=[])
        # 需要mock exchangeInfo才能通过_get_coin_info
        mock_binance_client._request = AsyncMock(
            return_value={
                'symbols': [{
                    'symbol': 'EMPTYUSDT',
                    'baseAsset': 'EMPTY',
                    'quoteAsset': 'USDT',
                    'onboardDate': int(datetime.now().timestamp() * 1000) - 7200000,
                    'status': 'TRADING'
                }]
            }
        )

        result = await strategy.analyze('EMPTYUSDT')

        assert result['symbol'] == 'EMPTYUSDT'
        assert result.get('error') == 'K线数据不足'

    @pytest.mark.asyncio
    async def test_analyze_insufficient_klines_skipped(self, strategy, mock_binance_client):
        """K线服务返回的数据不足最低要求，策略应跳过"""
        # 配置要求至少14根K线，只返回3根
        strategy.kline_service.get_klines = AsyncMock(return_value=MOCK_KLINE_FEW)
        # 需要mock exchangeInfo才能通过_get_coin_info
        mock_binance_client._request = AsyncMock(
            return_value={
                'symbols': [{
                    'symbol': 'FEWUSDT',
                    'baseAsset': 'FEW',
                    'quoteAsset': 'USDT',
                    'onboardDate': int(datetime.now().timestamp() * 1000) - 7200000,
                    'status': 'TRADING'
                }]
            }
        )

        result = await strategy.analyze('FEWUSDT')

        assert result['symbol'] == 'FEWUSDT'
        assert result.get('error') == 'K线数据不足'

    @pytest.mark.asyncio
    async def test_analyze_kline_service_exception_handled(self, strategy):
        """K线服务异常时，analyze应返回错误结果"""
        strategy.kline_service.get_klines = AsyncMock(
            side_effect=Exception("K线服务异常")
        )

        result = await strategy.analyze('ERRUSDT')

        assert result['symbol'] == 'ERRUSDT'
        assert 'error' in result


# ── 4. ATR计算（executor.py中的_calculate_atr） ──────────────────────

class TestCalculateATR:
    """测试executor.py中的_calculate_atr方法"""

    @pytest.mark.asyncio
    async def test_calculate_atr_normal(self, config, mock_binance_client, mock_kline_service,
                                         mock_notification_client, mock_db):
        """正常K线数据可以正确计算ATR"""
        executor = TradingExecutor(
            binance_api=mock_binance_client, db=mock_db,
            notification=mock_notification_client, config=config,
            kline_service=mock_kline_service
        )
        # 20根K线，high-low=6，TR=6，ATR=6
        atr = await executor._calculate_atr('TESTUSDT')

        assert atr > 0
        # 预期ATR = 6.0（因为每根K线的high-low=6，且prev_close在范围内）
        assert atr == Decimal('6')

    @pytest.mark.asyncio
    async def test_calculate_atr_insufficient_data(self, config, mock_binance_client,
                                                    mock_notification_client, mock_db):
        """K线数据不足（少于period+1），ATR返回0"""
        kline_service = Mock(spec=KLineService)
        kline_service.get_klines = AsyncMock(return_value=MOCK_KLINE_FEW)  # 只有3根
        kline_service.close = AsyncMock()

        executor = TradingExecutor(
            binance_api=mock_binance_client, db=mock_db,
            notification=mock_notification_client, config=config,
            kline_service=kline_service
        )

        atr = await executor._calculate_atr('TESTUSDT')

        assert atr == Decimal('0')

    @pytest.mark.asyncio
    async def test_calculate_atr_no_kline_service(self, config, mock_binance_client,
                                                   mock_notification_client, mock_db):
        """K线服务未设置，ATR返回0（不崩溃）"""
        executor = TradingExecutor(
            binance_api=mock_binance_client, db=mock_db,
            notification=mock_notification_client, config=config,
            kline_service=None  # 未设置K线服务
        )

        atr = await executor._calculate_atr('TESTUSDT')

        assert atr == Decimal('0')

    @pytest.mark.asyncio
    async def test_calculate_atr_empty_klines(self, config, mock_binance_client,
                                               mock_notification_client, mock_db):
        """K线服务返回空列表，ATR返回0"""
        kline_service = Mock(spec=KLineService)
        kline_service.get_klines = AsyncMock(return_value=[])
        kline_service.close = AsyncMock()

        executor = TradingExecutor(
            binance_api=mock_binance_client, db=mock_db,
            notification=mock_notification_client, config=config,
            kline_service=kline_service
        )

        atr = await executor._calculate_atr('TESTUSDT')

        assert atr == Decimal('0')

    @pytest.mark.asyncio
    async def test_calculate_atr_service_exception(self, config, mock_binance_client,
                                                    mock_notification_client, mock_db):
        """K线服务异常，ATR返回0（容错）"""
        kline_service = Mock(spec=KLineService)
        kline_service.get_klines = AsyncMock(
            side_effect=Exception("K线服务异常")
        )
        kline_service.close = AsyncMock()

        executor = TradingExecutor(
            binance_api=mock_binance_client, db=mock_db,
            notification=mock_notification_client, config=config,
            kline_service=kline_service
        )

        atr = await executor._calculate_atr('TESTUSDT')

        assert atr == Decimal('0')

    @pytest.mark.asyncio
    async def test_calculate_atr_custom_period(self, config, mock_binance_client,
                                                mock_notification_client, mock_db):
        """使用自定义ATR周期"""
        kline_service = Mock(spec=KLineService)
        # 返回5根K线，period=3需要4根，足够
        kline_data = MOCK_KLINE_DATA[:5]
        kline_service.get_klines = AsyncMock(return_value=kline_data)
        kline_service.close = AsyncMock()

        executor = TradingExecutor(
            binance_api=mock_binance_client, db=mock_db,
            notification=mock_notification_client, config=config,
            kline_service=kline_service
        )

        # 在配置中设置atr_period=3
        config['kline']['atr_period'] = 3

        atr = await executor._calculate_atr('TESTUSDT')

        # 验证 request 的 limit = period + 1 = 4
        kline_service.get_klines.assert_awaited_once_with(
            symbol='TESTUSDT', interval='1h', limit=4
        )
        assert atr == Decimal('6')


# ── 5. 从K线服务注销过期/已入场币种 ──────────────────────────────────

class TestUnregisterSymbol:
    """测试从K线服务注销币种"""

    @pytest.mark.asyncio
    async def test_unregister_after_successful_entry(self, strategy, mock_binance_client):
        """入场成功后从K线服务注销"""
        # 模拟 execute_cycle 中的入场流程
        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'ENTRYCOINUSDT'}
        ])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy._registered_symbols = set()
        strategy.drawdown_pause_until = None

        # 模拟交易所信息返回（让analyze通过时间检查）
        async def mock_request(method, endpoint, params=None, signed=False):
            if endpoint == '/fapi/v1/exchangeInfo':
                return {
                    'symbols': [{
                        'symbol': 'ENTRYCOINUSDT',
                        'baseAsset': 'ENTRY',
                        'quoteAsset': 'USDT',
                        'onboardDate': int(datetime.now().timestamp() * 1000) - 7200000,
                        'status': 'TRADING'
                    }]
                }
            elif endpoint == '/fapi/v1/openInterest':
                return {'openInterest': '1000000'}
            elif endpoint == '/fapi/v1/ticker/24hr':
                return {'quoteAssetVolume': '20000000'}
            elif endpoint == '/fapi/v1/fundingRate':
                return [{'fundingRate': '0.0001'}]
            elif endpoint == '/futures/data/openInterestHist':
                return [{'sumOpenInterest': '500000'}]
            return {}
        mock_binance_client._request.side_effect = mock_request

        # 模拟scoring_engine.should_entry返回True（入场）
        strategy.scoring_engine.should_entry = Mock(return_value=True)

        # 模拟execute_signal返回True
        strategy.execute_signal = AsyncMock(return_value=True)

        await strategy._execute_cycle()

        # 验证入场后调用了unregister_symbol
        strategy.kline_service.unregister_symbol.assert_awaited_with('ENTRYCOINUSDT')

    @pytest.mark.asyncio
    async def test_unregister_skip_too_long(self, strategy, mock_binance_client):
        """上线时间过长的币种从K线服务注销"""
        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'OLDCOINUSDT'}
        ])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy._registered_symbols = set()
        strategy.drawdown_pause_until = None
        # 确保known_symbols不包含该币种
        strategy.listing_detector.known_symbols = set()
        strategy.listing_detector._save_known_symbols = AsyncMock()

        # 模拟交易所信息返回（上线时间超过48小时）
        async def mock_request(method, endpoint, params=None, signed=False):
            if endpoint == '/fapi/v1/exchangeInfo':
                return {
                    'symbols': [{
                        'symbol': 'OLDCOINUSDT',
                        'baseAsset': 'OLD',
                        'quoteAsset': 'USDT',
                        'onboardDate': int(datetime.now().timestamp() * 1000) - 180000000,  # 50小时前
                        'status': 'TRADING'
                    }]
                }
            elif endpoint == '/fapi/v1/openInterest':
                return {'openInterest': '1000000'}
            elif endpoint == '/fapi/v1/ticker/24hr':
                return {'quoteAssetVolume': '20000000'}
            elif endpoint == '/fapi/v1/fundingRate':
                return [{'fundingRate': '0.0001'}]
            elif endpoint == '/futures/data/openInterestHist':
                return [{'sumOpenInterest': '500000'}]
            return {}
        mock_binance_client._request.side_effect = mock_request

        await strategy._execute_cycle()

        strategy.kline_service.unregister_symbol.assert_awaited_with('OLDCOINUSDT')
        # 应加入known_symbols
        assert 'OLDCOINUSDT' in strategy.listing_detector.known_symbols

    @pytest.mark.asyncio
    async def test_unregister_failure_does_not_affect_strategy(self, strategy, mock_binance_client):
        """注销失败时不影响策略运行"""
        strategy.kline_service.unregister_symbol = AsyncMock(return_value=False)

        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'OLDCOINUSDT'}
        ])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy._registered_symbols = set()
        strategy.drawdown_pause_until = None
        strategy.listing_detector.known_symbols = set()
        strategy.listing_detector._save_known_symbols = AsyncMock()

        async def mock_request(method, endpoint, params=None, signed=False):
            if endpoint == '/fapi/v1/exchangeInfo':
                return {
                    'symbols': [{
                        'symbol': 'OLDCOINUSDT',
                        'baseAsset': 'OLD',
                        'quoteAsset': 'USDT',
                        'onboardDate': int(datetime.now().timestamp() * 1000) - 180000000,
                        'status': 'TRADING'
                    }]
                }
            elif endpoint == '/fapi/v1/openInterest':
                return {'openInterest': '1000000'}
            elif endpoint == '/fapi/v1/ticker/24hr':
                return {'quoteAssetVolume': '20000000'}
            elif endpoint == '/fapi/v1/fundingRate':
                return [{'fundingRate': '0.0001'}]
            elif endpoint == '/futures/data/openInterestHist':
                return [{'sumOpenInterest': '500000'}]
            return {}
        mock_binance_client._request.side_effect = mock_request

        # 不应抛出异常
        await strategy._execute_cycle()

        strategy.kline_service.unregister_symbol.assert_awaited_with('OLDCOINUSDT')

    @pytest.mark.asyncio
    async def test_unregister_exception_handled(self, strategy, mock_binance_client):
        """注销时K线服务异常，不影响策略运行"""
        strategy.kline_service.unregister_symbol = AsyncMock(
            side_effect=Exception("连接异常")
        )

        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[
            {'symbol': 'OLDCOINUSDT'}
        ])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy._registered_symbols = set()
        strategy.drawdown_pause_until = None
        strategy.listing_detector.known_symbols = set()
        strategy.listing_detector._save_known_symbols = AsyncMock()

        async def mock_request(method, endpoint, params=None, signed=False):
            if endpoint == '/fapi/v1/exchangeInfo':
                return {
                    'symbols': [{
                        'symbol': 'OLDCOINUSDT',
                        'baseAsset': 'OLD',
                        'quoteAsset': 'USDT',
                        'onboardDate': int(datetime.now().timestamp() * 1000) - 180000000,
                        'status': 'TRADING'
                    }]
                }
            elif endpoint == '/fapi/v1/openInterest':
                return {'openInterest': '1000000'}
            elif endpoint == '/fapi/v1/ticker/24hr':
                return {'quoteAssetVolume': '20000000'}
            elif endpoint == '/fapi/v1/fundingRate':
                return [{'fundingRate': '0.0001'}]
            elif endpoint == '/futures/data/openInterestHist':
                return [{'sumOpenInterest': '500000'}]
            return {}
        mock_binance_client._request.side_effect = mock_request

        # 不应抛出异常
        await strategy._execute_cycle()

        strategy.kline_service.unregister_symbol.assert_awaited_with('OLDCOINUSDT')


# ── 6. 无效币种缓存 ──────────────────────────────────────────────────

class TestInvalidSymbolsCache:
    """测试无效币种缓存机制"""

    @pytest.mark.asyncio
    async def test_invalid_symbol_cached(self, strategy, mock_binance_client):
        """无效币种被缓存，避免重复请求K线服务"""
        # 设置无效币种缓存
        strategy._invalid_symbols = {'INVALIDUSDT'}

        # 调用_get_open_interest（内部会检查_invalid_symbols）
        oi = await strategy._get_open_interest('INVALIDUSDT')

        # 返回0，且不调用binance API
        assert oi == 0.0
        mock_binance_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_symbol_cached_for_oi_history(self, strategy, mock_binance_client):
        """无效币种缓存对历史OI也生效"""
        strategy._invalid_symbols = {'INVALIDUSDT'}

        oi = await strategy._get_open_interest_ahead('INVALIDUSDT', hours_ago=3)

        assert oi == 0.0
        mock_binance_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_symbol_cached_for_volume(self, strategy, mock_binance_client):
        """无效币种缓存对总交易量也生效"""
        strategy._invalid_symbols = {'INVALIDUSDT'}

        volume = await strategy._get_total_volume('INVALIDUSDT')

        assert volume == 0.0
        mock_binance_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_symbol_cached_for_funding_rate(self, strategy, mock_binance_client):
        """无效币种缓存对资金费率也生效"""
        strategy._invalid_symbols = {'INVALIDUSDT'}

        rate = await strategy._get_funding_rate('INVALIDUSDT')

        assert rate == 0.0
        mock_binance_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_symbol_auto_added_on_error(self, strategy, mock_binance_client):
        """币安API返回-9999错误时自动加入无效币种缓存"""
        # 模拟API返回-9999错误
        mock_binance_client._request = AsyncMock(
            side_effect=Exception("-9999 币种无效")
        )
        strategy._invalid_symbols = set()

        oi = await strategy._get_open_interest('BADCOINUSDT')

        assert oi == 0.0
        assert 'BADCOINUSDT' in strategy._invalid_symbols

    @pytest.mark.asyncio
    async def test_cache_cleared_at_cycle_start(self, strategy, mock_binance_client):
        """缓存在_execute_cycle开始时被清理"""
        strategy._invalid_symbols = {'STALEUSDT'}
        strategy.listing_detector.detect_new_listings = AsyncMock(return_value=[])
        strategy._check_blacklist_monitor = AsyncMock()
        strategy._refresh_drawdown_status = AsyncMock()
        strategy._monitor_positions = AsyncMock()
        strategy.drawdown_pause_until = None
        strategy._replenish_done = True

        await strategy._execute_cycle()

        # 缓存应在_execute_cycle开始时被清空
        assert len(strategy._invalid_symbols) == 0