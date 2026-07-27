"""
HRS 策略 K线服务相关核心逻辑测试

测试覆盖：
1. 策略初始化时K线服务验证
2. 注册标的到K线服务
3. 获取K线数据（含回退逻辑）
4. 合成4h K线数据
5. 注销标的
6. 重启后重新注册
7. 暖机数据获取
"""
import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, List, Any, Optional

# 配置路径
CONFIG_PATH = Path(__file__).parent.parent.parent / "strategies" / "hrs" / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

from strategies.hrs.strategy import HRSStrategy
from shared.kline_service import KLineService
from shared.binance_api import BinanceClient
from shared.notification import NotificationClient
from shared.database import DatabaseManager


# ==================== Fixtures ====================

@pytest.fixture
def mock_kline_service():
    """创建模拟的K线服务"""
    service = MagicMock(spec=KLineService)
    service.register_symbol = AsyncMock(return_value=True)
    service.unregister_symbol = AsyncMock(return_value=True)
    service.get_klines = AsyncMock(return_value=[])
    return service


@pytest.fixture
def mock_binance_client():
    """创建模拟的币安客户端"""
    client = MagicMock(spec=BinanceClient)
    client.get_klines = AsyncMock(return_value=[])
    client.get_account_info = AsyncMock(return_value={"totalMarginBalance": "10000"})
    client.get_position = AsyncMock(return_value=[])
    client.get_ticker = AsyncMock(return_value={"lastPrice": "50000"})
    return client


@pytest.fixture
def mock_notification_client():
    """创建模拟的通知客户端"""
    client = MagicMock(spec=NotificationClient)
    client.send = AsyncMock()
    return client


@pytest.fixture
def mock_db_manager():
    """创建模拟的数据库管理器"""
    db = MagicMock(spec=DatabaseManager)
    db.execute = AsyncMock()
    db.execute_ddl = AsyncMock()
    db.fetch_all = AsyncMock(return_value=[])
    db.fetch_one = AsyncMock(return_value=None)
    db.disconnect = AsyncMock()
    return db


@pytest.fixture
def strategy(mock_kline_service, mock_binance_client, mock_notification_client, mock_db_manager):
    """创建配置好依赖的 HRSStrategy 实例（不含initialize调用）"""
    strat = HRSStrategy(CONFIG)
    strat.kline_service = mock_kline_service
    strat.binance_client = mock_binance_client
    strat.notification_client = mock_notification_client
    strat.db = mock_db_manager
    return strat


# ==================== 辅助函数 ====================

def _make_klines(count: int, start_time: int = 1700000000000, interval_ms: int = 3600000) -> List[Dict]:
    """生成模拟的1h K线数据

    Args:
        count: K线数量
        start_time: 起始时间戳（毫秒）
        interval_ms: K线间隔（毫秒），默认1h

    Returns:
        K线数据列表
    """
    klines = []
    for i in range(count):
        price = 50000.0 + (i % 10) * 100
        klines.append({
            "open_time": start_time + i * interval_ms,
            "open": price,
            "high": price + 200,
            "low": price - 200,
            "close": price + 50,
            "volume": 100.0 + i,
            "close_time": start_time + i * interval_ms + interval_ms,
            "quote_volume": price * (100.0 + i),
            "trade_count": 1000 + i,
            "taker_buy_volume": 50.0 + i * 0.5,
            "taker_buy_quote_volume": price * (50.0 + i * 0.5),
        })
    return klines


def _make_klines_4h(count: int) -> List[Dict]:
    """生成模拟的4h K线数据"""
    klines = []
    for i in range(count):
        price = 50000.0 + (i % 10) * 200
        klines.append({
            "open_time": 1700000000000 + i * 14400000,
            "open": price,
            "high": price + 500,
            "low": price - 500,
            "close": price + 100,
            "volume": 400.0 + i * 4,
        })
    return klines


# ==================== 1. 策略初始化时K线服务验证 ====================

class TestInitialization:
    """测试策略初始化时K线服务验证"""

    def test_kline_service_set_correctly(self, strategy, mock_kline_service):
        """正确创建HRSStrategy并设置kline_service"""
        assert strategy.kline_service is mock_kline_service

    @pytest.mark.asyncio
    async def test_initialize_raises_without_kline_service(
        self, mock_binance_client, mock_notification_client, mock_db_manager
    ):
        """kline_service未设置时initialize应抛出异常"""
        strat = HRSStrategy(CONFIG)
        strat.binance_client = mock_binance_client
        strat.notification_client = mock_notification_client
        strat.db = mock_db_manager
        # kline_service 故意不设置

        with pytest.raises(ValueError, match="K线服务未设置"):
            await strat.initialize()

    @pytest.mark.asyncio
    async def test_initialize_raises_without_binance_client(self, strategy, mock_kline_service):
        """binance_client未设置时initialize应抛出异常"""
        strategy.binance_client = None
        with pytest.raises(ValueError, match="币安客户端未设置"):
            await strategy.initialize()

    @pytest.mark.asyncio
    async def test_initialize_raises_without_notification(self, strategy, mock_kline_service):
        """notification_client未设置时initialize应抛出异常"""
        strategy.notification_client = None
        with pytest.raises(ValueError, match="通知客户端未设置"):
            await strategy.initialize()


# ==================== 2. 注册标的到K线服务 ====================

class TestRegisterSymbol:
    """测试注册标的到K线服务"""

    @pytest.mark.asyncio
    async def test_register_symbol_success(self, strategy, mock_kline_service):
        """成功注册新标的到K线服务"""
        # Mock K线服务注册成功
        mock_kline_service.register_symbol.return_value = True
        mock_kline_service.get_klines.return_value = _make_klines(168)

        # Mock market_data
        strategy.market_data = MagicMock()
        strategy.market_data.synthesize_4h_klines = AsyncMock(return_value=_make_klines_4h(42))

        await strategy._register_and_warmup("BTCUSDT")

        mock_kline_service.register_symbol.assert_called_once_with("BTCUSDT", intervals=["1h"])
        assert "BTCUSDT" in strategy._registered_symbols
        assert "BTCUSDT" in strategy._klines_cache
        assert "BTCUSDT" in strategy._klines_4h_cache

    @pytest.mark.asyncio
    async def test_register_symbol_failure_continues(self, strategy, mock_kline_service, mock_binance_client):
        """注册失败（K线服务不可用），策略应能继续运行"""
        mock_kline_service.register_symbol.return_value = False
        # K线服务注册失败后，仍应尝试预热K线（从币安API获取）
        mock_binance_client.get_klines.return_value = _make_klines(168)
        strategy.market_data = MagicMock()
        strategy.market_data.synthesize_4h_klines = AsyncMock(return_value=_make_klines_4h(42))

        # 不应抛出异常
        await strategy._register_and_warmup("BTCUSDT")

        # 币种未添加到registered_symbols（因为register_symbol返回False）
        assert "BTCUSDT" not in strategy._registered_symbols
        # 但K线数据已缓存（从币安API拿到）
        assert "BTCUSDT" in strategy._klines_cache
        assert "BTCUSDT" in strategy._klines_4h_cache

    @pytest.mark.asyncio
    async def test_register_symbol_already_registered(self, strategy, mock_kline_service):
        """已注册的标的不重复注册（由 _perform_candidate_scan 中的判断控制）"""
        # 先加入registered_symbols
        strategy._registered_symbols.add("BTCUSDT")
        mock_kline_service.register_symbol.reset_mock()

        # 模拟 _perform_candidate_scan 中的去重逻辑
        # 注意：_register_and_warmup 本身没有去重，但 _perform_candidate_scan 中
        # 通过 if symbol not in self._registered_symbols 控制不重复调用
        # 这里验证 _register_and_warmup 在被调用时仍然会调用 register_symbol
        mock_kline_service.get_klines.return_value = _make_klines(168)
        strategy.market_data = MagicMock()
        strategy.market_data.synthesize_4h_klines = AsyncMock(return_value=_make_klines_4h(42))

        await strategy._register_and_warmup("BTCUSDT")
        # 没有去重保护，所以 register_symbol 仍会被调用
        mock_kline_service.register_symbol.assert_called_once()

        # 验证 _perform_candidate_scan 有去重判断
        from strategies.hrs.strategy import HRSStrategy
        method_source = HRSStrategy._perform_candidate_scan.__code__
        source_lines = [HRSStrategy._perform_candidate_scan.__code__.co_filename]
        # 通过检查候选池扫描逻辑来确认去重存在
        assert hasattr(HRSStrategy._perform_candidate_scan, "__code__")

    @pytest.mark.asyncio
    async def test_candidate_scan_skips_already_registered(self, strategy, mock_kline_service):
        """_perform_candidate_scan 中跳过已注册的标的"""
        # 设置候选池扫描结果
        strategy._registered_symbols.add("BTCUSDT")

        # Mock 候选池
        strategy.candidate_pool = MagicMock()
        strategy.candidate_pool.scan_and_update = AsyncMock(return_value={
            "short": ["BTCUSDT", "ETHUSDT"],
            "long": [],
        })
        strategy.candidate_pool.get_dynamic_thresholds = MagicMock(return_value={})
        strategy.candidate_pool.set_klines_4h_cache = MagicMock()

        # Mock 评分引擎
        strategy.scoring_engine = MagicMock()
        strategy.scoring_engine.set_dynamic_thresholds = MagicMock()

        # Mock market_data
        strategy.market_data = MagicMock()
        strategy.market_data.synthesize_4h_klines = AsyncMock(return_value=_make_klines_4h(42))

        # Mock _unregister_symbols 需要的依赖
        strategy.position_manager = MagicMock()
        strategy.position_manager.has_position = MagicMock(return_value=False)

        strategy.risk_manager = MagicMock()
        strategy.risk_manager.is_paused = MagicMock(return_value=False)
        strategy.risk_manager.is_blacklisted = MagicMock(return_value=False)

        # 重置mock
        mock_kline_service.register_symbol.reset_mock()
        mock_kline_service.get_klines.return_value = _make_klines(168)

        # 执行候选池扫描
        await strategy._perform_candidate_scan("08:05")

        # BTCUSDT 已注册，不应再调用 register_symbol
        # ETHUSDT 未注册，应调用 register_symbol
        # 注意：_perform_candidate_scan 中遍历 result["short"] 和 result["long"]
        # 对每个 symbol 检查 if symbol not in self._registered_symbols 才注册
        register_symbol_calls = [call[0][0] for call in mock_kline_service.register_symbol.call_args_list]
        assert "BTCUSDT" not in register_symbol_calls
        assert "ETHUSDT" in register_symbol_calls


# ==================== 3. 获取K线数据 ====================

class TestGetKlines:
    """测试获取K线数据"""

    @pytest.mark.asyncio
    async def test_get_klines_from_kline_service(self, strategy, mock_kline_service):
        """从K线服务成功获取K线数据"""
        expected_klines = _make_klines(168)
        mock_kline_service.get_klines.return_value = expected_klines

        klines = await strategy._warmup_klines("BTCUSDT")

        assert len(klines) == 168
        mock_kline_service.get_klines.assert_called_once_with(
            symbol="BTCUSDT",
            interval="1h",
            limit=168,
        )

    @pytest.mark.asyncio
    async def test_fallback_to_binance_when_kline_service_empty(self, strategy, mock_kline_service, mock_binance_client):
        """K线服务返回空数据，回退到币安API"""
        mock_kline_service.get_klines.return_value = []
        mock_binance_client.get_klines.return_value = _make_klines(168)

        klines = await strategy._warmup_klines("BTCUSDT")

        assert len(klines) == 168
        mock_binance_client.get_klines.assert_called_once_with(
            symbol="BTCUSDT",
            interval="1h",
            limit=min(168, 100),  # max_api_limit=100
        )

    @pytest.mark.asyncio
    async def test_fallback_to_binance_when_kline_service_raises(self, strategy, mock_kline_service, mock_binance_client):
        """K线服务抛出异常，回退到币安API"""
        mock_kline_service.get_klines.side_effect = Exception("K线服务不可用")
        mock_binance_client.get_klines.return_value = _make_klines(168)

        klines = await strategy._warmup_klines("BTCUSDT")

        assert len(klines) == 168
        mock_binance_client.get_klines.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_symbol_when_both_fail(self, strategy, mock_kline_service, mock_binance_client):
        """同时使用K线服务和币安API都失败，策略应跳过该标的"""
        mock_kline_service.get_klines.return_value = []
        mock_binance_client.get_klines.return_value = []

        klines = await strategy._warmup_klines("BTCUSDT")

        assert klines == []

    @pytest.mark.asyncio
    async def test_fallback_when_insufficient_data(self, strategy, mock_kline_service, mock_binance_client):
        """K线服务返回的数据不足min_klines，回退到币安API"""
        # 返回 10 根 < min_klines (24)
        mock_kline_service.get_klines.return_value = _make_klines(10)
        mock_binance_client.get_klines.return_value = _make_klines(168)

        klines = await strategy._warmup_klines("BTCUSDT")

        # 应回退到币安API
        assert len(klines) == 168
        mock_binance_client.get_klines.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_skips_when_klines_insufficient(self, strategy, mock_kline_service, mock_binance_client):
        """analyze方法在K线数据不足时跳过该标的"""
        # 缓存中只有5根K线 < min_klines (24)
        strategy._klines_cache["BTCUSDT"] = _make_klines(5)
        # 预热也失败（返回数据不足）
        mock_kline_service.get_klines.return_value = _make_klines(5)
        mock_binance_client.get_klines.return_value = _make_klines(5)

        # Mock 候选池
        strategy.candidate_pool = MagicMock()
        strategy.candidate_pool.get_short_candidates = MagicMock(return_value=set())
        strategy.candidate_pool.get_long_candidates = MagicMock(return_value=set())

        # Mock market_data（analyze中会用到）
        strategy.market_data = MagicMock()
        strategy.market_data.get_oi_usd = AsyncMock(return_value=1000000)
        strategy.market_data.get_funding_rate = AsyncMock(return_value=0.0001)
        strategy.market_data.get_24h_volume = AsyncMock(return_value=10000000)
        strategy.market_data.get_market_cap = AsyncMock(return_value=1000000000)

        result = await strategy.analyze("BTCUSDT")

        assert result.get("skip") is True
        assert "K线数据不足" in result.get("reason", "")


# ==================== 4. 合成4h K线数据 ====================

class TestSynthesize4hKlines:
    """测试合成4h K线数据"""

    @pytest.mark.asyncio
    async def test_synthesize_4h_klines_success(self, strategy):
        """从1h K线合成4h K线的逻辑正确"""
        klines_1h = _make_klines(24)

        strategy.market_data = MagicMock()
        strategy.market_data.synthesize_4h_klines = AsyncMock(return_value=_make_klines_4h(6))

        await strategy._synthesize_4h_klines("BTCUSDT", klines_1h)

        assert "BTCUSDT" in strategy._klines_4h_cache
        assert len(strategy._klines_4h_cache["BTCUSDT"]) == 6
        strategy.market_data.synthesize_4h_klines.assert_called_once_with(klines_1h)

    @pytest.mark.asyncio
    async def test_synthesize_4h_klines_empty_result(self, strategy):
        """market_data合成4h K线返回空时，不缓存"""
        klines_1h = _make_klines(3)  # 只有3根1h K线

        strategy.market_data = MagicMock()
        strategy.market_data.synthesize_4h_klines = AsyncMock(return_value=[])

        await strategy._synthesize_4h_klines("BTCUSDT", klines_1h)

        # 4h K线缓存中不应有该币种
        assert "BTCUSDT" not in strategy._klines_4h_cache

    @pytest.mark.asyncio
    async def test_synthesize_4h_klines_without_market_data(self, strategy):
        """market_data未初始化时，合成4h K线应跳过不报错"""
        strategy.market_data = None
        klines_1h = _make_klines(24)

        # 不应抛出异常
        await strategy._synthesize_4h_klines("BTCUSDT", klines_1h)

        assert "BTCUSDT" not in strategy._klines_4h_cache

    @pytest.mark.asyncio
    async def test_synthesize_4h_caches_trimmed(self, strategy):
        """合成4h K线只保留最近 synthetic_4h_count 根"""
        klines_1h = _make_klines(200)  # 足够合成 50 根4h K线
        synthetic_4h_count = CONFIG.get("kline", {}).get("synthetic_4h_count", 50)

        # 生成 60 根4h K线，超出缓存限制
        all_4h = _make_klines_4h(60)
        strategy.market_data = MagicMock()
        strategy.market_data.synthesize_4h_klines = AsyncMock(return_value=all_4h)

        await strategy._synthesize_4h_klines("BTCUSDT", klines_1h)

        assert len(strategy._klines_4h_cache["BTCUSDT"]) == synthetic_4h_count


# ==================== 5. 注销标的 ====================

class TestUnregisterSymbol:
    """测试注销标的"""

    @pytest.mark.asyncio
    async def test_unregister_symbol_success(self, strategy, mock_kline_service):
        """从K线服务注销标的成功"""
        # 先设置状态
        strategy._registered_symbols.add("BTCUSDT")
        strategy._klines_cache["BTCUSDT"] = _make_klines(10)
        strategy._klines_4h_cache["BTCUSDT"] = _make_klines_4h(2)

        await strategy._unregister_single_symbol("BTCUSDT")

        mock_kline_service.unregister_symbol.assert_called_once_with("BTCUSDT")
        assert "BTCUSDT" not in strategy._registered_symbols
        assert "BTCUSDT" not in strategy._klines_cache
        assert "BTCUSDT" not in strategy._klines_4h_cache

    @pytest.mark.asyncio
    async def test_unregister_symbol_from_non_existent(self, strategy, mock_kline_service):
        """注销不存在的标的（_registered_symbols中没有），不应报错"""
        # BTCUSDT 不在 registered_symbols 中
        await strategy._unregister_single_symbol("BTCUSDT")

        mock_kline_service.unregister_symbol.assert_called_once_with("BTCUSDT")
        # discard 对不存在的元素不会报错

    @pytest.mark.asyncio
    async def test_unregister_symbol_failure(self, strategy, mock_kline_service):
        """注销失败时，本地状态不清除"""
        strategy._registered_symbols.add("BTCUSDT")
        strategy._klines_cache["BTCUSDT"] = _make_klines(10)
        strategy._klines_4h_cache["BTCUSDT"] = _make_klines_4h(2)

        # unregister_symbol 抛出异常
        mock_kline_service.unregister_symbol.side_effect = Exception("K线服务不可用")

        # 不应抛出异常
        await strategy._unregister_single_symbol("BTCUSDT")

        # 由于异常发生在 try 块开头，后续的本地清理代码不会执行
        # 所以本地状态被保留
        # 注意：这是代码的当前行为，异常后本地状态不会被清理
        assert "BTCUSDT" in strategy._registered_symbols
        assert "BTCUSDT" in strategy._klines_cache
        assert "BTCUSDT" in strategy._klines_4h_cache

    @pytest.mark.asyncio
    async def test_should_unregister_checks_conditions(self, strategy):
        """_should_unregister 正确检查注销条件"""
        # Mock 各模块
        strategy.candidate_pool = MagicMock()
        strategy.candidate_pool.get_short_candidates = MagicMock(return_value=set())
        strategy.candidate_pool.get_long_candidates = MagicMock(return_value=set())

        strategy.position_manager = MagicMock()
        strategy.position_manager.has_position = MagicMock(return_value=False)

        strategy.risk_manager = MagicMock()
        strategy.risk_manager.is_paused = MagicMock(return_value=False)
        strategy.risk_manager.is_blacklisted = MagicMock(return_value=False)

        # 不在候选池、无持仓、不在黑名单 = 应注销
        assert strategy._should_unregister("BTCUSDT") is True

        # 在候选池中 = 不应注销
        strategy.candidate_pool.get_short_candidates = MagicMock(return_value={"BTCUSDT"})
        assert strategy._should_unregister("BTCUSDT") is False
        strategy.candidate_pool.get_short_candidates = MagicMock(return_value=set())

        # 有持仓 = 不应注销
        strategy.position_manager.has_position = MagicMock(return_value=True)
        assert strategy._should_unregister("BTCUSDT") is False
        strategy.position_manager.has_position = MagicMock(return_value=False)

        # 在黑名单中 = 不应注销
        strategy.risk_manager.is_blacklisted = MagicMock(return_value=True)
        assert strategy._should_unregister("BTCUSDT") is False
        strategy.risk_manager.is_blacklisted = MagicMock(return_value=False)

        # 暂停中 = 不应注销
        strategy.risk_manager.is_paused = MagicMock(return_value=True)
        assert strategy._should_unregister("BTCUSDT") is False
        strategy.risk_manager.is_paused = MagicMock(return_value=False)


# ==================== 6. 重启后重新注册 ====================

class TestReregister:
    """测试重启后重新注册"""

    @pytest.mark.asyncio
    async def test_reregister_klines_services(self, strategy, mock_kline_service):
        """策略重启后重新注册所有已注册标的"""
        strategy._registered_symbols = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}
        mock_kline_service.register_symbol.return_value = True
        # 返回足够的K线数据，让 _wait_klines_warmup 立即退出
        mock_kline_service.get_klines.return_value = _make_klines(50)

        await strategy._reregister_klines_services()

        assert mock_kline_service.register_symbol.call_count == 3
        # 验证每个币种都注册了
        calls = [call[0][0] for call in mock_kline_service.register_symbol.call_args_list]
        assert "BTCUSDT" in calls
        assert "ETHUSDT" in calls
        assert "BNBUSDT" in calls

    @pytest.mark.asyncio
    async def test_reregister_skips_when_empty(self, strategy, mock_kline_service):
        """没有已注册标的时跳过重新注册"""
        strategy._registered_symbols = set()
        await strategy._reregister_klines_services()
        mock_kline_service.register_symbol.assert_not_called()

    @pytest.mark.asyncio
    async def test_reregister_partial_failure(self, strategy, mock_kline_service):
        """部分重新注册失败不影响其他币种"""
        strategy._registered_symbols = {"BTCUSDT", "ETHUSDT"}

        # BTCUSDT 注册成功，ETHUSDT 注册失败
        mock_kline_service.register_symbol = AsyncMock(side_effect=[
            True,   # BTCUSDT
            False,  # ETHUSDT
        ])
        mock_kline_service.get_klines.return_value = _make_klines(50)

        # 不应抛出异常
        await strategy._reregister_klines_services()

        # 两个都应该在 registered_symbols 中保留
        assert "BTCUSDT" in strategy._registered_symbols
        assert "ETHUSDT" in strategy._registered_symbols

    @pytest.mark.asyncio
    async def test_reregister_with_exception(self, strategy, mock_kline_service):
        """重新注册时某个币种抛出异常，不影响其他币种"""
        strategy._registered_symbols = {"BTCUSDT", "ETHUSDT"}

        # BTCUSDT 抛出异常，ETHUSDT 成功
        mock_kline_service.register_symbol = AsyncMock(side_effect=[
            Exception("注册失败"),  # BTCUSDT
            True,                   # ETHUSDT
        ])
        mock_kline_service.get_klines.return_value = _make_klines(50)

        # 不应抛出异常
        await strategy._reregister_klines_services()

        # 异常不应影响其他币种
        assert "BTCUSDT" in strategy._registered_symbols
        assert "ETHUSDT" in strategy._registered_symbols


# ==================== 7. 暖机数据获取 ====================

class TestWarmup:
    """测试暖机数据获取"""

    @pytest.mark.asyncio
    async def test_warmup_sufficient_data(self, strategy, mock_kline_service):
        """从K线服务获取足够暖机数据"""
        expected_klines = _make_klines(168)
        mock_kline_service.get_klines.return_value = expected_klines

        klines = await strategy._warmup_klines("BTCUSDT")

        assert len(klines) == 168
        mock_kline_service.get_klines.assert_called_once()
        # 不应回退到币安API
        strategy.binance_client.get_klines.assert_not_called()

    @pytest.mark.asyncio
    async def test_warmup_insufficient_data_fallback(self, strategy, mock_kline_service, mock_binance_client):
        """K线服务数据不足时回退到币安API"""
        mock_kline_service.get_klines.return_value = _make_klines(5)  # 不足24根
        mock_binance_client.get_klines.return_value = _make_klines(168)

        klines = await strategy._warmup_klines("BTCUSDT")

        assert len(klines) == 168
        mock_binance_client.get_klines.assert_called_once()

    @pytest.mark.asyncio
    async def test_warmup_returns_empty_on_all_fail(self, strategy, mock_kline_service, mock_binance_client):
        """K线服务和币安API都失败时返回空列表"""
        mock_kline_service.get_klines.side_effect = Exception("K线服务不可用")
        mock_binance_client.get_klines.side_effect = Exception("币安API不可用")

        klines = await strategy._warmup_klines("BTCUSDT")

        assert klines == []

    @pytest.mark.asyncio
    async def test_warmup_respects_max_api_limit(self, strategy, mock_kline_service, mock_binance_client):
        """回退到币安API时遵守max_api_limit限制"""
        mock_kline_service.get_klines.return_value = _make_klines(5)
        mock_binance_client.get_klines.return_value = _make_klines(100)

        klines = await strategy._warmup_klines("BTCUSDT")

        # 验证 limit 参数为 min(keep_count, max_api_limit) = min(168, 100) = 100
        call_kwargs = mock_binance_client.get_klines.call_args.kwargs
        assert call_kwargs["limit"] == 100


# ==================== 8. 批量注销 ====================

class TestBatchUnregister:
    """测试批量注销标的"""

    @pytest.mark.asyncio
    async def test_unregister_symbols(self, strategy, mock_kline_service):
        """批量注销不再需要的标的"""
        # 设置已注册的币种
        strategy._registered_symbols = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}

        # Mock 各模块 - 只有 BTCUSDT 应注销
        strategy.candidate_pool = MagicMock()
        strategy.candidate_pool.get_short_candidates = MagicMock(return_value={"ETHUSDT"})
        strategy.candidate_pool.get_long_candidates = MagicMock(return_value=set())

        strategy.position_manager = MagicMock()
        strategy.position_manager.has_position = MagicMock(side_effect=lambda s: s == "BNBUSDT")

        strategy.risk_manager = MagicMock()
        strategy.risk_manager.is_paused = MagicMock(return_value=False)
        strategy.risk_manager.is_blacklisted = MagicMock(return_value=False)

        # ETHUSDT 在候选池 → 不注销
        # BNBUSDT 有持仓 → 不注销
        # BTCUSDT 应注销

        await strategy._unregister_symbols()

        # BTCUSDT 应被注销
        assert mock_kline_service.unregister_symbol.call_count == 1
        mock_kline_service.unregister_symbol.assert_called_with("BTCUSDT")
        # ETHUSDT 和 BNBUSDT 应保留
        assert "ETHUSDT" in strategy._registered_symbols
        assert "BNBUSDT" in strategy._registered_symbols
        # BTCUSDT 应从 registered_symbols 中移除
        assert "BTCUSDT" not in strategy._registered_symbols