"""
测试上下文构建器（ContextBuilder）

覆盖用例：
- 包含 reasoning 的上下文构建
- 不包含 reasoning 的上下文构建
- 无历史记录的情况
- reasoning 截断（超过200字）
- 异常处理
"""

import sys

import pytest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")

from ai_tuner.memory.context_builder import ContextBuilder


@pytest.fixture
def mock_db_handler():
    """创建 MemoryDBHandler 的 mock 实例"""
    handler = MagicMock()
    handler.get_recent_memories = AsyncMock()
    return handler


@pytest.fixture
def builder():
    """创建 ContextBuilder 实例（窗口大小为 5）"""
    return ContextBuilder(context_window_size=5)


@pytest.fixture
def sample_current_report():
    """模拟当前策略报告"""
    return {
        "meta": {
            "strategy_name": "测试策略",
            "version": "v1.0",
            "week_start": "2026-06-22",
            "week_end": "2026-06-28",
        },
        "metrics": {
            "total_pnl": 1250.50,
            "win_rate": 0.62,
        },
    }


# ============================================================
# 测试用例 1：包含 reasoning 的上下文构建
# ============================================================


class TestBuildContextWithReasoning:
    """测试包含 reasoning 的上下文构建"""

    @pytest.mark.asyncio
    async def test_reasoning_included(self, builder, mock_db_handler, sample_current_report):
        """验证 reasoning 内容被正确包含在输出中"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整网格间距从 0.5% 到 0.8%",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 320.50,
                "ai_suggestions": {
                    "reasoning": "历史胜率偏低，建议扩大网格间距以提高单笔盈利",
                },
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        # 验证标题
        assert "btc_eth_grid_v1 历史调优简史" in context
        # 验证摘要
        assert "调整网格间距从 0.5% 到 0.8%" in context
        # 验证效果行
        assert "应用后胜率: 65.0%" in context
        assert "应用后盈亏: 320.50 USDT" in context
        # 验证推理要点
        assert "推理要点:" in context
        assert "历史胜率偏低，建议扩大网格间距以提高单笔盈利" in context
        # 验证数据库调用参数
        mock_db_handler.get_recent_memories.assert_awaited_once_with(
            "btc_eth_grid_v1", limit=5
        )

    @pytest.mark.asyncio
    async def test_reasoning_only_win_rate(self, builder, mock_db_handler, sample_current_report):
        """验证只有胜率没有盈亏时，效果行只显示胜率"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整网格间距",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": None,
                "ai_suggestions": {
                    "reasoning": "仅调整胜率相关参数",
                },
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "应用后胜率: 65.0%" in context
        assert "应用后盈亏" not in context
        assert "推理要点: 仅调整胜率相关参数" in context

    @pytest.mark.asyncio
    async def test_reasoning_only_pnl(self, builder, mock_db_handler, sample_current_report):
        """验证只有盈亏没有胜率时，效果行只显示盈亏"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整止损参数",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": None,
                "post_total_pnl": -150.75,
                "ai_suggestions": {
                    "reasoning": "止损收窄后亏损减少",
                },
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "应用后胜率" not in context
        assert "应用后盈亏: -150.75 USDT" in context
        assert "推理要点: 止损收窄后亏损减少" in context

    @pytest.mark.asyncio
    async def test_multiple_memories(self, builder, mock_db_handler, sample_current_report):
        """验证多条记忆时正确编号"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "第二次调优",
                "created_at": "2026-06-28 10:00:00",
                "post_win_rate": 0.70,
                "post_total_pnl": 500.00,
                "ai_suggestions": {"reasoning": "第二次推理"},
            },
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "第一次调优",
                "created_at": "2026-06-21 10:00:00",
                "post_win_rate": 0.60,
                "post_total_pnl": 200.00,
                "ai_suggestions": {"reasoning": "第一次推理"},
            },
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "第1次调优" in context
        assert "第2次调优" in context
        assert "第二次推理" in context
        assert "第一次推理" in context
        # 验证显示的记忆数量
        assert "最近 2 次" in context


# ============================================================
# 测试用例 2：不包含 reasoning 的上下文构建
# ============================================================


class TestBuildContextWithoutReasoning:
    """测试不包含 reasoning 的上下文构建"""

    @pytest.mark.asyncio
    async def test_no_ai_suggestions_key(self, builder, mock_db_handler, sample_current_report):
        """验证没有 ai_suggestions 字段时，不显示推理要点"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整网格间距",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 320.50,
                # 没有 ai_suggestions 字段
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "推理要点:" not in context
        assert "调整网格间距" in context

    @pytest.mark.asyncio
    async def test_empty_ai_suggestions(self, builder, mock_db_handler, sample_current_report):
        """验证 ai_suggestions 为空字典时，不显示推理要点"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整网格间距",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 320.50,
                "ai_suggestions": {},
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "推理要点:" not in context

    @pytest.mark.asyncio
    async def test_ai_suggestions_is_none(self, builder, mock_db_handler, sample_current_report):
        """验证 ai_suggestions 为 None 时，不显示推理要点"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整网格间距",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 320.50,
                "ai_suggestions": None,
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "推理要点:" not in context

    @pytest.mark.asyncio
    async def test_ai_suggestions_not_dict(self, builder, mock_db_handler, sample_current_report):
        """验证 ai_suggestions 不是 dict 类型时，不显示推理要点"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整网格间距",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 320.50,
                "ai_suggestions": "非字典字符串",
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "推理要点:" not in context

    @pytest.mark.asyncio
    async def test_reasoning_empty_string(self, builder, mock_db_handler, sample_current_report):
        """验证 reasoning 为空字符串时，不显示推理要点"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整网格间距",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 320.50,
                "ai_suggestions": {"reasoning": ""},
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "推理要点:" not in context

    @pytest.mark.asyncio
    async def test_no_effect_data(self, builder, mock_db_handler, sample_current_report):
        """验证没有效果数据时，不显示效果行"""
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "调整网格间距",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": None,
                "post_total_pnl": None,
                "ai_suggestions": {"reasoning": "无需效果追踪"},
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert "应用后胜率" not in context
        assert "应用后盈亏" not in context
        assert "推理要点: 无需效果追踪" in context


# ============================================================
# 测试用例 3：无历史记录的情况
# ============================================================


class TestBuildContextNoHistory:
    """测试无历史记录的情况"""

    @pytest.mark.asyncio
    async def test_empty_memories(self, builder, mock_db_handler, sample_current_report):
        """验证无历史记录时返回首次调优提示"""
        mock_db_handler.get_recent_memories.return_value = []

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert context == "暂无历史调优记录，这是首次调优。"

    @pytest.mark.asyncio
    async def test_none_memories(self, builder, mock_db_handler, sample_current_report):
        """验证返回 None 时（非空列表但 falsy）也触发首次调优提示"""
        mock_db_handler.get_recent_memories.return_value = []

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert context == "暂无历史调优记录，这是首次调优。"


# ============================================================
# 测试用例 4：reasoning 截断（超过200字）
# ============================================================


class TestBuildContextReasoningTruncation:
    """测试 reasoning 截断逻辑"""

    @pytest.mark.asyncio
    async def test_reasoning_exactly_200_chars(self, builder, mock_db_handler, sample_current_report):
        """验证 reasoning 恰好 200 字时不截断"""
        reasoning = "调" * 200
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "精确长度测试",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 100.00,
                "ai_suggestions": {"reasoning": reasoning},
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        # 恰好 200 字，不截断，因此不应有 "..."
        assert "推理要点:" in context
        assert "..." not in context
        assert reasoning[:50] in context

    @pytest.mark.asyncio
    async def test_reasoning_exceeds_200_chars(self, builder, mock_db_handler, sample_current_report):
        """验证 reasoning 超过 200 字时截断并添加 ..."""
        reasoning = "调" * 250
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "超长推理测试",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 100.00,
                "ai_suggestions": {"reasoning": reasoning},
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        # 验证截断后的内容（200 个"调" + "..."）
        assert "推理要点:" in context
        truncated = reasoning[:200]
        assert truncated in context
        assert "..." in context
        # 验证推理要点行总长度
        lines = context.split("\n")
        reasoning_line = next(line for line in lines if "推理要点:" in line)
        # 推理要点行格式：`  推理要点: <200字>...`
        assert len(reasoning_line) - len("  推理要点: ") == 200 + 3  # 200 字 + "..."

    @pytest.mark.asyncio
    async def test_reasoning_201_chars(self, builder, mock_db_handler, sample_current_report):
        """验证 reasoning 刚好 201 字时截断"""
        reasoning = "调" * 201
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "边界长度测试",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 100.00,
                "ai_suggestions": {"reasoning": reasoning},
            }
        ]

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        # 201 > 200，应被截断
        assert "..." in context
        assert reasoning[:200] in context


# ============================================================
# 测试用例 5：异常处理
# ============================================================


class TestBuildContextExceptionHandling:
    """测试异常处理"""

    @pytest.mark.asyncio
    async def test_db_query_raises_exception(self, builder, mock_db_handler, sample_current_report):
        """验证数据库查询异常时返回降级提示"""
        mock_db_handler.get_recent_memories.side_effect = Exception("数据库连接超时")

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert context == "历史调优记忆获取失败，请仅基于当前数据进行判断。"

    @pytest.mark.asyncio
    async def test_db_query_raises_value_error(self, builder, mock_db_handler, sample_current_report):
        """验证 ValueError 异常也能被捕获"""
        mock_db_handler.get_recent_memories.side_effect = ValueError("无效的策略ID")

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert context == "历史调优记忆获取失败，请仅基于当前数据进行判断。"

    @pytest.mark.asyncio
    async def test_db_query_raises_runtime_error(self, builder, mock_db_handler, sample_current_report):
        """验证 RuntimeError 异常也能被捕获"""
        mock_db_handler.get_recent_memories.side_effect = RuntimeError("服务不可用")

        context = await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        assert context == "历史调优记忆获取失败，请仅基于当前数据进行判断。"


# ============================================================
# 测试用例 6：边缘情况 - 上下文窗口大小
# ============================================================


class TestBuildContextWindowSize:
    """测试窗口大小参数传递"""

    @pytest.mark.asyncio
    async def test_window_size_passed_correctly(self, sample_current_report):
        """验证 context_window_size 正确传递给 get_recent_memories"""
        builder = ContextBuilder(context_window_size=3)
        mock_db_handler = MagicMock()
        mock_db_handler.get_recent_memories = AsyncMock()
        mock_db_handler.get_recent_memories.return_value = [
            {
                "strategy_id": "btc_eth_grid_v1",
                "summary": "测试",
                "created_at": "2026-06-25 10:00:00",
                "post_win_rate": 0.65,
                "post_total_pnl": 100.00,
                "ai_suggestions": {"reasoning": "测试推理"},
            }
        ]

        await builder.build_context(
            strategy_id="btc_eth_grid_v1",
            db_handler=mock_db_handler,
            current_report=sample_current_report,
        )

        mock_db_handler.get_recent_memories.assert_awaited_once_with(
            "btc_eth_grid_v1", limit=3
        )