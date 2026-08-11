"""
StratTuneAI 反馈闭环模块

提供效果追踪、上下文增强、学习信号生成三大核心功能，
打通"调优建议 -> 实际效果 -> 反馈给下次调优"的完整链路。

对外暴露的类：
- EffectTracker: 效果追踪器，计算上周实际表现，回填 post_* 字段
- ContextEnhancer: 上下文增强器，将效果摘要格式化为 LLM 可理解的上下文
- LearningSignalGenerator: 学习信号生成器，基于效果评级生成规则指令
"""

from ai_tuner.feedback.effect_tracker import EffectTracker, EffectSummary
from ai_tuner.feedback.context_enhancer import ContextEnhancer
from ai_tuner.feedback.learning_signal import LearningSignalGenerator

__all__ = [
    "EffectTracker",
    "EffectSummary",
    "ContextEnhancer",
    "LearningSignalGenerator",
]