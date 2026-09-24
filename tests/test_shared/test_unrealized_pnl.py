"""
浮动盈亏（未实现盈亏）汇总逻辑单元测试

覆盖 DataService._query_unrealized_pnl 的正负盈亏汇总与异常降级，
通过 mock _binance_client.get_position 执行，不发起真实请求。
"""
from unittest.mock import AsyncMock, MagicMock
import sys
from pathlib import Path

import pytest

# data_service_docker 依赖 dashboard/backend 下的 core 模块，需在其父目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard" / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dashboard.backend.services.data_service_docker import DataService


def _make_service(positions_return):
    """构造 DataService 并注入 mock 币安客户端（仅测 _query_unrealized_pnl，不触发 _ensure_initialized）"""
    svc = DataService()
    client = MagicMock()
    client.get_position = AsyncMock(return_value=positions_return)
    svc._binance_client = client
    return svc


class TestQueryUnrealizedPnl:
    """测试 DataService._query_unrealized_pnl 浮动盈亏汇总"""

    @pytest.mark.asyncio
    async def test_sum_positive_unrealized(self):
        """应汇总所有持仓的算术未实现盈亏（可含正负，兼容 PM 大写 R 字段）"""
        svc = _make_service([
            {"symbol": "BTCUSDT", "unRealizedProfit": "12.5"},   # PM 账户大写 R
            {"symbol": "ETHUSDT", "unrealizedProfit": "-4.25"},  # 常规账户小写 r
            {"symbol": "SOLUSDT", "unRealizedProfit": "0"},      # 0 参与不影响
        ])
        assert await svc._query_unrealized_pnl() == 8.25

    @pytest.mark.asyncio
    async def test_missing_or_invalid_field_ignored(self):
        """字段缺失或非法字符串应跳过，不中断整体汇总"""
        svc = _make_service([
            {"symbol": "BTCUSDT"},  # 缺未实现盈亏字段
            {"symbol": "ETHUSDT", "unRealizedProfit": "abc"},  # 非法
            {"symbol": "SOLUSDT", "unRealizedProfit": "7"},
        ])
        assert await svc._query_unrealized_pnl() == 7.0

    @pytest.mark.asyncio
    async def test_empty_positions_returns_zero(self):
        """无持仓时返回 0"""
        svc = _make_service([])
        assert await svc._query_unrealized_pnl() == 0.0

    @pytest.mark.asyncio
    async def test_api_exception_fallback_zero(self):
        """币安接口抛异常时降级为 0，不影响主统计"""
        svc = DataService()
        client = MagicMock()
        client.get_position = AsyncMock(side_effect=Exception("network error"))
        svc._binance_client = client
        assert await svc._query_unrealized_pnl() == 0.0


class TestOverviewMetricPathUnrealized:
    """测试 get_overview 命中快照路径时仍补上实时浮动盈亏"""

    @pytest.mark.asyncio
    async def test_metric_path_adds_unrealized(self):
        """快照路径返回的 metric dict 应包含实时浮动盈亏字段"""
        svc = DataService()
        client = MagicMock()
        client.get_position = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "unrealizedProfit": "-10.5"},
            {"symbol": "ETHUSDT", "unrealizedProfit": "3.25"},
        ])
        svc._binance_client = client
        svc._ensure_initialized = AsyncMock()  # 跳过真实 DB/币安初始化
        # mock 命中快照：metric 为不含 total_unrealized_pnl 的固定字段 dict
        svc.get_overview_from_metric = AsyncMock(return_value={
            "total_pnl": "-1.0000", "total_closed": 10, "total_orders": 20,
        })

        result = await svc.get_overview("daily")

        # 应补上实时未实现盈亏：-10.5 + 3.25 = -7.25
        assert result["total_unrealized_pnl"] == "-7.2500"
        assert result["total_pnl"] == "-1.0000"

    @pytest.mark.asyncio
    async def test_metric_path_no_unrealized_when_empty_positions(self):
        """快照路径下无持仓时浮动盈亏为 0.00"""
        svc = DataService()
        client = MagicMock()
        client.get_position = AsyncMock(return_value=[])
        svc._binance_client = client
        svc._ensure_initialized = AsyncMock()  # 跳过真实 DB/币安初始化
        svc.get_overview_from_metric = AsyncMock(return_value={"total_pnl": "0.0000"})

        result = await svc.get_overview("daily")

        assert result["total_unrealized_pnl"] == "0.0000"