# -*- coding: utf-8 -*-
"""MVP 阶段一：学习信号 L4 禁用强制调参（learning_signal.py 修改点）

- L4（连续 HOLD 触发）在阶段一必须恒返回 None，不得为"调参而调参"
- db_handler（加 _ 前缀的幽灵参数）不得被调用
"""

import pytest

from ai_tuner.feedback.learning_signal import LearningSignalGenerator


@pytest.fixture
def generator(tuner_config):
    return LearningSignalGenerator(tuner_config)


class TestL4Disabled:
    async def test_l4_always_returns_none(self, generator):
        """L4 无论历史如何都返回 None（阶段一禁用强制调参）"""
        boom = object()  # 任何被误用的对象都会让测试暴露调用行为
        result = await generator._apply_l4_stale_trigger(boom, "hrs")
        assert result is None

    async def test_l4_does_not_touch_db_handler(self, generator):
        """L4 不查询数据库（db_handler 参数仅为兼容保留）"""
        from unittest.mock import MagicMock

        handler = MagicMock()
        result = await generator._apply_l4_stale_trigger(handler, "btc_eth_aggressive")
        assert result is None
        handler.assert_not_called()

    async def test_build_learning_instructions_with_no_history(self, generator):
        """无历史数据时返回占位文本，不触发 L4"""
        from ai_tuner.feedback.effect_tracker import EffectSummary

        summary = EffectSummary(has_data=False)
        text = await generator.build_learning_instructions(
            strategy_id="hrs",
            effect_summary=summary,
            db_handler=None,
            current_report={},
        )
        assert "暂无历史调优数据" in text
