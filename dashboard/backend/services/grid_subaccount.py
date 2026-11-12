"""Validate and expose the isolated grid PM snapshot without account credentials."""

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import time

FIELDS = (
    "version", "account_scope", "symbol", "as_of_ms", "engine_state",
    "risk_status", "stream_ready", "active_epoch_id", "phase_epoch_id",
    "fill_count", "order_count", "open_order_count", "net_qty", "entry_price",
    "mark_price", "unrealized_pnl_usdt", "realized_net_usdt", "funding_usdt",
    "stage_pnl_usdt", "cumulative_pnl_usdt",
    "last_trade",
)
NUMERIC = (
    "net_qty", "entry_price", "mark_price", "unrealized_pnl_usdt",
    "realized_net_usdt", "funding_usdt", "stage_pnl_usdt", "cumulative_pnl_usdt",
)


def read_grid_snapshot(path="/app/grid-dashboard/snapshot.json", now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        payload = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"status": "unavailable", "reason": "snapshot_missing"}
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "snapshot_invalid"}
    try:
        if (payload.get("version") != 1 or payload.get("account_scope") != "grid_subaccount_pm"
                or payload.get("symbol") not in (None, "ETHUSDT")
                or not isinstance(payload["as_of_ms"], int)):
            raise ValueError("invalid scope")
        age = now_ms - payload["as_of_ms"]
        if not 0 <= age <= 120_000:
            return {"status": "unavailable", "reason": "snapshot_stale"}
        if any(not Decimal(str(payload[key])).is_finite() for key in NUMERIC):
            raise ValueError("invalid number")
        if any(not isinstance(payload[key], int) or payload[key] < 0
               for key in ("fill_count", "order_count", "open_order_count")):
            raise ValueError("invalid count")
        if not isinstance(payload["engine_state"], str) or not isinstance(payload["risk_status"], str):
            raise ValueError("invalid state")
        trade = payload.get("last_trade")
        if trade is not None:
            if (not isinstance(trade, dict) or trade.get("side") not in ("BUY", "SELL")
                    or not isinstance(trade.get("trade_id"), str)
                    or not isinstance(trade.get("time_ms"), int)):
                raise ValueError("invalid last trade")
            for key in ("quantity", "price"):
                if not Decimal(str(trade[key])).is_finite():
                    raise ValueError("invalid last trade")
        result = {"status": "ok", **{key: payload[key] for key in FIELDS if key in payload}}
        if trade is not None:
            result["last_trade"] = {key: trade[key] for key in
                                    ("trade_id", "side", "quantity", "price", "time_ms")}
        return result
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return {"status": "unavailable", "reason": "snapshot_invalid"}
