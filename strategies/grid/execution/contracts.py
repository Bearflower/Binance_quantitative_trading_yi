"""Typed configuration and contracts for the isolated grid executor."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional


@dataclass(frozen=True)
class ExecutionSettings:
    mode: str = "off"
    symbol: str = "ETHUSDT"
    use_unified_account: bool = False
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    max_margin_usdt: Optional[Decimal] = None
    max_notional_usdt: Optional[Decimal] = None
    max_daily_loss_usdt: Optional[Decimal] = None
    max_drawdown_usdt: Optional[Decimal] = None
    max_open_orders: Optional[int] = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ExecutionSettings":
        mode = env.get("GRID_EXECUTION_MODE", "off").strip().lower()
        if mode not in ("off", "testnet", "live"):
            raise ValueError("GRID_EXECUTION_MODE must be off, testnet or live")
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
        return cls(mode, symbol, False, key, secret, margin, notional,
                   daily_loss, drawdown, orders)
