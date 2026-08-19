"""
测试效果追踪器（EffectTracker）

覆盖用例：
- 正常追踪流程：有上周记忆记录，adapter.collect 返回有效数据
- 无历史记忆：数据库中无对应记录
- collect 异常：adapter.collect 抛出异常
- .active 文件不存在：覆盖层目录无 .active 文件
- 多个历史记忆：多条记忆记录，只回填最新一条
- ratings 阈值边界：胜率变化正好 3%
- 幂等性检查：已填充的 post_* 字段不重复计算
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")

from ai_tuner.adapters.base_adapter import PerformanceMetrics, RiskMetrics, StrategyReport
from ai_tuner.feedback.effect_tracker import EffectSummary, EffectTracker


@pytest.fixture
def base_config():
    """基础配置，使用默认阈值"""
    return {
        "feedback": {
            "rating": {
                "good_win_rate_increase": 0.03,
                "good_pnl_increase_usdt": 5.0,
                "bad_win_rate_decrease": 0.03,
                "bad_pnl_decrease_usdt": 5.0,
                "min_trades_for_valid": 3,
            },
        },
    }


@pytest.fixture
def mock_adapter():
    """创建 BaseAdapter 的 mock 实例"""
    adapter = MagicMock()
    adapter.config_path = "strategies/btc_eth/config.yaml"
    # collect 返回一个 StrategyReport
    perf = PerformanceMetrics(
        total_trades=10,
        win_count=6,
        loss_count=4,
        win_rate=0.65,
        total_pnl=150.0,
        avg_win=50.0,
        avg_loss=30.0,
        profit_factor=1.5,
        sharpe_approx=1.2,
    )
    risk = RiskMetrics(
        max_consecutive_losses=2,
        current_drawdown_pct=0.05,
        is_circuit_breaker_active=False,
        max_drawdown_pct=0.08,
    )
    adapter.collect = AsyncMock(return_value=StrategyReport(performance=perf, risk=risk))
    return adapter


@pytest.fixture
def mock_db_handler():
    """创建 MemoryDBHandler 的 mock 实例"""
    handler = MagicMock()
    handler.find_memory_by_version = AsyncMock()
    handler.update_effect_tracking = AsyncMock(return_value=True)
    return handler


@pytest.fixture
def mock_version_manager():
    """创建 VersionManager 的 mock 实例"""
    vm = MagicMock()
    vm.get_active_version = MagicMock(return_value="V20260804")
    return vm


# ============================================================
# 测试用例 1：正常追踪流程
# ============================================================


class TestNormalTracking:
    """测试正常追踪流程"""

    @pytest.mark.asyncio
    async def test_full_tracking_with_data(self, base_config, mock_adapter, mock_db_handler, mock_version_manager):
        """验证完整追踪流程：有记忆记录，adapter.collect 返回有效数据"""
        # 模拟有记忆记录
        mock_db_handler.find_memory_by_version.return_value = {
            "id": 42,
            "strategy_id": "btc_eth_grid_v1",
            "active_version": "V20260804",
            "post_win_rate": None,
            "post_total_pnl": None,
            "effect_notes": None,
            "full_report": {
                "performance": {
                    "win_rate": 0.50,
                    "total_pnl": 100.0,
                },
            },
        }

        tracker = EffectTracker(base_config, version_manager=mock_version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=mock_adapter,
            db_handler=mock_db_handler,
        )

        # 验证 EffectSummary 字段
        assert result.has_data is True
        assert result.memory_id == 42
        assert result.pre_win_rate == 0.50
        assert result.pre_total_pnl == 100.0
        assert result.post_win_rate == 0.65  # 来自 mock_adapter.collect
        assert result.post_total_pnl == 150.0
        assert result.win_rate_change == pytest.approx(0.15, rel=1e-9)  # 0.65 - 0.50（浮点精度）
        assert result.pnl_change == 50.0  # 150.0 - 100.0
        assert result.total_trades == 10
        assert result.original_version == "V20260804"
        # 胜率提升 15% >= 3%，评级应为"良好"
        assert result.rating == "良好"

        # 验证回填调用
        mock_db_handler.update_effect_tracking.assert_awaited_once_with(
            memory_id=42,
            post_win_rate=0.65,
            post_total_pnl=150.0,
            notes=result.notes,
        )

    @pytest.mark.asyncio
    async def test_rating_good_at_boundary(self, base_config, mock_db_handler, mock_version_manager):
        """验证胜率变化正好 3% 时评为"良好"（阈值边界）"""
        mock_db_handler.find_memory_by_version.return_value = {
            "id": 1,
            "strategy_id": "btc_eth_grid_v1",
            "active_version": "V20260804",
            "post_win_rate": None,
            "post_total_pnl": None,
            "effect_notes": None,
            "full_report": {
                "performance": {
                    "win_rate": 0.50,
                    "total_pnl": 0.0,
                },
            },
        }

        # 构造 collect 返回：胜率正好提升 3%
        perf = PerformanceMetrics(
            total_trades=5,
            win_count=3,
            loss_count=2,
            win_rate=0.53,  # 0.53 - 0.50 = 0.03，正好 3%
            total_pnl=10.0,
            avg_win=5.0,
            avg_loss=3.0,
            profit_factor=1.2,
            sharpe_approx=0.8,
        )
        risk = RiskMetrics(max_drawdown_pct=0.05)
        adapter = MagicMock()
        adapter.config_path = "strategies/btc_eth/config.yaml"
        adapter.collect = AsyncMock(return_value=StrategyReport(performance=perf, risk=risk))

        tracker = EffectTracker(base_config, version_manager=mock_version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=adapter,
            db_handler=mock_db_handler,
        )

        # 胜率变化 0.03 >= 0.03，评为"良好"
        assert result.rating == "良好"
        assert result.win_rate_change == pytest.approx(0.03, rel=1e-9)


# ============================================================
# 测试用例 2：无历史记忆
# ============================================================


class TestNoHistory:
    """测试无历史记忆的情况"""

    @pytest.mark.asyncio
    async def test_no_memory_record(self, base_config, mock_adapter, mock_db_handler, mock_version_manager):
        """验证无记忆记录时返回 has_data=False"""
        mock_db_handler.find_memory_by_version.return_value = None

        tracker = EffectTracker(base_config, version_manager=mock_version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=mock_adapter,
            db_handler=mock_db_handler,
        )

        assert result.has_data is False
        assert result.rating == "数据不足"
        # 不应调用 update_effect_tracking
        mock_db_handler.update_effect_tracking.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adapter_no_config_path(self, base_config, mock_db_handler, mock_version_manager):
        """验证 adapter.config_path 为空时跳过"""
        adapter = MagicMock()
        adapter.config_path = ""

        tracker = EffectTracker(base_config, version_manager=mock_version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=adapter,
            db_handler=mock_db_handler,
        )

        assert result.has_data is False


# ============================================================
# 测试用例 3：collect 异常
# ============================================================


class TestCollectException:
    """测试 collect 异常处理"""

    @pytest.mark.asyncio
    async def test_collect_raises_exception(self, base_config, mock_db_handler, mock_version_manager):
        """验证 adapter.collect 抛出异常时返回 has_data=False"""
        mock_db_handler.find_memory_by_version.return_value = {
            "id": 1,
            "strategy_id": "btc_eth_grid_v1",
            "active_version": "V20260804",
            "post_win_rate": None,
            "post_total_pnl": None,
            "effect_notes": None,
            "full_report": {},
        }

        adapter = MagicMock()
        adapter.config_path = "strategies/btc_eth/config.yaml"
        adapter.collect = AsyncMock(side_effect=RuntimeError("数据库连接失败"))

        tracker = EffectTracker(base_config, version_manager=mock_version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=adapter,
            db_handler=mock_db_handler,
        )

        assert result.has_data is False


# ============================================================
# 测试用例 4：.active 文件不存在
# ============================================================


class TestActiveFileMissing:
    """测试 .active 文件不存在的情况"""

    @pytest.mark.asyncio
    async def test_active_version_none(self, base_config, mock_adapter, mock_db_handler):
        """验证 version_manager.get_active_version 返回 None 时跳过"""
        version_manager = MagicMock()
        version_manager.get_active_version = MagicMock(return_value=None)

        tracker = EffectTracker(base_config, version_manager=version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=mock_adapter,
            db_handler=mock_db_handler,
        )

        assert result.has_data is False
        assert result.original_version == ""
        mock_db_handler.find_memory_by_version.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_version_empty_string(self, base_config, mock_adapter, mock_db_handler):
        """验证 version_manager.get_active_version 返回空字符串时跳过"""
        version_manager = MagicMock()
        version_manager.get_active_version = MagicMock(return_value="")

        tracker = EffectTracker(base_config, version_manager=version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=mock_adapter,
            db_handler=mock_db_handler,
        )

        assert result.has_data is False


# ============================================================
# 测试用例 5：多个历史记忆
# ============================================================


class TestMultipleHistoryRecords:
    """测试多条历史记忆，只回填最新一条"""

    @pytest.mark.asyncio
    async def test_only_latest_record_filled(self, base_config, mock_adapter, mock_version_manager):
        """验证多条记忆记录时，只回填最新一条"""
        # find_memory_by_version 返回最新一条
        mock_db_handler = MagicMock()
        mock_db_handler.find_memory_by_version = AsyncMock(
            return_value={
                "id": 99,
                "strategy_id": "btc_eth_grid_v1",
                "active_version": "V20260804",
                "post_win_rate": None,
                "post_total_pnl": None,
                "effect_notes": None,
                "full_report": {
                    "performance": {
                        "win_rate": 0.55,
                        "total_pnl": 200.0,
                    },
                },
            }
        )
        mock_db_handler.update_effect_tracking = AsyncMock(return_value=True)

        tracker = EffectTracker(base_config, version_manager=mock_version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=mock_adapter,
            db_handler=mock_db_handler,
        )

        # 验证回填的是最新一条记录
        assert result.memory_id == 99
        assert result.pre_win_rate == 0.55
        mock_db_handler.update_effect_tracking.assert_awaited_once_with(
            memory_id=99,
            post_win_rate=0.65,
            post_total_pnl=150.0,
            notes=result.notes,
        )


# ============================================================
# 测试用例 6：幂等性检查
# ============================================================


class TestIdempotency:
    """测试已填充的 post_* 字段不重复计算"""

    @pytest.mark.asyncio
    async def test_already_filled_skips_collect(self, base_config, mock_adapter, mock_db_handler, mock_version_manager):
        """验证 post_* 字段已填充时跳过 collect 和回填"""
        mock_db_handler.find_memory_by_version.return_value = {
            "id": 42,
            "strategy_id": "btc_eth_grid_v1",
            "active_version": "V20260804",
            "post_win_rate": 0.68,
            "post_total_pnl": 180.0,
            "effect_notes": "评级：良好；胜率变化：+15.0%；盈亏变化：+50.00 USDT；交易笔数：10",
            "full_report": {
                "performance": {
                    "win_rate": 0.50,
                    "total_pnl": 100.0,
                },
            },
        }

        tracker = EffectTracker(base_config, version_manager=mock_version_manager)
        result = await tracker.track_and_fill(
            strategy_id="btc_eth_grid_v1",
            adapter=mock_adapter,
            db_handler=mock_db_handler,
        )

        # 验证返回了已填充的数据
        assert result.has_data is True
        assert result.memory_id == 42
        assert result.post_win_rate == 0.68
        assert result.post_total_pnl == 180.0
        # 验证未调用 collect 和 update_effect_tracking
        mock_adapter.collect.assert_not_awaited()
        mock_db_handler.update_effect_tracking.assert_not_awaited()


# ============================================================
# 测试用例 7：_calc_rating 函数单元测试
# ============================================================


class TestCalcRating:
    """测试 _calc_rating 评级逻辑"""

    def test_rating_good_win_rate(self, base_config):
        """胜率提升超过阈值，评为良好"""
        tracker = EffectTracker(base_config)
        rating = tracker._calc_rating({
            "win_rate_change": 0.05,
            "pnl_change": 1.0,
            "total_trades": 10,
        })
        assert rating == "良好"

    def test_rating_good_pnl(self, base_config):
        """收益提升超过阈值，评为良好"""
        tracker = EffectTracker(base_config)
        rating = tracker._calc_rating({
            "win_rate_change": 0.01,
            "pnl_change": 10.0,
            "total_trades": 10,
        })
        assert rating == "良好"

    def test_rating_poor(self, base_config):
        """胜率下降且收益下降，评为较差"""
        tracker = EffectTracker(base_config)
        rating = tracker._calc_rating({
            "win_rate_change": -0.05,
            "pnl_change": -10.0,
            "total_trades": 10,
        })
        assert rating == "较差"

    def test_rating_average(self, base_config):
        """不符合良好和较差条件，评为一般"""
        tracker = EffectTracker(base_config)
        rating = tracker._calc_rating({
            "win_rate_change": 0.01,
            "pnl_change": 1.0,
            "total_trades": 10,
        })
        assert rating == "一般"

    def test_rating_insufficient_data(self, base_config):
        """交易笔数不足，评为数据不足"""
        tracker = EffectTracker(base_config)
        rating = tracker._calc_rating({
            "win_rate_change": 0.05,
            "pnl_change": 10.0,
            "total_trades": 1,
        })
        assert rating == "数据不足"

    def test_rating_zero_trades(self, base_config):
        """交易笔数为 0，评为数据不足"""
        tracker = EffectTracker(base_config)
        rating = tracker._calc_rating({
            "win_rate_change": 0.0,
            "pnl_change": 0.0,
            "total_trades": 0,
        })
        assert rating == "数据不足"


# ============================================================
# 测试用例 8：_build_effect_notes 函数单元测试
# ============================================================


class TestBuildEffectNotes:
    """测试 _build_effect_notes 备注文本构建"""

    def test_notes_with_positive_changes(self, base_config):
        """正收益变化时备注包含 + 号"""
        tracker = EffectTracker(base_config)
        notes = tracker._build_effect_notes("良好", 0.05, 100.0, 10)
        assert "评级：良好" in notes
        assert "胜率变化：+5.0%" in notes
        assert "盈亏变化：+100.00 USDT" in notes
        assert "交易笔数：10" in notes

    def test_notes_with_negative_changes(self, base_config):
        """负收益变化时备注包含 - 号"""
        tracker = EffectTracker(base_config)
        notes = tracker._build_effect_notes("较差", -0.03, -50.0, 8)
        assert "评级：较差" in notes
        assert "胜率变化：-3.0%" in notes
        assert "盈亏变化：-50.00 USDT" in notes

    def test_notes_with_zero_trades(self, base_config):
        """交易笔数为 0 时只显示评级"""
        tracker = EffectTracker(base_config)
        notes = tracker._build_effect_notes("数据不足", 0.0, 0.0, 0)
        assert "评级：数据不足" in notes
        assert "胜率变化" not in notes
        assert "盈亏变化" not in notes
        assert "交易笔数" not in notes