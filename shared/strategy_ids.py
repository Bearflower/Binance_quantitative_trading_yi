"""
策略唯一 id 常量（单一事实源）

本次改动前，dashboard DataService._PERF_STRATEGY_IDS 和
ai_tuner UnifiedPerformanceReader 各自硬编码一份，
极易在新增/删除策略时遗漏一处。集中到此模块，各处 import 引用。

注意：仅包含参与统一绩效计算的策略（不包括已下线或独立部署的）。
"""

# 参与统一绩效计算的策略规范 id 列表（去重保序）
PERFORMANCE_STRATEGY_IDS = [
    "btc_eth",
    "btc_eth_aggressive",
    "new_coin",
    "hrs",
    "grid",
]
