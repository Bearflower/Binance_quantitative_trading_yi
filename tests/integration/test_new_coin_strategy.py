"""
新币做空策略集成测试
测试策略初始化、分析、执行等功能
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import yaml

from strategies.new_coin.strategy import NewCoinStrategy
from strategies.new_coin.scoring_engine import ScoringEngine, ScoringResult
from strategies.new_coin.pattern import PatternRecognizer
from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.database import DatabaseManager


@pytest.fixture
def config():
    """测试配置"""
    return {
        'strategy': {
            'name': 'new_coin_test',
            'version': '1.0.0'
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
            }
        },
        'trading': {
            'leverage': 2,
            'max_positions': 3,
            'single_position_margin': 50,
            'stop_loss_percent': 0.05,
            'take_profit_percent': 0.10
        },
        'pattern': {
            'three_tops': {
                'max_deviation': 0.02,
                'score_high': 4.0,
                'score_medium': 2.0
            },
            'long_upper_shadow': {
                'ratio_threshold': 2.0,
                'score_high': 3.0,
                'score_medium': 2.0
            },
            'volume_divergence': {
                'volume_ratio_threshold': 1.5,
                'price_change_threshold': 0.02,
                'score_high': 3.0,
                'score_medium': 2.0
            }
        },
        'detector': {
            'check_interval': 300,
            'min_listing_hours': 1,
            'max_listing_hours': 48
        },
        'kline': {
            'interval': '1h',
            'limit': 18
        }
    }


@pytest.fixture
def mock_binance_client():
    """模拟币安客户端"""
    client = Mock(spec=BinanceClient)
    client._request = AsyncMock()
    client.get_account_balance = AsyncMock(return_value={'USDT': 1000})
    client.get_position = AsyncMock(return_value=[])
    client.place_order = AsyncMock(return_value={'orderId': '12345', 'status': 'FILLED'})
    return client


@pytest.fixture
def mock_kline_service():
    """模拟K线服务"""
    service = Mock(spec=KLineService)
    service.get_klines = AsyncMock(return_value=[
        {
            'open': 100 + i,
            'high': 105 + i,
            'low': 95 + i,
            'close': 102 + i,
            'volume': 1000,
            'open_time': datetime.now().timestamp()
        }
        for i in range(200)
    ])
    service.close = AsyncMock()
    return service


@pytest.fixture
def mock_notification_client():
    """模拟通知客户端"""
    client = Mock(spec=NotificationClient)
    client.send = AsyncMock(return_value=True)
    client.send_alert = AsyncMock(return_value=True)
    client.send_trade_notification = AsyncMock(return_value=True)
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


@pytest.mark.asyncio
async def test_strategy_initialization(config, mock_binance_client, mock_kline_service, mock_notification_client, mock_db):
    """测试策略初始化"""
    # 创建策略实例
    strategy = NewCoinStrategy(config)

    # 设置客户端
    await strategy.set_binance_client(mock_binance_client)
    await strategy.set_kline_service(mock_kline_service)
    await strategy.set_notification_client(mock_notification_client)
    await strategy.set_database(mock_db)

    # 初始化策略
    await strategy.initialize()

    # 验证初始化成功
    assert strategy.scoring_engine is not None
    assert strategy.pattern_recognizer is not None
    assert strategy.listing_detector is not None
    assert strategy.trading_executor is not None


@pytest.mark.asyncio
async def test_pattern_recognition(config):
    """测试形态识别"""
    recognizer = PatternRecognizer(config)

    # 创建测试K线数据
    klines = [
        {
            'open': 100,
            'high': 105,
            'low': 95,
            'close': 102,
            'volume': 1000
        }
        for _ in range(200)
    ]

    # 检测形态
    result = recognizer.detect(klines)

    # 验证结果格式
    assert 'three_tops' in result
    assert 'long_upper_shadow' in result
    assert 'volume_divergence' in result

    # 验证结果类型
    assert isinstance(result['three_tops'], tuple)
    assert isinstance(result['long_upper_shadow'], tuple)
    assert isinstance(result['volume_divergence'], tuple)


@pytest.mark.asyncio
async def test_scoring_engine(config):
    """测试评分引擎"""
    engine = ScoringEngine(config)

    # 执行评分
    result = engine.score(
        symbol='TESTUSDT',
        oi_usd=1000000,
        total_volume_usd=20000000,
        funding_rate=0.0001,
        oi_change_rate=0.1,
        three_tops_detected=True,
        three_tops_score=4.0,
        long_upper_shadow=True,
        long_upper_shadow_score=3.0,
        volume_divergence=False,
        volume_divergence_score=0.0,
        listing_hours=24,
        current_price=100.0,
        recent_coins_oi=[]
    )

    # 验证评分结果
    assert isinstance(result, ScoringResult)
    assert result.symbol == 'TESTUSDT'
    assert result.total_score > 0
    assert not result.veto


@pytest.mark.asyncio
async def test_scoring_engine_veto(config):
    """测试评分引擎的一票否决"""
    engine = ScoringEngine(config)

    # 执行评分（上线时间超过阈值）
    result = engine.score(
        symbol='TESTUSDT',
        oi_usd=1000000,
        total_volume_usd=20000000,
        funding_rate=0.0001,
        oi_change_rate=0.1,
        three_tops_detected=True,
        three_tops_score=4.0,
        long_upper_shadow=True,
        long_upper_shadow_score=3.0,
        volume_divergence=False,
        volume_divergence_score=0.0,
        listing_hours=50,  # 超过48小时
        current_price=100.0,
        recent_coins_oi=[]
    )

    # 验证一票否决
    assert result.veto
    assert result.total_score == 0.0
    assert '上线时间过长' in result.veto_reason


@pytest.mark.asyncio
async def test_strategy_analyze(config, mock_binance_client, mock_kline_service, mock_notification_client, mock_db):
    """测试策略分析功能"""
    # 创建策略实例
    strategy = NewCoinStrategy(config)

    # 设置客户端
    await strategy.set_binance_client(mock_binance_client)
    await strategy.set_kline_service(mock_kline_service)
    await strategy.set_notification_client(mock_notification_client)
    await strategy.set_database(mock_db)

    # 初始化策略
    await strategy.initialize()

    # Mock交易所信息
    mock_binance_client._request.return_value = {
        'symbols': [
            {
                'symbol': 'TESTUSDT',
                'baseAsset': 'TEST',
                'quoteAsset': 'USDT',
                'onboardDate': int(datetime.now().timestamp() * 1000) - 3600000,  # 1小时前
                'status': 'TRADING'
            }
        ]
    }

    # Mock合约数据
    async def mock_request(method, endpoint, params=None, signed=False):
        if endpoint == '/fapi/v1/exchangeInfo':
            return {
                'symbols': [
                    {
                        'symbol': 'TESTUSDT',
                        'baseAsset': 'TEST',
                        'quoteAsset': 'USDT',
                        'onboardDate': int(datetime.now().timestamp() * 1000) - 3600000,
                        'status': 'TRADING'
                    }
                ]
            }
        elif endpoint == '/fapi/v1/openInterest':
            return {'openInterest': '1000000'}
        elif endpoint == '/fapi/v1/ticker/24hr':
            return {'quoteAssetVolume': '20000000'}
        elif endpoint == '/fapi/v1/fundingRate':
            return [{'fundingRate': '0.0001'}]
        return {}

    mock_binance_client._request.side_effect = mock_request

    # 执行分析
    result = await strategy.analyze('TESTUSDT')

    # 验证分析结果
    assert 'symbol' in result
    assert result['symbol'] == 'TESTUSDT'

    # 如果没有错误，验证评分结果
    if not result.get('error') and not result.get('skip'):
        assert 'score_result' in result
        assert 'patterns' in result
        assert 'market_data' in result


@pytest.mark.asyncio
async def test_strategy_stop(config, mock_binance_client, mock_kline_service, mock_notification_client, mock_db):
    """测试策略停止"""
    # 创建策略实例
    strategy = NewCoinStrategy(config)

    # 设置客户端
    await strategy.set_binance_client(mock_binance_client)
    await strategy.set_kline_service(mock_kline_service)
    await strategy.set_notification_client(mock_notification_client)
    await strategy.set_database(mock_db)

    # 初始化策略
    await strategy.initialize()

    # 停止策略
    await strategy.stop()

    # 验证策略已停止
    assert not strategy.is_running()


@pytest.mark.asyncio
async def test_should_entry_logic(config):
    """测试入场逻辑"""
    engine = ScoringEngine(config)

    # 创建高分评分结果
    high_score_result = ScoringResult(
        symbol='TESTUSDT',
        total_score=8.0,
        contract_score=8.0,
        technical_score=7.0,
        sentiment_score=7.0,
        veto=False,
        veto_reason=None,
        details={}
    )

    # 测试应该入场
    should_entry = engine.should_entry(high_score_result, 4.0, 7.0)
    assert should_entry

    # 创建低分评分结果
    low_score_result = ScoringResult(
        symbol='TESTUSDT',
        total_score=5.0,
        contract_score=5.0,
        technical_score=4.0,
        sentiment_score=5.0,
        veto=False,
        veto_reason=None,
        details={}
    )

    # 测试不应该入场
    should_entry = engine.should_entry(low_score_result, 2.0, 3.0)
    assert not should_entry


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
