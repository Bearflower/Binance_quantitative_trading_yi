"""Typed configuration and contracts for the isolated grid executor."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional

from strategies.grid.grid_calculator import (CapitalFeasibility, DynamicGridParams,
                                             GridMode)


@dataclass(frozen=True)
class ExecutionSettings:
    mode: str = "off"
    symbol: str = "ETHUSDT"
    use_unified_account: bool = True
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    max_margin_usdt: Optional[Decimal] = None
    max_notional_usdt: Optional[Decimal] = None
    max_daily_loss_usdt: Optional[Decimal] = None
    max_drawdown_usdt: Optional[Decimal] = None
    max_open_orders: Optional[int] = None
    expected_leverage: Optional[int] = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ExecutionSettings":
        mode = env.get("GRID_EXECUTION_MODE", "off").strip().lower()
        if mode not in ("off", "live"):
            raise ValueError("GRID_EXECUTION_MODE must be off or live for PM")
        symbol = env.get("GRID_SYMBOL", "ETHUSDT").strip().upper()
        if symbol != "ETHUSDT":
            raise ValueError("GRID_SYMBOL must be ETHUSDT")
        if mode == "off":
            return cls()

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError("%s is required" % name)
            return value

        def positive_decimal(name: str) -> Decimal:
            raw = required(name)
            try:
                value = Decimal(raw)
            except InvalidOperation as exc:
                raise ValueError("%s must be positive" % name) from exc
            if not value.is_finite() or value <= 0:
                raise ValueError("%s must be positive" % name)
            return value

        key = required("GRID_SUBACCOUNT_API_KEY")
        secret = required("GRID_SUBACCOUNT_API_SECRET")
        margin = positive_decimal("GRID_MAX_MARGIN_USDT")
        notional = positive_decimal("GRID_MAX_NOTIONAL_USDT")
        daily_loss = positive_decimal("GRID_MAX_DAILY_LOSS_USDT")
        drawdown = positive_decimal("GRID_MAX_DRAWDOWN_USDT")
        raw_orders = required("GRID_MAX_OPEN_ORDERS")
        try:
            orders = int(raw_orders)
        except ValueError as exc:
            raise ValueError("GRID_MAX_OPEN_ORDERS must be positive") from exc
        if orders <= 0:
            raise ValueError("GRID_MAX_OPEN_ORDERS must be positive")
        raw_leverage = required("GRID_EXPECTED_LEVERAGE")
        try:
            leverage = int(raw_leverage)
        except ValueError as exc:
            raise ValueError("GRID_EXPECTED_LEVERAGE must be positive") from exc
        if leverage <= 0:
            raise ValueError("GRID_EXPECTED_LEVERAGE must be positive")
        return cls(mode, symbol, True, key, secret, margin, notional,
                   daily_loss, drawdown, orders, leverage)


def _decimal(value, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid %s" % name) from exc
    if not result.is_finite():
        raise ValueError("invalid %s" % name)
    return result


def decode_grid_params(payload: Mapping) -> DynamicGridParams:
    try:
        mode = GridMode[payload["grid_mode"]]
        count = payload["grid_count"]
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("invalid grid_count")
        return DynamicGridParams(
            lower_boundary=_decimal(payload["lower_boundary"], "lower_boundary"),
            upper_boundary=_decimal(payload["upper_boundary"], "upper_boundary"),
            grid_count=count, grid_mode=mode,
            stop_loss_low=_decimal(payload["stop_loss_low"], "stop_loss_low"),
            stop_loss_high=_decimal(payload["stop_loss_high"], "stop_loss_high"),
            stop_move_up_price=(_decimal(payload["stop_move_up_price"], "stop_move_up_price")
                                if payload.get("stop_move_up_price") is not None else None),
            stop_move_down_price=(_decimal(payload["stop_move_down_price"], "stop_move_down_price")
                                  if payload.get("stop_move_down_price") is not None else None),
            profit_rate=_decimal(payload["profit_rate"], "profit_rate"),
            grid_spacing=_decimal(payload["grid_spacing"], "grid_spacing"),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed grid params") from exc


def decode_feasibility(payload: Mapping) -> CapitalFeasibility:
    try:
        feasible = payload["feasible"]
        count = payload["grid_count"]
        if not isinstance(feasible, bool) or not isinstance(count, int):
            raise ValueError("malformed feasibility")
        return CapitalFeasibility(
            feasible=feasible, grid_count=count,
            qty_per_grid=_decimal(payload["qty_per_grid"], "qty_per_grid"),
            binding=str(payload["binding"]),
            required_margin=(_decimal(payload["required_margin"], "required_margin")
                             if payload.get("required_margin") is not None else None),
            required_leverage=payload.get("required_leverage"),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed feasibility") from exc
