"""XAUUSDT MTCPS 影子仿真 — 第二阶段：影子采集器

只读生成虚拟决策事件、落 research schema，绝不调用写接口、不建在线持仓。
"""

from .instance import ShadowInstanceConfig, parse_shadow_instance_config

__all__ = [
    "ShadowInstanceConfig",
    "parse_shadow_instance_config",
]
