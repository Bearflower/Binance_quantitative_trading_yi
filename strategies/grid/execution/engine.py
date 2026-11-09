"""Safety-first reconciliation for the dedicated ETHUSDT account.

All writes are disabled until the journal and exchange agree. More execution
transitions are added after the reconciliation contract is tested.
"""

from decimal import Decimal


class GridEngine:
    def __init__(self, store, exchange):
        self.store = store
        self.exchange = exchange

    def _lock(self, reason: str) -> bool:
        self.store.set_engine_state("LOCKED", reason)
        return False

    async def reconcile(self) -> bool:
        """Compare account-wide ETHUSDT facts with owned, durable intents."""
        if self.store.engine_state()["state"] == "LOCKED":
            return False
        ordinary = await self.exchange.open_orders()
        algo = await self.exchange.open_stops()
        position_rows = await self.exchange.position()
        if len(position_rows) != 1 or position_rows[0].get("positionSide") != "BOTH":
            return self._lock("cannot confirm one-way ETHUSDT position")
        position = Decimal(str(position_rows[0]["positionAmt"]))

        owned = {row["client_order_id"]: row for row in self.store.active_orders()}
        open_by_id = {}
        for row in ordinary:
            if row.get("symbol") != "ETHUSDT":
                return self._lock("unexpected ordinary order symbol")
            client_id = row.get("clientOrderId")
            if not client_id or not client_id.startswith("eg-") or client_id not in owned:
                return self._lock("foreign or unknown ETHUSDT ordinary order")
            if client_id in open_by_id:
                return self._lock("duplicate exchange client order ID")
            open_by_id[client_id] = row

        protection_ids = {row["client_algo_id"] for row in self.store.active_protections()}
        for row in algo:
            if row.get("symbol") != "ETHUSDT":
                return self._lock("unexpected algo order symbol")
            client_id = row.get("clientAlgoId")
            if not client_id or client_id not in protection_ids:
                return self._lock("foreign or unknown ETHUSDT algo order")

        for client_id, intent in owned.items():
            row = open_by_id.get(client_id)
            if row is None:
                row = await self.exchange.query_order(client_id)
            if not row or row.get("code") is not None:
                return self._lock("order result unknown: %s" % client_id)
            if row.get("clientOrderId", client_id) != client_id:
                return self._lock("order client ID mismatch")
            status = row.get("status")
            if status not in ("NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED"):
                return self._lock("unknown exchange order status")
            self.store.record_order(client_id, str(row["orderId"]), status,
                                    Decimal(str(row.get("executedQty", "0"))))

        expected_position = sum((Decimal(row["quantity"]) *
                                 (1 if row["side"] == "BUY" else -1)
                                 for row in self.store.all_fills()), Decimal(0))
        if expected_position != position:
            return self._lock("position does not match journal fills")
        if position != 0 and not algo:
            return self._lock("position has no exchange stop protection")
        return True
