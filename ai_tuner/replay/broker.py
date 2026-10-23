"""统一虚拟 Broker。

职责：市价/限价/止损/止盈、reduce-only、部分平仓、手续费、滑点、资金费、
价格与数量精度、最小名义价值、杠杆约束，以及「同根 K 线退出固定优先级」。

设计口径（与契约 §4 的差异已在复审包登记）：
- 成交价不含滑点，滑点作为独立成本行计入权益 —— 避免「价格已调整 + 再扣一次滑点」的双计；
- 市价入场在**下一根** K 线开盘价成交（确定性，无未来数据依赖）；
- 资金费 mark_price 用 entry_price 近似（与 backtest/btc_eth/replay/fees.py 口径一致）。

本模块为纯计算 + 内部状态，不读配置、不 import 策略代码、不产生随机成交价。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from shared.trade_logger import TradeLogger

from .models import (
    EXIT_PRIORITY,
    ClosedTrade,
    EquityPoint,
    Fill,
    Order,
    OrderIntent,
    Position,
    ZERO,
)

# ===========================================================================
# 纯函数（搬迁 backtest/btc_eth/replay/{matching,fees,invariants}.py 口径，
# 不 import backtest.*）
# ===========================================================================


def q_round(value: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    """按步长做通用舍入（Decimal 原生 quantize 不支持任意 step）。"""
    if step <= ZERO:
        raise ValueError("step 必须 > 0，收到 step={0}".format(step))
    scaled = (value / step).to_integral_value(rounding=rounding)
    return scaled * step


def round_price(price: Decimal, tick_size: Decimal) -> Decimal:
    """价格按 tickSize 做 ROUND_HALF_UP（对齐 Binance 舍入习惯）。"""
    return q_round(price, tick_size, rounding=ROUND_HALF_UP)


def round_quantity(quantity: Decimal, step_size: Decimal) -> Decimal:
    """数量按 stepSize 向下取整（避免因舍入过大触发 minNotional 检查失败）。"""
    return q_round(quantity, step_size, rounding=ROUND_DOWN)


def clamp_notional(quantity: Decimal, price: Decimal, min_notional: Decimal) -> Decimal:
    """名义价值低于 minNotional 时，返回使其恰好达标的最小数量（向上取整）。"""
    if min_notional <= ZERO:
        return quantity
    notional = quantity * price
    if notional >= min_notional:
        return quantity
    if price <= ZERO:
        return quantity
    return (min_notional / price).to_integral_value(rounding=ROUND_UP)


def price_crosses_limit(
    limit_price: Decimal,
    kline_high: Decimal,
    kline_low: Decimal,
    direction: str,
) -> bool:
    """入场限价穿越判定：LONG 看 low <= limit，SHORT 看 high >= limit。"""
    if direction == "LONG":
        return kline_low <= limit_price
    if direction == "SHORT":
        return kline_high >= limit_price
    raise ValueError("不支持的 direction: {0}".format(direction))


def market_fill_price(
    reference_price: Decimal,
    slippage_bps: Decimal,
    direction: str,
) -> Decimal:
    """市价成交基准价（slippage_bps=0 时即原价）。

    保留该函数是为了与 backtest 口径同源：滑点若并入价格，则不应再单独计提。
    本 Broker 采用「滑点独立计提」，故调用时传 0，禁止在此处重复扣减。
    """
    bps = slippage_bps / Decimal("10000")
    if direction == "LONG":
        return reference_price * (Decimal("1") + bps)
    if direction == "SHORT":
        return reference_price * (Decimal("1") - bps)
    raise ValueError("不支持的 direction: {0}".format(direction))


def taker_fee_notional(notional: Decimal, bps: Decimal) -> Decimal:
    """单边 taker 手续费，返回负值（支出）。"""
    return -(notional * bps / Decimal("10000"))


def slippage_cost(notional: Decimal, bps: Decimal) -> Decimal:
    """单边滑点成本，返回负值（支出，与方向无关）。"""
    return -(notional * bps / Decimal("10000"))


def calculate_pnl(
    direction: str,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    """统一盈亏计算——委托 shared.trade_logger.TradeLogger，禁止重复实现。"""
    return TradeLogger.calculate_pnl(direction, entry_price, exit_price, quantity)


def sum_funding_fee(
    quantity: Decimal,
    entry_price: Decimal,
    direction: str,
    funding_rates: Tuple[Decimal, ...],
) -> Decimal:
    """持仓跨越多个结算点的资金费合计（正=收取，负=支付）。"""
    total = ZERO
    notional = quantity * entry_price
    for rate in funding_rates:
        if direction == "LONG":
            total += -notional * rate
        elif direction == "SHORT":
            total += notional * rate
        else:
            raise ValueError("不支持的 direction: {0}".format(direction))
    return total


def check_quantity_in_range(quantity: Decimal, step_size: Decimal) -> Optional[str]:
    """撮合后数量必须是 stepSize 的整数倍（允许 1e-6 容差）。"""
    if step_size <= ZERO:
        return None
    ratio = quantity / step_size
    remainder = ratio - ratio.to_integral_value()
    if abs(remainder) > Decimal("1e-6"):
        return "quantity={0} 不是 stepSize={1} 的整数倍".format(quantity, step_size)
    return None


def check_notional_above_min(
    quantity: Decimal, price: Decimal, min_notional: Decimal
) -> Optional[str]:
    """撮合成交名义价值不得低于 minNotional（安全兜底）。"""
    if min_notional <= ZERO:
        return None
    if quantity * price < min_notional - Decimal("1e-8"):
        return "notional={0} 低于 minNotional={1}".format(quantity * price, min_notional)
    return None


# ===========================================================================
# Broker
# ===========================================================================


@dataclass(frozen=True)
class SymbolFilters:
    """交易所精度与最小名义价值。"""

    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class PortfolioView:
    """交给适配器只读的持仓视图（适配器不得持有 Broker）。"""

    positions: Tuple[Position, ...]
    last_prices: Dict[str, Decimal]
    realized_equity: Decimal


class VirtualBroker:
    """统一虚拟 Broker（单策略实例，状态全部内部冻结可重放）。"""

    def __init__(
        self,
        filters: Dict[str, SymbolFilters],
        fee_bps: Decimal,
        slippage_bps: Decimal,
        stop_offset_pct: Decimal,
        tp_offset_pct: Decimal,
        initial_equity: Decimal,
        max_leverage: Decimal,
        tp1_close_ratio: Decimal,
    ) -> None:
        self.filters = dict(filters)
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.stop_offset_pct = stop_offset_pct
        self.tp_offset_pct = tp_offset_pct
        self.initial_equity = initial_equity
        self.max_leverage = max_leverage
        self.tp1_close_ratio = tp1_close_ratio

        self.realized_equity = initial_equity
        self.positions: Dict[str, Position] = {}
        self.last_prices: Dict[str, Decimal] = {}
        self._pending_entries: Dict[str, List[Order]] = {}
        self._working_exits: Dict[str, List[Order]] = {}

        self.closed_trades: List[ClosedTrade] = []
        self.fills: List[Fill] = []
        self.audit: List[Dict[str, Any]] = []
        self.invariant_violations: List[str] = []

        self._entry_fee: Dict[str, Decimal] = {}
        self._entry_slip: Dict[str, Decimal] = {}
        self._funding_acc: Dict[str, Decimal] = {}
        self._mae: Dict[str, Decimal] = {}
        self._mfe: Dict[str, Decimal] = {}
        self._order_seq = 0
        self._trade_seq = 0

    # ------------------------------------------------------------------
    # 对外：提交意图 / 推进 K 线 / 资金费 / 盯市
    # ------------------------------------------------------------------
    def submit(self, intents: Tuple[OrderIntent, ...], bar: Any, ts: datetime) -> None:
        """接收适配器意图。

        - 非 reduce-only 的 MARKET 意图排队到下一根 K 线开盘成交；
        - reduce-only 的 MARKET 意图按 ``reference_price``（缺省为当根收盘）立即成交；
        - STOP / TAKE_PROFIT / LIMIT 作为工作单，随 K 线推进判定。
        """
        for intent in intents:
            order = self._new_order(intent, ts)
            if intent.reduce_only:
                if intent.order_type == "MARKET":
                    reference = intent.reference_price if intent.reference_price is not None else bar.close
                    self._execute_exit(order, reference, ts)
                else:
                    self._register_working_exit(order)
            else:
                if intent.order_type == "MARKET":
                    self._pending_entries.setdefault(intent.symbol, []).append(order)
                else:
                    self._pending_entries.setdefault(intent.symbol, []).append(order)

    def on_bar(self, symbol: str, bar: Any, ts: datetime) -> None:
        """推进一根 K 线：先按开盘价执行待成交入场，再按固定优先级处理退出。"""
        self.last_prices[symbol] = bar.close
        self._execute_pending_entries(symbol, bar, ts)
        self._evaluate_exits(symbol, bar, ts)
        self._update_mae_mfe(symbol, bar)

    def settle_funding(self, symbol: str, rate: Decimal, ts: datetime) -> None:
        """计提一次资金费结算（mark_price 用 entry_price 近似）。"""
        position = self.positions.get(symbol)
        if position is None or not position.is_open():
            return
        contribution = sum_funding_fee(
            position.current_quantity, position.entry_price, position.direction, (rate,)
        )
        self._funding_acc[position.position_id] = (
            self._funding_acc.get(position.position_id, ZERO) + contribution
        )
        self.realized_equity += contribution
        self._event(
            ts,
            "funding_settlement",
            {
                "symbol": symbol,
                "position_id": position.position_id,
                "funding_rate": str(rate),
                "contribution": str(contribution),
            },
        )

    def mark(self, ts: datetime) -> EquityPoint:
        """生成盯市权益点。"""
        unrealized = ZERO
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            if not position.is_open():
                continue
            last = self.last_prices.get(symbol, position.entry_price)
            unrealized += calculate_pnl(
                position.direction, position.entry_price, last, position.current_quantity
            )
        point = EquityPoint(
            ts=ts,
            realized_equity=self.realized_equity,
            marked_equity=self.realized_equity + unrealized,
            has_open_position=any(p.is_open() for p in self.positions.values()),
        )
        return point

    def view(self) -> PortfolioView:
        """当前持仓只读视图（供适配器计算退出）。"""
        return PortfolioView(
            positions=tuple(self.positions[symbol] for symbol in sorted(self.positions)),
            last_prices=dict(self.last_prices),
            realized_equity=self.realized_equity,
        )

    def final_position(self) -> Optional[Position]:
        for symbol in sorted(self.positions):
            if self.positions[symbol].is_open():
                return self.positions[symbol]
        return None

    def set_protective_prices(
        self,
        symbol: str,
        stop_loss_price: Optional[Decimal] = None,
        tp1_price: Optional[Decimal] = None,
        tp2_price: Optional[Decimal] = None,
    ) -> None:
        """把适配器算出的止损/止盈价格写入持仓（由 core/exits 提供，Broker 不重算）。"""
        position = self.positions.get(symbol)
        if position is None:
            return
        self.positions[symbol] = Position(
            position_id=position.position_id,
            symbol=position.symbol,
            direction=position.direction,
            entry_price=position.entry_price,
            entry_quantity=position.entry_quantity,
            current_quantity=position.current_quantity,
            atr=position.atr,
            grade=position.grade,
            entry_time=position.entry_time,
            stop_loss_price=stop_loss_price if stop_loss_price is not None else position.stop_loss_price,
            tp1_price=tp1_price if tp1_price is not None else position.tp1_price,
            tp2_price=tp2_price if tp2_price is not None else position.tp2_price,
            trailing_stop_price=position.trailing_stop_price,
            trailing_activated=position.trailing_activated,
            tp1_hit=position.tp1_hit,
            tp2_hit=position.tp2_hit,
        )

    # ------------------------------------------------------------------
    # 内部：订单生命周期
    # ------------------------------------------------------------------
    def _new_order(self, intent: OrderIntent, ts: datetime) -> Order:
        self._order_seq += 1
        order_id = "{0}.{1:06d}".format(intent.strategy_id, self._order_seq)
        return Order(order_id=order_id, intent=intent, status="OPEN", created_at=ts)

    def _register_working_exit(self, order: Order) -> None:
        """同一 (symbol, reason) 的新工作单替换旧单（移动止损更新语义）。"""
        symbol = order.intent.symbol
        existing = self._working_exits.setdefault(symbol, [])
        kept = [o for o in existing if o.intent.reason != order.intent.reason]
        kept.append(order)
        self._working_exits[symbol] = kept

    def _execute_pending_entries(self, symbol: str, bar: Any, ts: datetime) -> None:
        orders = self._pending_entries.pop(symbol, [])
        for order in sorted(orders, key=lambda o: o.intent.seq):
            intent = order.intent
            if intent.order_type == "MARKET":
                self._open_market_position(order, bar.open, ts)
            elif intent.order_type in ("LIMIT", "STOP"):
                trigger = intent.limit_price if intent.order_type == "LIMIT" else intent.trigger_price
                if trigger is not None and price_crosses_limit(
                    trigger, bar.high, bar.low, intent.side
                ):
                    self._open_market_position(order, trigger, ts)
                else:
                    self._pending_entries.setdefault(symbol, []).append(order)
            else:
                self._reject(order, ts, "ENTRY_ORDER_TYPE_UNSUPPORTED")

    def _open_market_position(self, order: Order, reference_price: Decimal, ts: datetime) -> None:
        intent = order.intent
        symbol = intent.symbol
        if symbol in self.positions and self.positions[symbol].is_open():
            self._reject(order, ts, "POSITION_EXISTS")
            return

        symbol_filters = self.filters.get(symbol)
        if symbol_filters is None:
            self._reject(order, ts, "SYMBOL_FILTERS_MISSING")
            return

        fill_price = round_price(reference_price, symbol_filters.tick_size)
        quantity = round_quantity(
            clamp_notional(intent.quantity, fill_price, symbol_filters.min_notional),
            symbol_filters.step_size,
        )
        if quantity <= ZERO:
            self._reject(order, ts, "QUANTITY_BELOW_STEP")
            return

        notional = quantity * fill_price
        if self.max_leverage > ZERO and notional > self.realized_equity * self.max_leverage:
            self._reject(order, ts, "LEVERAGE_LIMIT")
            return

        fee = taker_fee_notional(notional, self.fee_bps)
        slip = slippage_cost(notional, self.slippage_bps)
        self.realized_equity += fee + slip

        position = Position(
            position_id="{0}.{1:06d}".format(intent.strategy_id, self._order_seq),
            symbol=symbol,
            direction=intent.side,
            entry_price=fill_price,
            entry_quantity=quantity,
            current_quantity=quantity,
            atr=ZERO,
            grade=intent.grade or "",
            entry_time=ts,
        )
        self.positions[symbol] = position
        self._entry_fee[position.position_id] = fee
        self._entry_slip[position.position_id] = slip
        self._funding_acc.setdefault(position.position_id, ZERO)
        self._mae[position.position_id] = ZERO
        self._mfe[position.position_id] = ZERO
        self.fills.append(
            Fill(
                order_id=order.order_id,
                fill_price=fill_price,
                quantity=quantity,
                fee=fee,
                slippage_cost=slip,
                ts=ts,
            )
        )
        self._check_invariants(quantity, fill_price, symbol_filters, "open")
        self._event(
            ts,
            "fill",
            {
                "order_id": order.order_id,
                "symbol": symbol,
                "side": "OPEN_" + intent.side,
                "price": str(fill_price),
                "quantity": str(quantity),
                "fee": str(fee),
                "slippage": str(slip),
                "reason": intent.reason,
            },
        )

    def _evaluate_exits(self, symbol: str, bar: Any, ts: datetime) -> None:
        position = self.positions.get(symbol)
        if position is None or not position.is_open():
            return
        orders = self._working_exits.get(symbol, [])
        if not orders:
            return
        ordered = sorted(
            orders,
            key=lambda o: (
                EXIT_PRIORITY.get(o.intent.reason, 99),
                o.intent.seq,
            ),
        )
        for order in ordered:
            current = self.positions.get(symbol)
            if current is None or not current.is_open():
                return
            trigger = order.intent.trigger_price
            if trigger is None:
                continue
            if not self._trigger_crossed(order.intent, trigger, bar):
                continue
            if order.intent.reason in ("TP1", "TP2") and self._tp_already_hit(current, order.intent.reason):
                continue
            fill_price = self._exit_fill_price(order.intent, trigger)
            self._close_position(order, fill_price, ts)

    def _trigger_crossed(self, intent: OrderIntent, trigger: Decimal, bar: Any) -> bool:
        """保守穿越判定：止损看不利方向极值，止盈看有利方向极值。"""
        if intent.reason == "STOP_LOSS":
            if intent.side == "LONG":
                return bar.low <= trigger
            return bar.high >= trigger
        if intent.side == "LONG":
            return bar.high >= trigger
        return bar.low <= trigger

    @staticmethod
    def _tp_already_hit(position: Position, reason: str) -> bool:
        if reason == "TP1":
            return position.tp1_hit
        if reason == "TP2":
            return position.tp2_hit
        return False

    def _exit_fill_price(self, intent: OrderIntent, trigger: Decimal) -> Decimal:
        """止损向不利方向偏移、止盈向有利方向偏移（保证触发后必成交）。"""
        if intent.reason == "STOP_LOSS":
            if intent.side == "LONG":
                return trigger * (Decimal("1") - self.stop_offset_pct)
            return trigger * (Decimal("1") + self.stop_offset_pct)
        if intent.side == "LONG":
            return trigger * (Decimal("1") + self.tp_offset_pct)
        return trigger * (Decimal("1") - self.tp_offset_pct)

    def _execute_exit(self, order: Order, reference_price: Decimal, ts: datetime) -> None:
        """reduce-only MARKET 意图（时间退出等）按参考价立即成交。"""
        symbol = order.intent.symbol
        position = self.positions.get(symbol)
        if position is None or not position.is_open():
            self._reject(order, ts, "NO_POSITION_TO_REDUCE")
            return
        self._close_position(order, reference_price, ts)

    def _close_position(self, order: Order, reference_price: Decimal, ts: datetime) -> None:
        intent = order.intent
        symbol = intent.symbol
        position = self.positions[symbol]
        symbol_filters = self.filters.get(symbol)
        if symbol_filters is None:
            self._reject(order, ts, "SYMBOL_FILTERS_MISSING")
            return

        exit_price = round_price(reference_price, symbol_filters.tick_size)
        ratio = intent.close_ratio
        if intent.reason == "TP1" and intent.close_ratio == Decimal("1"):
            ratio = self.tp1_close_ratio
        close_qty = round_quantity(position.current_quantity * ratio, symbol_filters.step_size)
        if close_qty <= ZERO:
            self._event(
                ts,
                "reject",
                {
                    "order_id": order.order_id,
                    "symbol": symbol,
                    "reason": "REDUCE_TO_ZERO",
                    "requested_ratio": str(ratio),
                },
            )
            return
        if close_qty > position.current_quantity:
            close_qty = position.current_quantity

        notional = close_qty * exit_price
        fee = taker_fee_notional(notional, self.fee_bps)
        slip = slippage_cost(notional, self.slippage_bps)
        realized_pnl = calculate_pnl(
            position.direction, position.entry_price, exit_price, close_qty
        )

        share = close_qty / position.current_quantity
        entry_fee_share = self._entry_fee.get(position.position_id, ZERO) * share
        entry_slip_share = self._entry_slip.get(position.position_id, ZERO) * share
        funding_total = self._funding_acc.get(position.position_id, ZERO)
        funding_share = funding_total * share

        self._entry_fee[position.position_id] = (
            self._entry_fee.get(position.position_id, ZERO) - entry_fee_share
        )
        self._entry_slip[position.position_id] = (
            self._entry_slip.get(position.position_id, ZERO) - entry_slip_share
        )
        self._funding_acc[position.position_id] = funding_total - funding_share

        fee_paid = entry_fee_share + fee
        slippage_paid = entry_slip_share + slip
        net_pnl = realized_pnl + fee_paid + slippage_paid + funding_share

        self.realized_equity += realized_pnl + fee + slip

        remaining = position.current_quantity - close_qty
        self.positions[symbol] = Position(
            position_id=position.position_id,
            symbol=position.symbol,
            direction=position.direction,
            entry_price=position.entry_price,
            entry_quantity=position.entry_quantity,
            current_quantity=remaining,
            atr=position.atr,
            grade=position.grade,
            entry_time=position.entry_time,
            stop_loss_price=position.stop_loss_price,
            tp1_price=position.tp1_price,
            tp2_price=position.tp2_price,
            trailing_stop_price=position.trailing_stop_price,
            trailing_activated=position.trailing_activated,
            tp1_hit=position.tp1_hit or intent.reason == "TP1",
            tp2_hit=position.tp2_hit or intent.reason == "TP2",
        )

        self._trade_seq += 1
        self.closed_trades.append(
            ClosedTrade(
                trade_id="{0}.T{1:06d}".format(intent.strategy_id, self._trade_seq),
                position_id=position.position_id,
                symbol=symbol,
                direction=position.direction,
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=close_qty,
                realized_pnl=realized_pnl,
                fee_paid=fee_paid,
                slippage_cost=slippage_paid,
                funding_fee=funding_share,
                net_pnl=net_pnl,
                exit_reason=intent.reason,
                entry_time=position.entry_time,
                exit_time=ts,
                mae=self._mae.get(position.position_id, ZERO),
                mfe=self._mfe.get(position.position_id, ZERO),
            )
        )
        self.fills.append(
            Fill(
                order_id=order.order_id,
                fill_price=exit_price,
                quantity=close_qty,
                fee=fee,
                slippage_cost=slip,
                ts=ts,
            )
        )
        self._check_invariants(close_qty, exit_price, symbol_filters, "close")
        self._event(
            ts,
            "fill",
            {
                "order_id": order.order_id,
                "symbol": symbol,
                "side": "CLOSE_" + position.direction,
                "price": str(exit_price),
                "quantity": str(close_qty),
                "fee": str(fee),
                "slippage": str(slip),
                "reason": intent.reason,
                "net_pnl": str(net_pnl),
            },
        )
        if remaining <= ZERO:
            self._working_exits.pop(symbol, None)

    def _reject(self, order: Order, ts: datetime, reason: str) -> None:
        self._event(
            ts,
            "reject",
            {"order_id": order.order_id, "symbol": order.intent.symbol, "reason": reason},
        )

    def _check_invariants(
        self,
        quantity: Decimal,
        price: Decimal,
        symbol_filters: SymbolFilters,
        label: str,
    ) -> None:
        for message in (
            check_quantity_in_range(quantity, symbol_filters.step_size),
            check_notional_above_min(quantity, price, symbol_filters.min_notional),
        ):
            if message:
                self.invariant_violations.append("[{0}] {1}".format(label, message))
                self.audit.append(
                    {
                        "ts": None,
                        "type": "invariant_violation",
                        "payload": {"label": label, "message": message},
                    }
                )

    def _update_mae_mfe(self, symbol: str, bar: Any) -> None:
        position = self.positions.get(symbol)
        if position is None or not position.is_open():
            return
        if position.direction == "LONG":
            adverse = (position.entry_price - bar.low) * position.current_quantity
            favorable = (bar.high - position.entry_price) * position.current_quantity
        else:
            adverse = (bar.high - position.entry_price) * position.current_quantity
            favorable = (position.entry_price - bar.low) * position.current_quantity
        position_id = position.position_id
        self._mae[position_id] = max(self._mae.get(position_id, ZERO), adverse)
        self._mfe[position_id] = max(self._mfe.get(position_id, ZERO), favorable)

    def _event(self, ts: Optional[datetime], event_type: str, payload: Dict[str, Any]) -> None:
        self.audit.append({"ts": ts, "type": event_type, "payload": payload})
