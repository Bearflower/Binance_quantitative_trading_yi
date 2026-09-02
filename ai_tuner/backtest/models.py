"""
回测引擎数据模型

定义回测引擎使用的所有数据类，独立于引擎逻辑，避免循环导入。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


# ============================================================
# 自定义异常
# ============================================================

class BacktestTimeoutError(Exception):
    """回测超时异常"""
    pass


class BacktestDataError(Exception):
    """回测数据不足异常"""
    pass


# ============================================================
# 网格参数
# ============================================================

@dataclass
class GridParams:
    """回测用的网格参数（从 strategies/grid/config.yaml 提取）"""
    base_grid_count: int = 6                # grid.base_grid_count
    grid_spacing_atr_multiplier: float = 2.5  # grid.grid_spacing_atr_multiplier
    stop_loss_buffer: int = 2               # grid.stop_loss_buffer
    leverage: int = 10                      # trading.leverage
    margin: float = 500.0                   # trading.margin
    single_position_margin: float = 100.0   # trading.single_position_margin
    stop_loss_percent: float = 0.10         # risk.stop_loss_percent
    hard_stop_loss: float = -0.15           # risk.hard_stop_loss
    atr: float = 0.0                        # 当前ATR值（外部计算后传入）
    current_price: float = 0.0              # 当前价格（外部传入）


# ============================================================
# 成交记录
# ============================================================

@dataclass
class FillRecord:
    """单笔成交记录"""
    kline_index: int = 0                    # 触发成交的K线序号
    open_time: Optional[datetime] = None    # K线开盘时间
    direction: str = ""                     # "buy" 或 "sell"
    price: float = 0.0                      # 成交价格
    quantity: float = 0.0                   # 成交数量（币本位）
    fee: float = 0.0                        # 手续费（USDT）
    fee_type: str = "maker"                 # "maker" 或 "taker"
    notional: float = 0.0                   # 名义价值（USDT）
    regime: str = ""                        # 成交时的市况（上涨/下跌/横盘）


# ============================================================
# 回测状态
# ============================================================

@dataclass
class BacktestState:
    """回测过程中的持仓状态"""
    cash: float = 0.0                       # 可用资金（USDT）
    position: float = 0.0                   # 当前持仓（币本位，正=多，负=空）
    entry_price: float = 0.0                # 开仓均价
    last_price: float = 0.0                 # 最新价格
    equity_curve: List[float] = field(default_factory=list)  # 权益曲线
    fills: List[FillRecord] = field(default_factory=list)    # 所有成交记录
    buy_orders: Dict[float, float] = field(default_factory=dict)   # 买单挂单
    sell_orders: Dict[float, float] = field(default_factory=dict)  # 卖单挂单
    total_fee: float = 0.0                  # 累计手续费
    peak_equity: float = 0.0                # 权益峰值