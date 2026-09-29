"""
绩效指标纯函数模块

提供年化夏普比率（Sharpe Ratio）和最大回撤（Max Drawdown）的计算函数。
这些函数是纯函数，只依赖 Python 标准库（statistics / math），
不访问数据库、不依赖项目其他模块，便于单元测试和复用。

与 ai_tuner/backtest/metrics.py 的区别：
    - backtest 模块入参权益曲线、年化用 sqrt(kline_count)，口径不同；
    - 本模块入参是日收益率序列（sharpe）或净值序列（max_drawdown），
      年化系数固定为 365（币圈 7x24 交易），与金融业界常见口径一致。
"""
import math
import statistics
from typing import List, Optional

# 币圈 7×24 年化系数：一年 365 天（而非股市 252 个交易日）
ANNUALIZATION_PERIODS = 365

# 样本数不足此值时返回 None（至少 2 个数据点才能算收益率/回撤）
MIN_SAMPLE_COUNT = 2


def annualized_sharpe(daily_returns: List[float]) -> Optional[float]:
    """计算年化夏普比率

    计算公式：sharpe = mean(日收益率) / pstdev(日收益率) * sqrt(365)

    使用总体标准差 statistics.pstdev（除以 N 而非 N-1），
    与常见金融业界口径一致。

    Args:
        daily_returns: 日收益率序列（如 [0.012, -0.003, 0.008, ...]）
                       每个元素是当日收益率（正数盈利 / 负数亏损）

    Returns:
        float: 年化夏普比率；样本不足 2 个、或标准差为 0（全 0 收益率）时返回 None
    """
    # 样本数量检查
    if not daily_returns or len(daily_returns) < MIN_SAMPLE_COUNT:
        return None

    mean = statistics.mean(daily_returns)
    std = statistics.pstdev(daily_returns)

    # 标准差为 0 时无法计算夏普比率（全 0 收益率或所有收益率相同）
    if std == 0:
        return None

    return mean / std * math.sqrt(ANNUALIZATION_PERIODS)


def max_drawdown_ratio(net_values: List[float]) -> Optional[float]:
    """从净值序列计算最大回撤（比例）

    最大回撤 = max_{t} (peak - NV_t) / peak，其中 peak 是 NV_t 之前（含自身）的 running max。

    Args:
        net_values: 净值序列（累计权益 / 净值曲线，从 1.0 开始逐日复利）
                    例如 [1.0, 1.02, 0.98, 1.05, ...]

    Returns:
        float: 最大回撤比例（0~1 之间，如 0.25 表示最大回撤 25%）；
               样本不足 2 个时返回 None；
               单调递增序列的最大回撤为 0.0
    """
    if not net_values or len(net_values) < MIN_SAMPLE_COUNT:
        return None

    peak = net_values[0]
    max_dd = 0.0

    for value in net_values:
        if value > peak:
            peak = value
        # 净值不可能跌破 0（本金归零是极端情况，此处按常规处理）
        if peak > 0:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd

    return max_dd
