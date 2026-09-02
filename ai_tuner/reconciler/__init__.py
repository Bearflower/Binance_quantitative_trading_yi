"""
统一 PnL 校准器模块

实现 IncomeReconciler：从 Binance income API 拉取 REALIZED_PNL 权威盈亏数据，
兜底补齐 databases.trade_records 中缺失的 realized_pnl，确保数据库盈亏与
Binance 实际一致，供 AI 优化建议可信使用。
"""

from .income_reconciler import IncomeReconciler

__all__ = ["IncomeReconciler"]