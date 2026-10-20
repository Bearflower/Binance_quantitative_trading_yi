"""
per-profile 有状态门槛状态机。

每个 profile（xau_original / xau_aggressive）都有独立的 VirtualState 实例，
维护虚拟权益、可用资金、当前持仓、每日交易计数、单品种冷却、连续亏损暂停等。
被门槛拒绝的 candidate 仍返回 rejection_reason 字典，不丢。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple


@dataclass
class VirtualState:
    """单 profile 独立的虚拟资金状态。"""

    # --- pilot 约束（从冻结 profile 配置的 pilot 段读取） ---
    max_single_position_margin_usdt: Decimal = Decimal("30")
    max_concurrent_positions: int = 1
    max_leverage: int = 3

    # --- 运行态 ---
    initial_equity: Decimal = Decimal("300")            # 初始虚拟权益（spec §4.1）
    equity: Decimal = Decimal("300")                      # 实时虚拟权益（含已实现盈亏 + 未实现盈亏）
    realized_pnl: Decimal = Decimal("0")                  # 累计已实现盈亏
    fee_paid: Decimal = Decimal("0")                      # 累计已付手续费
    funding_fee: Decimal = Decimal("0")                    # 累计资金费
    slippage_cost: Decimal = Decimal("0")                 # 累计滑点成本

    # 当前持仓（最多 1，符合 pilot 约束）：None 表示空仓
    # 为避免循环引用，持仓结构保持最小字段
    open_position: Optional[Dict[str, Any]] = None

    # 每日交易计数，key 为 yyyy-mm-dd 字符串
    daily_trade_count: Dict[str, int] = field(default_factory=dict)
    max_daily_trades: int = 1  # 每 profile 每日最大开仓次数（从 symbol_config 读取）

    # 单品种冷却（按 symbol 记录上次开仓时间）
    symbol_cooldown_until: Dict[str, datetime] = field(default_factory=dict)
    symbol_cooldown_hours: int = 72                         # 冷却期小时数（从 risk.frequency_control 读取）

    # 连续亏损暂停
    consecutive_losses: int = 0
    consecutive_loss_pause_threshold: int = 2
    consecutive_loss_pause_until: Optional[datetime] = None
    pause_duration_hours: int = 24

    # 历史权益曲线（用于最大回撤 / 反事实）
    # 阻断 #4：三元组 (datetime, marked_equity, has_open_position)
    # 第三字段 = 当刻是否有持仓，让 counterfactual 反事实停牌标记
    # 能同时给出「当时有没有仓位」
    equity_history: List[Tuple[datetime, Decimal, bool]] = field(default_factory=list)

    def snapshot_equity(self, at: datetime) -> None:
        """把当前 equity（已实现）记录进 equity_history（第三字段用 has_open_position）。
        回放引擎在每次平仓 / 门槛拒绝 / 期终调用。
        阻断 #4：此方法保留给"无盯市"的旧快照时机；
        持仓期间由 snapshot_marked_equity 记录逐根盯市价。
        """
        # 避免连续时间戳完全相同的重复 snapshot（不影响最终结果，但节省）
        if self.equity_history and self.equity_history[-1][0] == at:
            return
        has_pos = self.open_position is not None
        self.equity_history.append((at, self.equity, has_pos))

    def snapshot_marked_equity(self, at: datetime, marked_equity: Decimal, has_open_position: bool) -> None:
        """阻断 #4：持仓期间逐根 1m K 线盯市快照。

        marked_equity = equity + unrealized（引擎在每根 K 线退出判定后、未平仓时传入）。
        第三字段 has_open_position 让反事实停牌标记能同时给出「当时有没有仓位」。
        空仓期间（unrealized=0，marked_equity==equity 恒定）引擎不再调此方法，避免数据量爆炸。
        """
        if self.equity_history and self.equity_history[-1][0] == at:
            return
        self.equity_history.append((at, marked_equity, has_open_position))

    # ---- 查询 ----

    def has_open_position(self) -> bool:
        return self.open_position is not None

    def available_margin(self) -> Decimal:
        """当前可用于新开仓的保证金 = equity - 保证金占用。
        简化：因 max_concurrent_positions=1，open_position 存在时 available = 0。
        """
        if self.has_open_position():
            return Decimal("0")
        return self.equity - self._margin_used()

    def _margin_used(self) -> Decimal:
        if self.open_position is None:
            return Decimal("0")
        # 简化：直接记录为 position["margin"]，由撮合时写入
        return Decimal(str(self.open_position.get("margin", "0")))

    def _date_str(self, at: datetime) -> str:
        return at.strftime("%Y-%m-%d")

    def _is_in_consecutive_loss_pause(self, at: datetime) -> bool:
        if self.consecutive_loss_pause_until is None:
            return False
        return at < self.consecutive_loss_pause_until

    # ---- 门槛检查（返回 (allowed: bool, reason_code: str)） ----

    def check_entry_eligibility(
        self,
        at: datetime,
        symbol: str,
        direction: str,      # 接口兼容：由 engine 在撮合前单独校验方向合法性，此处不使用
        required_margin: Decimal,
    ) -> Dict[str, Any]:
        """综合门槛检查；返回 {"allowed": bool, "rejection_reason": str}。"""
        reasons: List[str] = []

        # 1. 最大并发持仓
        if self.has_open_position():
            reasons.append("position_limit")

        # 2. 最大单笔保证金
        if required_margin > self.max_single_position_margin_usdt:
            reasons.append("exceeds_max_single_margin")

        # 3. 资金可用性
        if required_margin > self.available_margin():
            reasons.append("insufficient_margin")

        # 4. 每日交易计数
        day_key = self._date_str(at)
        today_count = self.daily_trade_count.get(day_key, 0)
        if today_count >= self.max_daily_trades:
            reasons.append("daily_trade_limit")

        # 5. 单品种冷却
        cooldown_until = self.symbol_cooldown_until.get(symbol)
        if cooldown_until is not None and at < cooldown_until:
            reasons.append("symbol_cooldown")

        # 6. 连续亏损暂停
        if self._is_in_consecutive_loss_pause(at):
            reasons.append("consecutive_loss_pause")

        # 7. 最大杠杆：required_margin 与 notional 的比；回放器在撮合时已知杠杆，
        # 这里用 required_margin >= notional / max_leverage 反推——直接比较 max_leverage 值即可。
        # （调用方在撮合前已按 pilot.max_leverage 调整 quantity，这里只记录诊断）

        return {
            "allowed": len(reasons) == 0,
            "rejection_reason": reasons[0] if reasons else "",
            "rejection_reasons": reasons,
        }

    # ---- 状态变更 ----

    def on_entry_approved(
        self,
        at: datetime,
        symbol: str,
        direction: str,
        quantity: Decimal,
        entry_price: Decimal,
        margin: Decimal,
        leverage: int,
        stop_loss_price: Optional[Decimal],
        grade: Optional[str],
        tp1: Optional[Decimal] = None,
        tp1_close_ratio: Optional[Decimal] = None,
        tp2: Optional[Decimal] = None,
        tp2_close_ratio: Optional[Decimal] = None,
        trailing: Optional[Dict[str, Any]] = None,
        time_stop: Optional[Dict[str, Any]] = None,
        # 阻断 #3 / #4 新增：trailing 初始化字段
        # （由引擎在撮合成功后传入，state 不自行读配置）
        highest_price: Optional[Decimal] = None,
        lowest_price: Optional[Decimal] = None,
        atr: Optional[Decimal] = None,
    ) -> None:
        """撮合成功、门槛通过后，写入 open_position。

        阻断 #3：新增 highest_price / lowest_price / trailing_activated
        / trailing_stop_price / tp1_hit / atr 字段。
        由引擎在撮合时从 fill_price 初始化 highest_price/lowest_price=fill_price，
        确保 running peak 起点正确。
        """
        # 阻断 #3：running peak 初始化为 fill_price（入场后第一根 K 线开始更新）
        init_highest = highest_price if highest_price is not None else entry_price
        init_lowest = lowest_price if lowest_price is not None else entry_price

        self.open_position = {
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            # 批次三 A：入场总数量（不随部分平仓变化），report 聚合时用此字段，
            # 而非各批次 exit 明细里的 quantity（那是该批次平仓量）
            "entry_quantity": quantity,
            "entry_price": entry_price,
            "margin": margin,
            "leverage": leverage,
            "opened_at": at,
            "stop_loss": stop_loss_price,
            "tp1": tp1,
            "tp1_close_ratio": tp1_close_ratio,
            "tp2": tp2,
            "tp2_close_ratio": tp2_close_ratio,
            "trailing": trailing,
            "time_stop": time_stop,
            "grade": grade,
            # 阻断 #3 新增：trailing 运行态字段
            "highest_price": init_highest,         # LONG 历史最高
            "lowest_price": init_lowest,           # SHORT 历史最低
            "trailing_activated": False,           # 激活后保持 True 不退出
            "trailing_stop_price": None,           # 当前 trailing 停止价（单向移动）
            "pending_profit_pct": None,            # 上次峰值浮盈 %（诊断用）
            "tp1_hit": False,                      # TP1 部分平仓后置 True
            "atr": atr,                            # ATR 值（trailing 硬止损兜底用）
            # Round4 修复：整笔仓位级累计净盈亏（含手续费/资金费/滑点），
            # 用于连续亏损判定——分批平仓场景下，单批 realized_pnl 只是一腿，
            # 连续亏损必须按整笔 position_net_pnl 判定。
            "position_net_pnl": Decimal("0"),
        }
        day_key = self._date_str(at)
        self.daily_trade_count[day_key] = self.daily_trade_count.get(day_key, 0) + 1

    def on_position_closed(
        self,
        at: datetime,
        realized_pnl: Decimal,
        fee: Decimal,
        funding: Decimal,
        slippage: Decimal,
        close_reason: str,
        position_net_pnl: Decimal,
    ) -> None:
        """平仓后更新权益、重置连续亏损计数器、写冷却期。

        Round4 修复：连续亏损判定改用整笔 ``position_net_pnl`` 而非单批 ``realized_pnl``。
        分批平仓场景下，``realized_pnl`` 只是最后一批的毛盈亏，
        ``position_net_pnl`` 才是整笔仓位累计的净盈亏（含所有腿的 fee/funding/slippage）。
        """
        # 权益 / 费用累计口径 **不变**：on_position_closed 只处理最后一批，
        # 部分腿已在 _execute_exit 部分平仓分支入账，严禁双重累计。
        self.realized_pnl += realized_pnl
        self.fee_paid += abs(fee) if fee < 0 else Decimal("0")
        self.funding_fee += funding  # 正=收入、负=支出，净额累计
        self.slippage_cost += abs(slippage) if slippage < 0 else Decimal("0")
        self.equity += realized_pnl + fee + funding + slippage  # fee/slippage 已是负值

        # 连续亏损计数（整笔仓位级净盈亏判定）
        if position_net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.consecutive_loss_pause_threshold:
                self.consecutive_loss_pause_until = at + timedelta(hours=self.pause_duration_hours)
        else:
            self.consecutive_losses = 0
            self.consecutive_loss_pause_until = None

        # 单品种冷却
        if self.open_position is not None:
            symbol = self.open_position["symbol"]
            self.symbol_cooldown_until[symbol] = at + timedelta(hours=self.symbol_cooldown_hours)

        self.open_position = None
        self.snapshot_equity(at)

    def reset_daily_counters(self, at: datetime) -> None:
        """每日开盘时调用，清理过期的 daily_trade_count。（可选，回放器可不主动清理。）"""
        today_key = self._date_str(at)
        stale_keys = [k for k in self.daily_trade_count.keys() if k < today_key]
        for k in stale_keys:
            del self.daily_trade_count[k]
