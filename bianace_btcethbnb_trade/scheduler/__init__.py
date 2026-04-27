#!/usr/bin/env python3
"""
调度器模块

基于 traderule.txt 的规则引擎调度器：
- 每小时执行一次行情分析和信号检测
- 自动执行符合规则的交易
- 完整的风险控制

模块结构：
- scheduler.py: 调度器核心（APScheduler配置）
- analyzer.py: 分析流程（信号检测、评分）
- trade_executor.py: 交易执行（开仓、平仓）
- statistics.py: 统计功能（胜率、日报）
- notifier.py: 通知功能（飞书推送）
"""

from scheduler.scheduler import RuleEngineScheduler, run_scheduler
from scheduler.analyzer import MarketAnalyzer
from scheduler.trade_executor import TradeExecutor
from scheduler.statistics import StatisticsManager
from scheduler.notifier import NotificationManager

__all__ = [
    'RuleEngineScheduler',
    'run_scheduler',
    'MarketAnalyzer',
    'TradeExecutor',
    'StatisticsManager',
    'NotificationManager',
]
