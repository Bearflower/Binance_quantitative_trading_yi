"""Recover a live position's entry time from complete exchange fill history."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Optional


def verified_entry_time(
    fills: Iterable[Mapping], position_amt: Decimal, *, page_limit: int = 1000
) -> Optional[datetime]:
    """Return the first fill after the last verified flat balance.

    A truncated page, missing flat boundary, or mismatch with positionRisk
    makes the answer unknown. This never guesses an entry time.
    """
    rows = list(fills)
    if not rows or len(rows) >= page_limit:
        return None
    try:
        rows.sort(key=lambda row: (int(row["time"]), int(row["id"])))
        current = Decimal("0")
        last_flat_index = None
        for index, row in enumerate(rows):
            qty = Decimal(str(row["qty"]))
            if not qty.is_finite() or qty <= 0:
                return None
            side = row["side"]
            if side not in ("BUY", "SELL"):
                return None
            current += qty if side == "BUY" else -qty
            if current == 0:
                last_flat_index = index
        actual = Decimal(str(position_amt))
        if actual == 0 or current != actual or last_flat_index is None:
            return None
        next_index = last_flat_index + 1
        if next_index >= len(rows):
            return None
        first = rows[next_index]
        if (first["side"] == "BUY") != (actual > 0):
            return None
        return datetime.fromtimestamp(int(first["time"]) / 1000, timezone.utc)
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return None


def verified_anchored_lot(
    fills: Iterable[Mapping], position_amt: Decimal, anchor_ms: int, *, page_limit: int = 1000
) -> bool:
    """Confirm an audited lot remains open from its exact first fill.

    The caller must query userTrades with start_time=anchor_ms. A later flat
    boundary or quantity mismatch means this is no longer the audited lot.
    """
    rows = list(fills)
    if not rows or len(rows) >= page_limit:
        return False
    try:
        rows.sort(key=lambda row: (int(row["time"]), int(row["id"])))
        if int(rows[0]["time"]) != anchor_ms:
            return False
        actual = Decimal(str(position_amt))
        if actual == 0 or not actual.is_finite():
            return False
        net = Decimal("0")
        for row in rows:
            qty = Decimal(str(row["qty"]))
            side = row["side"]
            if not qty.is_finite() or qty <= 0 or side not in ("BUY", "SELL"):
                return False
            net += qty if side == "BUY" else -qty
            if net == 0 or (net > 0) != (actual > 0):
                return False
        return net == actual
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return False


async def fetch_lot_fills(client, symbol: str, anchor_ms: int, now_ms: int = None):
    """Fetch a lot in complete six-day windows; return None on any truncated page."""
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if anchor_ms <= 0 or now_ms < anchor_ms:
        return None
    six_days_ms = 6 * 24 * 60 * 60 * 1000
    rows = []
    start = anchor_ms
    while start <= now_ms:
        end = min(start + six_days_ms - 1, now_ms)
        page = await client.get_user_trades(
            symbol, start_time=start, end_time=end, limit=1000
        )
        if not isinstance(page, list) or len(page) >= 1000:
            return None
        rows.extend(page)
        start = end + 1
    return rows
