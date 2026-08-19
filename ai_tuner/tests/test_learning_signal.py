"""
测试学习信号生成器（LearningSignalGenerator）

覆盖用例：
- L1 良好评级：rating="良好" → 输出含"建议延续上次方向"
- L1 较差评级：rating="较差" → 输出含"建议回撤"
- L2 避免过度优化：连续同方向调整次数超过阈值 → 指令含减少调整幅度
- L3 零交易处理：上周交易笔数为 0 → 指令含"数据不足"
- L4 连续不变触发：连续 N 次"维持不变" → 指令含调整幅度减半
- 配置缺失 fallback：config 中缺少 instructions 配置 → 使用 _DEFAULT_INSTRUCTIONS
- 数据库查询异常：db_handler.get_recent_applied_memories 抛出异常 → 不阻断，返回基础指令
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, ".")

from ai_tuner.feedback.effect_tracker import EffectSummary
from ai_tuner.feedback.learning_signal import LearningSignalGenerator


@pytest.fixture
def base_config():
    """基础配置"""
    return {
        "feedback": {
            "learning": {
                "consecutive_same_direction": 2,
                "stale_unchanged_count": 3,
                "stale_adjustment_ratio": 0.5,
            },
            "rating": {
                "min_trades_for_valid": 3,
            },
            "instructions": {
                "l1_good": "上次调优效果良好，建议延续上次参数调整方向，可在此基础上进一步微调（调整幅度不超过上次的 50%）",
                "l1_fair": "上次调优效果一般，建议谨慎评估当前参数，如有必要可小幅调整或维持不变",
                "l1_poor": "上次调优效果较差，建议回撤上次调整，或朝相反方向调整（如上次上调了某参数，本次应考虑下调）",
                "l1_insufficient": "上周交易数据不足，建议以更长时间维度的数据为准，不做基于噪音数据的调整",
            },
        },
    }


@pytest.fixture
def mock_db_handler():
    """创建 MemoryDBHandler 的 mock 实例"""
    handler = MagicMock()
    handler.get_recent_applied_memories = AsyncMock()
    return handler


@pytest.fixture
def sample_current_report():
    """模拟当前策略报告"""
    return {
        "meta": {
            "strategy_name": "测试策略",
            "version": "v1.0",
            "week_start": "2026-08-04",
            "week_end": "2026-08-10",
        },
        "metrics": {
            "total_pnl": 1250.50,
            "win_rate": 0.62,
        },
    }


# ============================================================
# 测试用例 1：L1 良好评级
# ============================================================


class TestL1GoodRating:
    """测试 L1 良好评级场景"""

    @pytest.mark.asyncio
    async def test_l1_good_output(self, base_config, mock_db_handler, sample_current_report):
        """验证 rating="良好" 时输出含"建议延续上次方向" """
        mock_db_handler.get_recent_applied_memories.return_value = [
            {"ai_suggestions": {"adjustments": {"scoring.min_score": {"from": 0.5, "to": 0.55}}}},
        ]

        effect_summary = EffectSummary(
            has_data=True,
            memory_id=1,
            pre_win_rate=0.50,
            pre_total_pnl=100.0,
            post_win_rate=0.65,
            post_total_pnl=200.0,
            win_rate_change=0.15,
            pnl_change=100.0,
            max_drawdown_pct=0.05,
            total_trades=10,
            rating="良好",
        )

        generator = LearningSignalGenerator(base_config)
        result = await generator.build_learning_instructions(
            strategy_id="btc_eth_grid_v1",
            effect_summary=effect_summary,
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "建议延续上次参数调整方向" in result
        assert "上次调优评级：良好" in result
        assert "胜率变化+15.0%" in result
        assert "盈亏变化+100.00 USDT" in result


# ============================================================
# 测试用例 2：L1 较差评级
# ============================================================


class TestL1PoorRating:
    """测试 L1 较差评级场景"""

    @pytest.mark.asyncio
    async def test_l1_poor_output(self, base_config, mock_db_handler, sample_current_report):
        """验证 rating="较差" 时输出含"建议回撤" """
        mock_db_handler.get_recent_applied_memories.return_value = [
            {"ai_suggestions": {"adjustments": {"scoring.min_score": {"from": 0.5, "to": 0.55}}}},
        ]

        effect_summary = EffectSummary(
            has_data=True,
            memory_id=1,
            pre_win_rate=0.50,
            pre_total_pnl=100.0,
            post_win_rate=0.45,
            post_total_pnl=50.0,
            win_rate_change=-0.05,
            pnl_change=-50.0,
            max_drawdown_pct=0.15,
            total_trades=10,
            rating="较差",
        )

        generator = LearningSignalGenerator(base_config)
        result = await generator.build_learning_instructions(
            strategy_id="btc_eth_grid_v1",
            effect_summary=effect_summary,
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "建议回撤上次调整" in result
        assert "上次调优评级：较差" in result
        assert "胜率变化-5.0%" in result
        assert "盈亏变化-50.00 USDT" in result


# ============================================================
# 测试用例 3：L2 避免过度优化
# ============================================================


class TestL2AvoidOverOptimization:
    """测试 L2 避免过度优化"""

    @pytest.mark.asyncio
    async def test_consecutive_same_direction_warning(self, base_config, mock_db_handler, sample_current_report):
        """验证连续同方向调整超过阈值时输出警告"""
        # 连续 2 次同方向上调
        mock_db_handler.get_recent_applied_memories.return_value = [
            {
                "ai_suggestions": {
                    "adjustments": {
                        "scoring.min_score": {"from": 0.55, "to": 0.60},
                    },
                },
            },
            {
                "ai_suggestions": {
                    "adjustments": {
                        "scoring.min_score": {"from": 0.50, "to": 0.55},
                    },
                },
            },
        ]

        effect_summary = EffectSummary(
            has_data=True,
            memory_id=1,
            pre_win_rate=0.50,
            pre_total_pnl=100.0,
            post_win_rate=0.52,
            post_total_pnl=110.0,
            win_rate_change=0.02,
            pnl_change=10.0,
            total_trades=10,
            rating="良好",
        )

        generator = LearningSignalGenerator(base_config)
        result = await generator.build_learning_instructions(
            strategy_id="btc_eth_grid_v1",
            effect_summary=effect_summary,
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "注意事项" in result
        assert "已连续多次朝同一方向调整策略参数" in result


# ============================================================
# 测试用例 4：L3 零交易处理
# ============================================================


class TestL3ZeroTrades:
    """测试 L3 零交易处理"""

    @pytest.mark.asyncio
    async def test_insufficient_trades_warning(self, base_config, mock_db_handler, sample_current_report):
        """验证上周交易笔数为 0 时输出含"数据不足" """
        mock_db_handler.get_recent_applied_memories.return_value = [
            {"ai_suggestions": {"adjustments": {"scoring.min_score": {"from": 0.5, "to": 0.55}}}},
        ]

        effect_summary = EffectSummary(
            has_data=True,
            memory_id=1,
            pre_win_rate=0.50,
            pre_total_pnl=100.0,
            post_win_rate=0.0,
            post_total_pnl=0.0,
            win_rate_change=-0.50,
            pnl_change=-100.0,
            total_trades=0,
            rating="数据不足",
        )

        generator = LearningSignalGenerator(base_config)
        result = await generator.build_learning_instructions(
            strategy_id="btc_eth_grid_v1",
            effect_summary=effect_summary,
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "数据样本量不足" in result
        assert "0笔交易" in result


# ============================================================
# 测试用例 5：L4 连续不变触发
# ============================================================


class TestL4StaleTrigger:
    """测试 L4 连续不变触发"""

    @pytest.mark.asyncio
    async def test_consecutive_unchanged_trigger(self, base_config, mock_db_handler, sample_current_report):
        """验证连续 N 次维持不变时触发调整指令"""
        # 连续 3 次 adjustments 为空
        mock_db_handler.get_recent_applied_memories.return_value = [
            {"ai_suggestions": {"adjustments": {}}},
            {"ai_suggestions": {"adjustments": {}}},
            {"ai_suggestions": {"adjustments": {}}},
        ]

        effect_summary = EffectSummary(
            has_data=True,
            memory_id=1,
            pre_win_rate=0.50,
            pre_total_pnl=100.0,
            post_win_rate=0.55,
            post_total_pnl=150.0,
            win_rate_change=0.05,
            pnl_change=50.0,
            total_trades=10,
            rating="良好",
        )

        generator = LearningSignalGenerator(base_config)
        result = await generator.build_learning_instructions(
            strategy_id="btc_eth_grid_v1",
            effect_summary=effect_summary,
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        # L4 输出在"约束"段落
        assert "约束" in result
        assert "已连续3次维持不变" in result
        assert "调整幅度控制在正常范围的 50% 以内" in result


# ============================================================
# 测试用例 6：配置缺失 fallback
# ============================================================


class TestConfigFallback:
    """测试配置缺失时使用内置 fallback"""

    @pytest.mark.asyncio
    async def test_instructions_config_missing(self, mock_db_handler, sample_current_report):
        """验证 config 中缺少 instructions 时使用 _DEFAULT_INSTRUCTIONS"""
        mock_db_handler.get_recent_applied_memories.return_value = [
            {"ai_suggestions": {"adjustments": {"scoring.min_score": {"from": 0.5, "to": 0.55}}}},
        ]

        # config 中不包含 instructions 配置
        config = {
            "feedback": {
                "learning": {
                    "consecutive_same_direction": 2,
                    "stale_unchanged_count": 3,
                    "stale_adjustment_ratio": 0.5,
                },
                "rating": {
                    "min_trades_for_valid": 3,
                },
                # 没有 instructions 字段
            },
        }

        effect_summary = EffectSummary(
            has_data=True,
            memory_id=1,
            pre_win_rate=0.50,
            pre_total_pnl=100.0,
            post_win_rate=0.65,
            post_total_pnl=200.0,
            win_rate_change=0.15,
            pnl_change=100.0,
            total_trades=10,
            rating="良好",
        )

        generator = LearningSignalGenerator(config)
        result = await generator.build_learning_instructions(
            strategy_id="btc_eth_grid_v1",
            effect_summary=effect_summary,
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        # 应使用内置 fallback 指令
        assert "建议延续上次参数调整方向" in result


# ============================================================
# 测试用例 7：数据库查询异常
# ============================================================


class TestDbException:
    """测试数据库查询异常时的不阻断行为"""

    @pytest.mark.asyncio
    async def test_db_exception_does_not_block(self, base_config, mock_db_handler, sample_current_report):
        """验证 get_recent_applied_memories 抛出异常时不阻断，返回基础指令"""
        mock_db_handler.get_recent_applied_memories.side_effect = Exception("数据库连接超时")

        effect_summary = EffectSummary(
            has_data=True,
            memory_id=1,
            pre_win_rate=0.50,
            pre_total_pnl=100.0,
            post_win_rate=0.65,
            post_total_pnl=200.0,
            win_rate_change=0.15,
            pnl_change=100.0,
            total_trades=10,
            rating="良好",
        )

        generator = LearningSignalGenerator(base_config)
        result = await generator.build_learning_instructions(
            strategy_id="btc_eth_grid_v1",
            effect_summary=effect_summary,
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        # 即使数据库异常，L1 指令仍应输出
        assert "建议延续上次参数调整方向" in result
        assert "上次调优评级：良好" in result


# ============================================================
# 测试用例 8：无效果数据时的处理
# ============================================================


class TestNoEffectData:
    """测试无效果数据时的处理"""

    @pytest.mark.asyncio
    async def test_no_history_context(self, base_config, mock_db_handler, sample_current_report):
        """验证 has_data=False 时返回无历史数据提示"""
        effect_summary = EffectSummary(has_data=False)

        generator = LearningSignalGenerator(base_config)
        result = await generator.build_learning_instructions(
            strategy_id="btc_eth_grid_v1",
            effect_summary=effect_summary,
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "暂无历史调优数据" in result
        assert "请基于当前数据做判断" in result