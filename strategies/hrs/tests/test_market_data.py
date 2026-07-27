"""
市场数据测试
测试 MarketDataProvider 的 4h 合成、EMA 计算、结算时间计算
"""
import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)


@pytest.fixture
def mock_binance_api():
    """创建模拟的币安API客户端"""
    api = MagicMock()
    api.get_all_tickers = AsyncMock(return_value=[])
    api.get_funding_rate = AsyncMock(return_value=0.0001)
    api.get_funding_rate_history = AsyncMock(return_value=[])
    api.get_klines = AsyncMock(return_value=[])
    api.get_ticker = AsyncMock(return_value={"quoteVolume": "1000000", "priceChangePercent": "5"})
    api._request = AsyncMock(return_value={})
    return api


@pytest.fixture
def mock_kline_service():
    """创建模拟的K线服务客户端"""
    svc = MagicMock()
    svc.get_klines = AsyncMock(return_value=[])
    return svc


from strategies.hrs.market_data import MarketDataProvider


class TestMarketDataProviderInit:
    """测试市场数据提供者初始化"""

    def test_从配置正确加载参数(self, mock_binance_api, mock_kline_service):
        mdp = MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)
        assert mdp.max_api_limit == 100
        assert mdp._synthetic_4h_interval == 4
        assert mdp._ema_period == 20
        assert mdp._settlement_interval == 8


class TestSettlementTime:
    """测试结算时间计算"""

    @pytest.fixture
    def mdp(self, mock_binance_api, mock_kline_service):
        return MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)

    def test_UTC_00时段结算(self, mdp):
        """UTC 02:00 的结算时间应为 UTC 00:00"""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 2, 0, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        settlement = mdp._get_settlement_time(ts_ms)
        settlement_dt = datetime.fromtimestamp(settlement / 1000, tz=timezone.utc)
        assert settlement_dt.hour == 0
        assert settlement_dt.minute == 0

    def test_UTC_08时段结算(self, mdp):
        """UTC 10:00 的结算时间应为 UTC 08:00"""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        settlement = mdp._get_settlement_time(ts_ms)
        settlement_dt = datetime.fromtimestamp(settlement / 1000, tz=timezone.utc)
        assert settlement_dt.hour == 8

    def test_UTC_16时段结算(self, mdp):
        """UTC 18:00 的结算时间应为 UTC 16:00"""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 18, 0, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        settlement = mdp._get_settlement_time(ts_ms)
        settlement_dt = datetime.fromtimestamp(settlement / 1000, tz=timezone.utc)
        assert settlement_dt.hour == 16

    def test_结算时间边界(self, mdp):
        """UTC 08:00 整点的结算时间应为 UTC 08:00"""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        settlement = mdp._get_settlement_time(ts_ms)
        settlement_dt = datetime.fromtimestamp(settlement / 1000, tz=timezone.utc)
        assert settlement_dt.hour == 8

    def test_自定义结算间隔(self, mock_binance_api, mock_kline_service):
        """自定义结算间隔为4小时"""
        custom_config = dict(CONFIG)
        custom_config["funding_rate"] = dict(CONFIG["funding_rate"])
        custom_config["funding_rate"]["settlement_interval_hours"] = 4
        mdp = MarketDataProvider(mock_binance_api, mock_kline_service, custom_config)
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        settlement = mdp._get_settlement_time(ts_ms)
        settlement_dt = datetime.fromtimestamp(settlement / 1000, tz=timezone.utc)
        assert settlement_dt.hour == 12  # 14 // 4 * 4 = 12


class Test4hSlot:
    """测试4h槽位计算"""

    @pytest.fixture
    def mdp(self, mock_binance_api, mock_kline_service):
        return MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)

    def test_UTC_0点槽位(self, mdp):
        """UTC 02:30 的4h槽位应为 UTC 00:00"""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 2, 30, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        slot = mdp._get_4h_slot(ts_ms)
        slot_dt = datetime.fromtimestamp(slot / 1000, tz=timezone.utc)
        assert slot_dt.hour == 0
        assert slot_dt.minute == 0

    def test_UTC_4点槽位(self, mdp):
        """UTC 05:00 的4h槽位应为 UTC 04:00"""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 5, 0, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        slot = mdp._get_4h_slot(ts_ms)
        slot_dt = datetime.fromtimestamp(slot / 1000, tz=timezone.utc)
        assert slot_dt.hour == 4

    def test_UTC_12点槽位(self, mdp):
        """UTC 13:00 的4h槽位应为 UTC 12:00"""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 13, 0, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        slot = mdp._get_4h_slot(ts_ms)
        slot_dt = datetime.fromtimestamp(slot / 1000, tz=timezone.utc)
        assert slot_dt.hour == 12

    def test_UTC_20点槽位(self, mdp):
        """UTC 22:00 的4h槽位应为 UTC 20:00"""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 22, 0, 0, tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        slot = mdp._get_4h_slot(ts_ms)
        slot_dt = datetime.fromtimestamp(slot / 1000, tz=timezone.utc)
        assert slot_dt.hour == 20


class TestSynthesize4hKlines:
    """测试4h K线合成"""

    @pytest.fixture
    def mdp(self, mock_binance_api, mock_kline_service):
        return MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)

    @pytest.mark.asyncio
    async def test_synthesize_4h_klines_basic(self, mdp):
        """4根1h K线合成为1根4h K线"""
        from datetime import datetime, timezone
        
        # 4根连续的1h K线，同一4h槽位（UTC 0~3）
        klines_1h = []
        for i in range(4):
            dt = datetime(2024, 1, 15, i, 0, 0, tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp() * 1000)
            klines_1h.append({
                "open_time": ts_ms,
                "open": 100 + i,
                "high": 105 + i,
                "low": 95 + i,
                "close": 102 + i,
                "volume": 1000 + i * 100,
            })
        
        result = await mdp.synthesize_4h_klines(klines_1h)
        assert len(result) == 1
        k4h = result[0]
        assert k4h["open"] == 100     # 第一根1h的开盘价
        assert k4h["close"] == 105    # 最后一根1h的收盘价 (102+3)
        assert k4h["high"] == 108     # max(105, 106, 107, 108)
        assert k4h["low"] == 95       # min(95, 96, 97, 98)
        assert k4h["volume"] == 1000 + 1100 + 1200 + 1300  # 4600

    @pytest.mark.asyncio
    async def test_多槽位合成(self, mdp):
        """多个4h槽位"""
        from datetime import datetime, timezone
        
        klines_1h = []
        # 8根1h K线，跨越2个4h槽位
        for i in range(8):
            dt = datetime(2024, 1, 15, i, 0, 0, tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp() * 1000)
            klines_1h.append({
                "open_time": ts_ms,
                "open": 100 + i,
                "high": 105 + i,
                "low": 95 + i,
                "close": 102 + i,
                "volume": 1000,
            })
        
        result = await mdp.synthesize_4h_klines(klines_1h)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_空K线列表(self, mdp):
        result = await mdp.synthesize_4h_klines([])
        assert result == []

    @pytest.mark.asyncio
    async def test_无open_time字段(self, mdp):
        """没有 open_time 字段的K线，open_time 默认为0，仍会被合成"""
        klines_1h = [
            {"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000},
        ]
        result = await mdp.synthesize_4h_klines(klines_1h)
        # open_time 默认为 0，仍可被分组合成
        assert len(result) == 1
        assert result[0]["open"] == 100


class TestEMA20:
    """测试 EMA20(4h) 计算"""

    @pytest.fixture
    def mdp(self, mock_binance_api, mock_kline_service):
        return MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)

    @pytest.mark.asyncio
    async def test_EMA20计算正确(self, mdp):
        """使用已知数据验证EMA20计算"""
        from datetime import datetime, timedelta, timezone
        
        # 生成80小时的数据分布在连续80小时中（足够合成20根4h K线）
        klines_1h = []
        base_dt = datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(80):
            dt = base_dt + timedelta(hours=i)
            ts_ms = int(dt.timestamp() * 1000)
            base_price = 100 + i * 0.5
            klines_1h.append({
                "open_time": ts_ms,
                "open": base_price,
                "high": base_price + 1,
                "low": base_price - 1,
                "close": base_price + 0.2,
                "volume": 1000,
            })
        
        ema = await mdp.get_ema20_4h("TESTUSDT", klines_1h)
        assert ema > 0, f"EMA20计算结果应为正数，实际为 {ema}"
        # 价格从100到139.5，EMA应该在100~140之间
        assert 100 < ema < 140, f"EMA20={ema} 不在预期范围 [100, 140]"

    @pytest.mark.asyncio
    async def test_数据不足返回0(self, mdp):
        """数据不足20根4h K线时返回0"""
        from datetime import datetime, timezone
        klines_1h = []
        for i in range(4):
            dt = datetime(2024, 1, 15, i, 0, 0, tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp() * 1000)
            klines_1h.append({
                "open_time": ts_ms,
                "open": 100,
                "high": 105,
                "low": 95,
                "close": 102,
                "volume": 1000,
            })
        
        ema = await mdp.get_ema20_4h("TESTUSDT", klines_1h)
        assert ema == 0.0

    @pytest.mark.asyncio
    async def test_空K线返回0(self, mdp):
        ema = await mdp.get_ema20_4h("TESTUSDT", [])
        assert ema == 0.0


class Test24hData:
    """测试24h行情数据获取"""

    @pytest.mark.asyncio
    async def test_get_24h_volume(self, mock_binance_api, mock_kline_service):
        """测试获取24h成交额"""
        mdp = MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)
        mock_binance_api.get_ticker = AsyncMock(return_value={"quoteVolume": "1234567.89"})
        volume = await mdp.get_24h_volume("TESTUSDT")
        assert volume == 1234567.89

    @pytest.mark.asyncio
    async def test_get_24h_volume_with_ticker(self, mock_binance_api, mock_kline_service):
        """使用已有ticker数据避免重复请求"""
        mdp = MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)
        ticker_data = {"symbol": "TESTUSDT", "quoteVolume": "5000000"}
        volume = await mdp.get_24h_volume("TESTUSDT", ticker_data)
        assert volume == 5000000.0
        # 不应调用 API
        mock_binance_api.get_ticker.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_24h_price_change(self, mock_binance_api, mock_kline_service):
        """测试获取24h涨跌幅"""
        mdp = MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)
        mock_binance_api.get_ticker = AsyncMock(return_value={"priceChangePercent": "12.5"})
        change = await mdp.get_24h_price_change("TESTUSDT")
        assert change == 0.125

    @pytest.mark.asyncio
    async def test_get_24h_price_change_with_ticker(self, mock_binance_api, mock_kline_service):
        """使用已有ticker数据"""
        mdp = MarketDataProvider(mock_binance_api, mock_kline_service, CONFIG)
        ticker_data = {"symbol": "TESTUSDT", "priceChangePercent": "-8.5"}
        change = await mdp.get_24h_price_change("TESTUSDT", ticker_data)
        assert change == -0.085
        mock_binance_api.get_ticker.assert_not_called()


class TestCandidatePoolExclude:
    """测试候选池排除逻辑（纯逻辑部分）"""

    def test_排除BTC和ETH(self):
        """BTC和ETH应被排除"""
        from strategies.hrs.candidate_pool import CandidatePool
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        assert pool._should_exclude("BTCUSDT") is True
        assert pool._should_exclude("ETHUSDT") is True

    def test_排除MCTPS全部币种(self):
        """MCTPS策略全部币种应被排除"""
        from strategies.hrs.candidate_pool import CandidatePool
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        # MCTPS 策略交易币种：BTC/ETH/BNB/SOL/XRP/TRX
        assert pool._should_exclude("BNBUSDT") is True
        assert pool._should_exclude("SOLUSDT") is True
        assert pool._should_exclude("XRPUSDT") is True
        assert pool._should_exclude("TRXUSDT") is True

    def test_排除稳定币(self):
        """稳定币应被排除"""
        from strategies.hrs.candidate_pool import CandidatePool
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        assert pool._should_exclude("USDCUSDT") is True
        assert pool._should_exclude("BUSDUSDT") is True
        assert pool._should_exclude("DAIUSDT") is True

    def test_排除杠杆代币(self):
        """杠杆代币应被排除"""
        from strategies.hrs.candidate_pool import CandidatePool
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        assert pool._should_exclude("BTCBULLUSDT") is True
        assert pool._should_exclude("ETHBEARUSDT") is True
        assert pool._should_exclude("DOGEUPUSDT") is True
        assert pool._should_exclude("DOGEDOWNUSDT") is True

    def test_正常币种不被排除(self):
        """正常币种不应被排除"""
        from strategies.hrs.candidate_pool import CandidatePool
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        assert pool._should_exclude("DOGEUSDT") is False
        assert pool._should_exclude("AVAXUSDT") is False
        assert pool._should_exclude("LINKUSDT") is False

    def test_新币策略冲突黑名单(self):
        """新币策略冲突黑名单"""
        from strategies.hrs.candidate_pool import CandidatePool
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        pool.add_new_coin_conflict("XRPUSDT")
        assert pool._should_exclude("XRPUSDT") is True
        # 未在冲突列表中的不受影响
        assert pool._should_exclude("DOGEUSDT") is False

    def test_新币做空当前持仓排除(self):
        """新币做空当前开仓的币种应被排除"""
        from strategies.hrs.candidate_pool import CandidatePool
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        # 未加载持仓时，不应排除任何币种
        assert pool._should_exclude("DOGEUSDT") is False
        # 模拟加载了新币做空持仓
        pool._new_coin_open_positions.add("DOGEUSDT")
        assert pool._should_exclude("DOGEUSDT") is True
        # 未在持仓中的不受影响
        assert pool._should_exclude("AVAXUSDT") is False

    def test_流动性检查(self):
        """流动性门槛检查"""
        from strategies.hrs.candidate_pool import CandidatePool
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        # 成交额 >= 5000万
        assert pool._check_liquidity({"quoteVolume": "60000000"}) is True
        assert pool._check_liquidity({"quoteVolume": "10000000"}) is False

    def test_候选池健康状态(self):
        """候选池健康状态检查"""
        from strategies.hrs.candidate_pool import CandidatePool
        from datetime import datetime, timezone
        mock_md = MagicMock()
        pool = CandidatePool(CONFIG, mock_md)
        # 未扫描过，不健康
        assert pool.is_healthy() is False
        # 手动设置上次扫描时间
        pool._last_scan_time = datetime.now(timezone.utc)
        assert pool.is_healthy() is True