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
                  "type": "LIMIT", "timeInForce": "GTX", "quantity": str(qty),
                  "price": str(price), "newClientOrderId": client_id}
        return await self._post_once("/fapi/v1/order", params, client_id)

    async def close_position(self, side: str, qty: Decimal,
                             client_id: str) -> Dict[str, Any]:
        if side not in ("BUY", "SELL") or qty <= 0:
            raise ValueError("invalid close order")
        params = {"symbol": "ETHUSDT", "side": side, "positionSide": "BOTH",
                  "type": "MARKET", "reduceOnly": "true", "quantity": str(qty),
                  "newClientOrderId": client_id, "newOrderRespType": "RESULT"}
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

    async def ticker_price(self) -> Decimal:
        result = await self.client._request("GET", "/fapi/v2/ticker/price",
                                            {"symbol": "ETHUSDT"}, signed=False)
        if result.get("symbol") != "ETHUSDT":
            raise ValueError("wrong ticker symbol")
        price = Decimal(str(result["price"]))
        if not price.is_finite() or price <= 0:
            raise ValueError("invalid ticker price")
        return price

    async def income(self, start_ms: int, end_ms: int):
        """Read ordinary USD-M income directly; a full page is incomplete evidence.

        The shared client's non-unified helper returns [] without a request.
        A full 1000-row page cannot be safely advanced by timestamp because
        multiple facts can share a millisecond; fail closed instead.
        """
        if not 0 < start_ms <= end_ms:
            raise ValueError("invalid income window")
        result = await self.client._request("GET", "/fapi/v1/income",
                                            {"startTime": start_ms,
                                             "endTime": end_ms, "limit": 1000})
        if not isinstance(result, list) or len(result) >= 1000:
            raise ValueError("income page incomplete")
        return result

    async def order_trades(self, order_id: int):
        if order_id <= 0:
            raise ValueError("invalid exchange order ID")
        result = await self.client._request("GET", "/fapi/v1/userTrades",
                                            {"symbol": "ETHUSDT", "orderId": order_id,
                                             "limit": 1000})
        if not isinstance(result, list) or len(result) >= 1000:
            raise ValueError("order trade page incomplete")
        if any(not isinstance(row, dict) or row.get("orderId") != order_id
               for row in result):
            raise ValueError("trade response contains another order")
        return result

    async def start_user_stream(self) -> str:
        result = await self.client._request_once("POST", "/fapi/v1/listenKey",
                                                 signed=False)
        key = result.get("listenKey") if isinstance(result, dict) else None
        if not isinstance(key, str) or not key:
            raise ValueError("missing user stream listen key")
        return key

    async def keepalive_user_stream(self) -> str:
        result = await self.client._request_once("PUT", "/fapi/v1/listenKey",
                                                 signed=False)
        key = result.get("listenKey") if isinstance(result, dict) else None
        if not isinstance(key, str) or not key:
            raise ValueError("user stream renewal unconfirmed")
        return key

    async def account_info(self):
        result = await self.client._request("GET", "/fapi/v3/account")
        if not isinstance(result, dict) or result.get("canTrade") is not True:
            raise AccountModeError("futures account cannot trade")
        return result

    async def verify_account(self, leverage: int) -> ExchangeFilters:
        mode = await self.client._request("GET", "/fapi/v1/positionSide/dual")
        assets = await self.client._request("GET", "/fapi/v1/multiAssetsMargin")
        if mode.get("dualSidePosition") is not False or assets.get("multiAssetsMargin") is not False:
            raise AccountModeError("one-way single-asset mode required")
        configurations = await self.client._request(
            "GET", "/fapi/v1/symbolConfig", {"symbol": "ETHUSDT"})
        if not isinstance(configurations, list):
            raise AccountModeError("invalid symbol configuration response")
        matching = [p for p in configurations if p.get("symbol") == "ETHUSDT"]
        if len(matching) != 1:
            raise AccountModeError("cannot confirm ETHUSDT symbol configuration")
        config = matching[0]
        if config.get("marginType", "").lower() != "isolated":
            raise AccountModeError("isolated margin required")
        if int(config.get("leverage", 0)) != leverage:
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
