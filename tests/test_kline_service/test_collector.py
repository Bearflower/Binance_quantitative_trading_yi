"""
测试 K 线数据采集器 KlineCollector
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime, timedelta


class TestKlineCollector:
    """测试 KlineCollector"""

    @pytest.fixture
    def collector(self):
        """创建 KlineCollector 实例"""
        from services.kline_service.core.collector import KlineCollector
        from services.kline_service.core.binance_client import BinanceClient

        mock_client = AsyncMock(spec=BinanceClient)
        mock_db = MagicMock()

        # 模拟 db.get_connection 上下文管理器
        mock_conn = AsyncMock()
        mock_conn.fetch_val = AsyncMock(return_value=True)  # 表已存在
        mock_conn.execute = AsyncMock()

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_conn
        mock_db.get_connection.return_value = cm

        return KlineCollector(
            binance_client=mock_client,
            db=mock_db,
            symbols=["BTCUSDT"],
            intervals=["15m"],
        )

    # ==================== collect_klines 正常采集 ====================

    @pytest.mark.asyncio
    async def test_collect_klines_success(self, collector, mock_kline_data):
        """测试 collect_klines 正常采集"""
        collector.binance_client.get_klines.return_value = mock_kline_data

        result = await collector.collect_klines("BTCUSDT", "15m")

        assert len(result) == 1
        assert result[0].symbol == "BTCUSDT"
        assert result[0].interval == "15m"
        assert result[0].open_price == 50000.0
        assert result[0].close_price == 50500.0
        assert collector.stats["total_collected"] == 1
        assert collector.stats["last_collect_time"] is not None

        collector.binance_client.get_klines.assert_called_once_with(
            symbol="BTCUSDT", interval="15m",
            start_time=None, end_time=None, limit=500
        )

    @pytest.mark.asyncio
    async def test_collect_klines_empty_data(self, collector):
        """测试 collect_klines 币安API返回空数据"""
        collector.binance_client.get_klines.return_value = []

        result = await collector.collect_klines("BTCUSDT", "15m")

        assert result == []
        assert collector.stats["total_collected"] == 0

    @pytest.mark.asyncio
    async def test_collect_klines_api_exception(self, collector):
        """测试 collect_klines 币安API异常"""
        collector.binance_client.get_klines.side_effect = Exception("API 异常")

        result = await collector.collect_klines("BTCUSDT", "15m")

        assert result == []
        assert collector.stats["total_errors"] == 1

    @pytest.mark.asyncio
    async def test_collect_klines_store_failure(self, collector, mock_kline_objects):
        """测试存储 K 线数据时数据库失败"""
        collector.binance_client.get_klines.return_value = [
            [1700000000000, "50000.0", "51000.0", "49000.0", "50500.0",
             "100.5", 1700003600000, "5000000.0", 1000, "60.0", "3000000.0", "0"]
        ]

        # 模拟 _batch_insert 抛出异常
        with patch.object(collector, "_batch_insert", side_effect=Exception("数据库错误")):
            stored = await collector.store_klines(mock_kline_objects)
            assert stored == 0
            assert collector.stats["total_errors"] == 1

    # ==================== collect_recent ====================

    @pytest.mark.asyncio
    async def test_collect_recent_success(self, collector, mock_kline_data):
        """测试 collect_recent 正常采集"""
        collector.binance_client.get_klines.return_value = mock_kline_data

        # 模拟 store_klines 返回成功
        with patch.object(collector, "store_klines", new_callable=AsyncMock) as mock_store:
            mock_store.return_value = 1

            stored = await collector.collect_recent("BTCUSDT", "15m", minutes=30)

            assert stored == 1
            collector.binance_client.get_klines.assert_called_once()

    @pytest.mark.asyncio
    async def test_collect_recent_empty_data(self, collector):
        """测试 collect_recent 数据为空"""
        collector.binance_client.get_klines.return_value = []

        stored = await collector.collect_recent("BTCUSDT", "15m", minutes=30)

        assert stored == 0

    @pytest.mark.asyncio
    async def test_collect_recent_filter_unclosed(self, collector, mock_kline_data):
        """测试 collect_recent 过滤未收盘的 K 线"""
        # 创建一个 close_time 在当前时间之后的 K 线（未收盘）
        future_time = int((datetime.now() + timedelta(hours=1)).timestamp() * 1000)
        open_data = [
            1700000000000,
            "50000.0", "51000.0", "49000.0", "50500.0",
            "100.5", future_time, "5000000.0", 1000, "60.0", "3000000.0", "0"
        ]
        collector.binance_client.get_klines.return_value = [open_data]

        stored = await collector.collect_recent("BTCUSDT", "15m", minutes=30)

        # 未收盘的 K 线应被过滤掉
        assert stored == 0

    # ==================== validate_registered_symbols ====================

    @pytest.mark.asyncio
    async def test_validate_registered_symbols_clean(self, collector, mock_registered_symbol_config):
        """测试验证注册标的，无无效标的"""
        collector.binance_client.get_all_symbols.return_value = ["BTCUSDT", "ETHUSDT"]

        with patch("services.kline_service.core.collector.registry") as mock_registry:
            mock_registry.get_active_symbols.return_value = [mock_registered_symbol_config]

            cleaned = await collector.validate_registered_symbols()

            assert cleaned == 0
            mock_registry.unregister.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_registered_symbols_clean_invalid(self, collector, mock_registered_symbol_config):
        """测试验证注册标的，发现无效标的并清理"""
        collector.binance_client.get_all_symbols.return_value = ["ETHUSDT"]  # BTCUSDT 不在有效列表中

        with patch("services.kline_service.core.collector.registry") as mock_registry:
            mock_registry.get_active_symbols.return_value = [mock_registered_symbol_config]
            mock_registry.unregister = AsyncMock(return_value=True)

            cleaned = await collector.validate_registered_symbols()

            assert cleaned == 1
            mock_registry.unregister.assert_called_once_with("BTCUSDT")

    @pytest.mark.asyncio
    async def test_validate_registered_symbols_api_error(self, collector):
        """测试验证注册标的，币安API获取有效标的失败"""
        collector.binance_client.get_all_symbols.return_value = []

        with patch("services.kline_service.core.collector.registry") as mock_registry:
            cleaned = await collector.validate_registered_symbols()

            assert cleaned == 0
            mock_registry.get_active_symbols.assert_not_called()

    # ==================== collect_all ====================

    @pytest.mark.asyncio
    async def test_collect_all_success(self, collector, mock_kline_data):
        """测试 collect_all 批量采集"""
        collector.binance_client.get_klines.return_value = mock_kline_data

        with patch.object(collector, "store_klines", new_callable=AsyncMock) as mock_store:
            mock_store.return_value = 1

            total = await collector.collect_all()

            assert total == 1
            assert collector.binance_client.get_klines.call_count == 1

    @pytest.mark.asyncio
    async def test_collect_all_no_data(self, collector):
        """测试 collect_all 无数据"""
        collector.binance_client.get_klines.return_value = []

        total = await collector.collect_all()

        assert total == 0

    # ==================== ensure_table ====================

    @pytest.mark.asyncio
    async def test_ensure_table_already_exists(self, collector):
        """测试 ensure_table 表已存在"""
        result = await collector.ensure_table("BTCUSDT", "15m")
        assert result is True

    @pytest.mark.asyncio
    async def test_ensure_table_create_success(self, collector):
        """测试 ensure_table 创建新表成功"""
        # 模拟表不存在
        mock_conn = AsyncMock()
        mock_conn.fetch_val = AsyncMock(return_value=False)
        mock_conn.execute = AsyncMock()

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_conn
        collector.db.get_connection.return_value = cm

        result = await collector.ensure_table("BTCUSDT", "15m")
        assert result is True

    @pytest.mark.asyncio
    async def test_ensure_table_error(self, collector):
        """测试 ensure_table 创建失败"""
        mock_conn = AsyncMock()
        mock_conn.fetch_val.side_effect = Exception("数据库错误")

        cm = AsyncMock()
        cm.__aenter__.return_value = mock_conn
        collector.db.get_connection.return_value = cm

        result = await collector.ensure_table("BTCUSDT", "15m")
        assert result is False

    # ==================== store_klines ====================

    @pytest.mark.asyncio
    async def test_store_klines_empty(self, collector):
        """测试 store_klines 空数据"""
        stored = await collector.store_klines([])
        assert stored == 0

    @pytest.mark.asyncio
    async def test_store_klines_success(self, collector, mock_kline_objects):
        """测试 store_klines 正常存储"""
        with patch.object(collector, "_batch_insert", new_callable=AsyncMock) as mock_batch:
            mock_batch.return_value = 2

            stored = await collector.store_klines(mock_kline_objects)

            assert stored == 2
            assert collector.stats["total_stored"] == 2

    # ==================== get_stats ====================

    def test_get_stats(self, collector):
        """测试 get_stats 返回统计信息"""
        collector.stats["total_collected"] = 100
        collector.stats["total_stored"] = 80

        stats = collector.get_stats()

        assert stats["total_collected"] == 100
        assert stats["total_stored"] == 80
        # 验证返回的是副本，修改不影响原对象
        stats["total_collected"] = 999
        assert collector.stats["total_collected"] == 100