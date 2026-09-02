"""
网格策略专属指标计算器

计算回测结果的各项指标：
- 收益率类：总收益率、年化收益率、总盈亏
- 风险类：最大回撤、夏普比率
- 网格专属：成交频率、单次成交平均利润、手续费占比、资金利用率
- 市况细分：上涨/下跌/横盘分别统计
- 综合评分：收益率×0.40 + 回撤×0.30 + 夏普×0.30
"""

import math
from typing import Any, Dict, List

import structlog

from ai_tuner.backtest.models import BacktestState, FillRecord

logger = structlog.get_logger()


class MetricsCalculator:
    """网格策略专属指标计算器

    所有计算均为纯Python实现，不依赖pandas。
    评分权重和计算参数从配置读取，禁止硬编码。
    """

    def __init__(
        self,
        return_weight: float = 0.40,
        drawdown_weight: float = 0.30,
        sharpe_weight: float = 0.30,
        return_score_scale: float = 10.0,
        drawdown_cap: float = 0.20,
        sharpe_score_scale: float = 50.0,
    ):
        """
        初始化指标计算器

        Args:
            return_weight: 综合评分中收益率权重（默认 0.40）
            drawdown_weight: 综合评分中最大回撤权重（默认 0.30）
            sharpe_weight: 综合评分中夏普比率权重（默认 0.30）
            return_score_scale: 收益率得分缩放系数（默认 10，即 10% 收益率 = 100 分）
            drawdown_cap: 回撤得分上限（默认 0.20，即 20% 回撤 = 0 分）
            sharpe_score_scale: 夏普得分缩放系数（默认 50，即 2.0 夏普 = 100 分）
        """
        self._return_weight = return_weight
        self._drawdown_weight = drawdown_weight
        self._sharpe_weight = sharpe_weight
        self._return_score_scale = return_score_scale
        self._drawdown_cap = drawdown_cap
        self._sharpe_score_scale = sharpe_score_scale

    def calculate(
        self,
        fills: List[FillRecord],
        equity_curve: List[float],
        final_state: BacktestState,
        kline_count: int,
        initial_capital: float,
        final_equity: float,
        symbol: str = "ETHUSDT",
    ) -> Dict[str, Any]:
        """
        计算所有回测指标

        Args:
            fills: 所有成交记录
            equity_curve: 权益曲线（每根K线后的权益值）
            final_state: 最终持仓状态
            kline_count: K线总数
            initial_capital: 初始资金
            final_equity: 最终权益
            symbol: 交易对符号（默认 ETHUSDT）

        Returns:
            包含所有指标的字典
        """
        # 收益类指标
        total_pnl = final_equity - initial_capital
        total_return_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0.0
        days = kline_count / 24  # 1h K线，转换为天数
        annualized_return_pct = self._calc_annualized_return(total_return_pct, days)

        # 风险类指标
        max_drawdown_pct = self._calc_max_drawdown_pct(equity_curve, initial_capital)
        sharpe_ratio = self._calc_sharpe_ratio(equity_curve, kline_count)

        # 网格专属指标
        fill_count = len(fills)
        avg_profit_per_fill = self._calc_avg_profit_per_fill(fills)
        fee_ratio = self._calc_fee_ratio(fills, total_pnl)
        capital_utilization = self._calc_capital_utilization(equity_curve, final_state, initial_capital)

        # 市况细分
        segmented = self._calc_segmented_returns(fills, equity_curve, initial_capital)

        # 构建结果字典
        result = {
            "scenario_name": "",
            "symbol": symbol,
            "kline_count": kline_count,
            "total_return_pct": round(total_return_pct, 4),
            "annualized_return_pct": round(annualized_return_pct, 4),
            "total_pnl": round(total_pnl, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "sharpe_ratio": round(sharpe_ratio, 4),
            "fill_count": fill_count,
            "avg_profit_per_fill": round(avg_profit_per_fill, 4),
            "fee_ratio": round(fee_ratio, 4),
            "capital_utilization": round(capital_utilization, 4),
            "uptrend_return_pct": round(segmented.get("上涨", 0.0), 4),
            "downtrend_return_pct": round(segmented.get("下跌", 0.0), 4),
            "sideways_return_pct": round(segmented.get("横盘", 0.0), 4),
        }

        # 综合评分
        result["composite_score"] = round(self._calc_composite_score(result), 1)

        return result

    # ============================================================
    # 收益类指标
    # ============================================================

    @staticmethod
    def _calc_annualized_return(total_return_pct: float, days: float) -> float:
        """
        计算年化收益率

        Args:
            total_return_pct: 总收益率（%）
            days: 回测天数

        Returns:
            年化收益率（%）
        """
        if days <= 0:
            return 0.0
        # 年化：总收益率 / 天数 * 365
        return (total_return_pct / days) * 365

    # ============================================================
    # 风险类指标
    # ============================================================

    @staticmethod
    def _calc_max_drawdown_pct(
        equity_curve: List[float], initial_capital: float
    ) -> float:
        """
        计算最大回撤百分比

        遍历权益曲线，跟踪权益峰值，计算从峰值回落的最大幅度。

        Args:
            equity_curve: 权益曲线
            initial_capital: 初始资金

        Returns:
            最大回撤百分比（0-1 之间的浮点数，如 0.15 表示 15%）
        """
        if not equity_curve or initial_capital <= 0:
            return 0.0

        peak = equity_curve[0]
        max_drawdown = 0.0

        for equity in equity_curve:
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak if peak > 0 else 0.0
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        return max_drawdown

    @staticmethod
    def _calc_sharpe_ratio(
        equity_curve: List[float], kline_count: int
    ) -> float:
        """
        计算周度夏普比率

        使用每根K线后的权益收益率序列计算。
        简化版：不使用无风险利率（加密市场环境下近似为0）。

        Args:
            equity_curve: 权益曲线
            kline_count: K线总数

        Returns:
            年化夏普比率
        """
        if len(equity_curve) < 2:
            return 0.0

        # 计算每期收益率
        period_returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]
            curr = equity_curve[i]
            if prev > 0:
                period_returns.append((curr - prev) / prev)
            else:
                period_returns.append(0.0)

        if not period_returns:
            return 0.0

        # 计算平均收益率
        avg_return = sum(period_returns) / len(period_returns)

        # 计算标准差
        if len(period_returns) >= 2:
            variance = sum((r - avg_return) ** 2 for r in period_returns) / (len(period_returns) - 1)
            std_return = math.sqrt(variance) if variance > 0 else 0.0
        else:
            std_return = 0.0

        if std_return == 0:
            return 0.0

        # 年化夏普比率（168根1h K线 = 1周）
        sharpe = (avg_return / std_return) * math.sqrt(kline_count)

        return sharpe

    # ============================================================
    # 网格专属指标
    # ============================================================

    @staticmethod
    def _calc_avg_profit_per_fill(fills: List[FillRecord]) -> float:
        """
        计算单次成交平均利润（USDT）

        通过配对 buy/sell 成交计算每对利润。
        简化处理：计算所有成交的名义价值净额除以成交次数。

        Args:
            fills: 所有成交记录

        Returns:
            单次成交平均利润（USDT）
        """
        if not fills:
            return 0.0

        # 计算净现金流 / 成交次数
        net_cash_flow = 0.0
        for f in fills:
            if f.direction == "buy":
                net_cash_flow -= f.notional + f.fee
            else:
                net_cash_flow += f.notional - f.fee

        return net_cash_flow / len(fills) if len(fills) > 0 else 0.0

    @staticmethod
    def _calc_fee_ratio(fills: List[FillRecord], total_pnl: float) -> float:
        """
        计算手续费占利润比（%）

        Args:
            fills: 所有成交记录
            total_pnl: 总盈亏

        Returns:
            手续费占利润比（%），如 30 表示 30%
        """
        if not fills:
            return 0.0

        total_fee = sum(f.fee for f in fills)
        if total_fee <= 0:
            return 0.0

        # 手续费占比 = 手续费 / (|总盈亏| + 手续费)
        # 避免总盈亏为负时出现负数百分比
        base = abs(total_pnl) + total_fee
        if base <= 0:
            return 0.0

        return (total_fee / base) * 100

    @staticmethod
    def _calc_capital_utilization(
        equity_curve: List[float],
        final_state: BacktestState,
        initial_capital: float,
    ) -> float:
        """
        计算资金利用率（%）

        资金利用率 = 平均持仓市值 / 总可用资金

        Args:
            equity_curve: 权益曲线
            final_state: 最终持仓状态
            initial_capital: 初始资金

        Returns:
            资金利用率（%），如 45 表示 45%
        """
        if not equity_curve or initial_capital <= 0:
            return 0.0

        # 估算平均持仓市值
        # 使用权益曲线变化来估算：权益曲线波动反映持仓变化
        # 简化：如果权益曲线有显著波动，说明资金被使用
        if len(equity_curve) < 2:
            return 0.0

        # 计算权益曲线标准差 / 初始资金，作为资金利用率的近似
        avg_equity = sum(equity_curve) / len(equity_curve)
        deviation_sum = sum(abs(e - avg_equity) for e in equity_curve)
        avg_deviation = deviation_sum / len(equity_curve)

        utilization = (avg_deviation / initial_capital) * 100
        return min(utilization, 100.0)

    # ============================================================
    # 市况细分
    # ============================================================

    @staticmethod
    def _calc_segmented_returns(
        fills: List[FillRecord],
        equity_curve: List[float],
        initial_capital: float,
    ) -> Dict[str, float]:
        """
        按市况分别统计收益率

        将成交按市况分组，分别计算每种市况下的收益率。

        Args:
            fills: 所有成交记录
            equity_curve: 权益曲线
            initial_capital: 初始资金

        Returns:
            {"上涨": uptrend_return_pct, "下跌": downtrend_return_pct, "横盘": sideways_return_pct}
        """
        if not fills or initial_capital <= 0:
            return {"上涨": 0.0, "下跌": 0.0, "横盘": 0.0}

        # 按市况分组计算净现金流
        regime_pnl: Dict[str, float] = {"上涨": 0.0, "下跌": 0.0, "横盘": 0.0}

        for f in fills:
            regime = f.regime if f.regime in regime_pnl else "横盘"
            if f.direction == "buy":
                regime_pnl[regime] -= f.notional + f.fee
            else:
                regime_pnl[regime] += f.notional - f.fee

        # 转换为百分比
        result = {}
        for regime, pnl in regime_pnl.items():
            result[regime] = (pnl / initial_capital) * 100

        return result

    # ============================================================
    # 综合评分
    # ============================================================

    def _calc_composite_score(self, result: Dict[str, Any]) -> float:
        """
        计算综合评分（0-100）

        综合评分 = 收益率得分 × 收益率权重 + 回撤得分 × 回撤权重 + 夏普得分 × 夏普权重

        各子分数计算：
        - 收益率得分 = min(100, max(0, total_return_pct × return_score_scale))
        - 回撤得分 = min(100, max(0, (1 - max_drawdown_pct / drawdown_cap) × 100))
        - 夏普得分 = min(100, max(0, sharpe_ratio × sharpe_score_scale))

        Args:
            result: 已计算各项指标的结果字典

        Returns:
            综合评分（0-100）
        """
        # 收益率得分
        total_return_pct = result.get("total_return_pct", 0.0)
        return_score = min(100.0, max(0.0, total_return_pct * self._return_score_scale))

        # 回撤得分
        max_drawdown_pct = result.get("max_drawdown_pct", 0.0)
        if self._drawdown_cap > 0:
            drawdown_score = min(100.0, max(0.0, (1 - max_drawdown_pct / self._drawdown_cap) * 100))
        else:
            drawdown_score = 100.0

        # 夏普得分
        sharpe_ratio = result.get("sharpe_ratio", 0.0)
        sharpe_score = min(100.0, max(0.0, sharpe_ratio * self._sharpe_score_scale))

        # 综合评分
        composite = (
            self._return_weight * return_score
            + self._drawdown_weight * drawdown_score
            + self._sharpe_weight * sharpe_score
        )

        return min(100.0, max(0.0, composite))