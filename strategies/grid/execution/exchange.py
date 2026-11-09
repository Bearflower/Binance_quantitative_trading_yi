"""Isolated ordinary USD-M Binance adapter for the ETHUSDT order grid."""

import asyncio
from decimal import Decimal
from typing import Any, Dict

from shared.binance_api import BinanceClient
from .contracts import ExecutionSettings
from .planner import ExchangeFilters


class AccountModeError(RuntimeError):
    pass


class UnknownOrderResultError(RuntimeError):
    pass


class GridExchange:
    def __init__(self, client: BinanceClient):
        if client.use_unified_account:
            raise AccountModeError("grid requires ordinary USD-M client")
        self.client = client

    @classmethod
    def from_settings(cls, settings: ExecutionSettings) -> "GridExchange":
        if settings.mode == "off":
            raise ValueError("execution is off")
        return cls(BinanceClient(api_key=settings.api_key,
                                 api_secret=settings.api_secret,
                                 testnet=settings.mode == "testnet",
                                 use_unified_account=False))

    async def close(self):
        await self.client.close()

    async def _post_once(self, path: str, params: Dict[str, str], client_id: str,
                         algo: bool = False) -> Dict[str, Any]:
        try:
            return await self.client._request_once("POST", path, params, signed=True)
        except Exception as exc:
            # A lost POST response is ambiguous. Query only; never repeat the write.
            for _ in range(3):
                try:
                    found = (await self.query_stop(client_id) if algo
                             else await self.query_order(client_id))
                    if found and "code" not in found:
                        return found
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            raise UnknownOrderResultError("unresolved order result for %s" % client_id) from exc

    async def place_limit(self, side: str, qty: Decimal, price: Decimal,
                          client_id: str) -> Dict[str, Any]:
        if side not in ("BUY", "SELL") or qty <= 0 or price <= 0:
            raise ValueError("invalid grid limit order")
        params = {"symbol": "ETHUSDT", "side": side, "positionSide": "BOTH",
                  "type": "LIMIT", "timeInForce": "GTC", "quantity": str(qty),
                  "price": str(price), "newClientOrderId": client_id}
        return await self._post_once("/fapi/v1/order", params, client_id)

    async def close_position(self, side: str, qty: Decimal,
                             client_id: str) -> Dict[str, Any]:
        if side not in ("BUY", "SELL") or qty <= 0:
            raise ValueError("invalid close order")
        params = {"symbol": "ETHUSDT", "side": side, "positionSide": "BOTH",
                  "type": "MARKET", "reduceOnly": "true", "quantity": str(qty),
                  "newClientOrderId": client_id}
        return await self._post_once("/fapi/v1/order", params, client_id)

    async def place_stop(self, side: str, trigger_price: Decimal,
                         client_id: str) -> Dict[str, Any]:
        if side not in ("BUY", "SELL") or trigger_price <= 0:
            raise ValueError("invalid stop")
        params = {"algoType": "CONDITIONAL", "symbol": "ETHUSDT", "side": side,
                  "positionSide": "BOTH", "type": "STOP_MARKET",
                  "triggerPrice": str(trigger_price), "closePosition": "true",
                  "clientAlgoId": client_id, "workingType": "MARK_PRICE"}
        return await self._post_once("/fapi/v1/algoOrder", params, client_id,
                                     algo=True)

    async def query_order(self, client_id: str):
        return await self.client._request("GET", "/fapi/v1/order",
                                          {"symbol": "ETHUSDT",
                                           "origClientOrderId": client_id})

    async def cancel_order(self, client_id: str):
        return await self.client._request("DELETE", "/fapi/v1/order",
                                          {"symbol": "ETHUSDT",
                                           "origClientOrderId": client_id})

    async def open_orders(self):
        result = await self.client._request("GET", "/fapi/v1/openOrders",
                                            {"symbol": "ETHUSDT"})
        if not isinstance(result, list):
            raise ValueError("invalid open orders response")
        return result

    async def open_stops(self):
        result = await self.client._request("GET", "/fapi/v1/openAlgoOrders",
                                            {"symbol": "ETHUSDT"})
        if not isinstance(result, list):
            raise ValueError("invalid open algo orders response")
        return result

    async def query_stop(self, client_id: str):
        return await self.client._request("GET", "/fapi/v1/algoOrder",
                                          {"clientAlgoId": client_id})

    async def cancel_stop(self, client_id: str):
        return await self.client._request("DELETE", "/fapi/v1/algoOrder",
                                          {"clientAlgoId": client_id})

    async def position(self):
        result = await self.client._request("GET", "/fapi/v2/positionRisk",
                                            {"symbol": "ETHUSDT"})
        if not isinstance(result, list):
            raise ValueError("invalid position response")
        return result

    async def verify_account(self, leverage: int) -> ExchangeFilters:
        mode = await self.client._request("GET", "/fapi/v1/positionSide/dual")
        assets = await self.client._request("GET", "/fapi/v1/multiAssetsMargin")
        if mode.get("dualSidePosition") is not False or assets.get("multiAssetsMargin") is not False:
            raise AccountModeError("one-way single-asset mode required")
        positions = await self.position()
        matching = [p for p in positions if p.get("symbol") == "ETHUSDT"
                    and p.get("positionSide") == "BOTH"]
        if len(matching) != 1:
            raise AccountModeError("cannot confirm ETHUSDT BOTH position")
        position = matching[0]
        if position.get("marginType", "").lower() != "isolated":
            raise AccountModeError("isolated margin required")
        if int(position.get("leverage", 0)) != leverage:
            raise AccountModeError("leverage mismatch")
        info = await self.client._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        symbols = [item for item in info.get("symbols", []) if item.get("symbol") == "ETHUSDT"]
        if len(symbols) != 1 or symbols[0].get("status") != "TRADING":
            raise AccountModeError("ETHUSDT symbol unavailable")
        filters = {item["filterType"]: item for item in symbols[0].get("filters", [])}
        try:
            return ExchangeFilters(
                Decimal(filters["PRICE_FILTER"]["tickSize"]),
                Decimal(filters["LOT_SIZE"]["stepSize"]),
                Decimal(filters["LOT_SIZE"]["minQty"]),
                Decimal(filters["MIN_NOTIONAL"]["notional"]),
            )
        except (KeyError, ValueError) as exc:
            raise AccountModeError("missing ETHUSDT filters") from exc
