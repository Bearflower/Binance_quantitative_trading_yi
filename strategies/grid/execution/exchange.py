"""One-way PM and classic USD-M adapter for the ETHUSDT order grid."""

import asyncio
import time
from decimal import Decimal
from typing import Any, Dict

from shared.readonly_client import ReadOnlyBinanceClient
from shared.binance_api import BinanceClient, BinanceAPIError
from .contracts import ExecutionSettings, subaccount_pm
from .planner import ExchangeFilters
from .request_budget import GridBinanceClient


class AccountModeError(RuntimeError):
    pass


class UnknownOrderResultError(RuntimeError):
    pass


class StopCancellationUncertain(RuntimeError):
    pass


class GridExchange:
    pm = True
    history_path = "/papi/v1/um/allOrders"
    order_path = "/papi/v1/um/order"
    stop_path = "/papi/v1/um/algo/order"
    open_orders_path = "/papi/v1/um/openOrders"
    open_stops_path = "/papi/v1/um/algo/openAlgoOrders"
    query_stop_path = "/papi/v1/um/algo/algoOrder"
    position_path = "/papi/v1/um/positionRisk"
    mode_path = "/papi/v1/um/positionSide/dual"
    income_path = "/papi/v1/um/income"
    trades_path = "/papi/v1/um/userTrades"
    listen_key_path = "/papi/v1/listenKey"
    config_path = "/papi/v1/um/symbolConfig"

    def __init__(self, client: BinanceClient, wallet_client=None):
        if client.use_unified_account != self.pm:
            raise AccountModeError("grid adapter and client disagree on PM mode")
        self.client = client
        self.wallet_client = wallet_client
        self._listen_key = None
        self._one_way_checked_at = None

    @staticmethod
    def from_settings(settings: ExecutionSettings) -> "GridExchange":
        if settings.mode != "live":
            raise ValueError("grid requires explicit live settings")
        adapter = GridExchange if settings.use_unified_account else ClassicGridExchange
        wallet = (BinanceClient(api_key=settings.api_key, api_secret=settings.api_secret,
                               use_unified_account=False, testnet=False,
                               base_url="https://api.binance.com") if settings.use_unified_account else None)
        return adapter(GridBinanceClient(
            api_key=settings.api_key, api_secret=settings.api_secret,
            testnet=False, use_unified_account=settings.use_unified_account), wallet)

    @staticmethod
    def readonly_from_env(env):
        pm = subaccount_pm(env)
        client = ReadOnlyBinanceClient(env["GRID_SUBACCOUNT_API_KEY"], env["GRID_SUBACCOUNT_API_SECRET"],
                                       use_unified_account=pm, testnet=False, instance_id="grid-observer")
        return GridExchange(client) if pm else ClassicGridExchange(client)

    async def close(self):
        await self.client.close()
        if self.wallet_client is not None:
            await self.wallet_client.close()

    async def confirm_internal_settlement(self, row):
        if (self.wallet_client is None or row.get("asset") != "USDT"
                or row.get("symbol") not in ("", None)
                or Decimal(str(row.get("income", "0"))) <= 0):
            return None
        at = row["time"]
        records = await self.wallet_client._request(
            "GET", "/sapi/v1/margin/transfer",
            {"asset": "USDT", "startTime": at-1000, "endTime": at+1000})
        matches = [r for r in records.get("rows", [])
                   if str(r.get("txId")) == str(row.get("tranId"))
                   and r.get("asset") == row["asset"]
                   and Decimal(str(r.get("amount", "0"))) == Decimal(str(row["income"]))
                   and r.get("status") == "CONFIRMED" and r.get("type") == "ROLL_OUT"
                   and r.get("transFrom") == "CROSS_MARGIN" and r.get("transTo") == "FUTURES"
                   and isinstance(r.get("timestamp"), int)
                   and abs(r["timestamp"]-at) <= 1000]
        return matches[0] if len(matches) == 1 else None

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
                    identity = "clientAlgoId" if algo else "clientOrderId"
                    if (isinstance(found, dict) and found.get(identity) == client_id
                            and "code" not in found):
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
        return await self._post_once(self.order_path, params, client_id)

    async def close_position(self, side: str, qty: Decimal,
                             client_id: str) -> Dict[str, Any]:
        if side not in ("BUY", "SELL") or qty <= 0:
            raise ValueError("invalid close order")
        params = {"symbol": "ETHUSDT", "side": side, "positionSide": "BOTH",
                  "type": "MARKET", "reduceOnly": "true", "quantity": str(qty),
                  "newClientOrderId": client_id, "newOrderRespType": "RESULT"}
        return await self._post_once(self.order_path, params, client_id)

    async def place_stop(self, side: str, trigger_price: Decimal,
                         client_id: str) -> Dict[str, Any]:
        if side not in ("BUY", "SELL") or trigger_price <= 0:
            raise ValueError("invalid stop")
        params = {"algoType": "CONDITIONAL", "symbol": "ETHUSDT", "side": side,
                  "positionSide": "BOTH", "type": "STOP_MARKET",
                  "triggerPrice": str(trigger_price), "closePosition": "true",
                  "clientAlgoId": client_id, "workingType": "MARK_PRICE"}
        return await self._post_once(self.stop_path, params, client_id,
                                     algo=True)

    async def query_order(self, client_id: str):
        return await self.client._request("GET", self.order_path,
                                          {"symbol": "ETHUSDT",
                                           "origClientOrderId": client_id})

    async def query_order_by_id(self, order_id: int):
        return await self.client._request("GET", self.order_path,
                                          {"symbol": "ETHUSDT", "orderId": order_id})

    async def cancel_order(self, client_id: str):
        return await self.client._request("DELETE", self.order_path,
                                          {"symbol": "ETHUSDT",
                                           "origClientOrderId": client_id})

    async def open_orders(self):
        result = await self.client._request("GET", self.open_orders_path,
                                            {"symbol": "ETHUSDT"} if self.pm else None)
        if not isinstance(result, list):
            raise ValueError("invalid open orders response")
        return result

    async def open_stops(self):
        result = await self.client._request("GET", self.open_stops_path,
                                            {"symbol": "ETHUSDT"} if self.pm else None)
        if not isinstance(result, list):
            raise ValueError("invalid open algo orders response")
        return result

    async def query_stop(self, client_id: str):
        for attempt in range(4):
            try:
                return await self.client._request("GET", self.query_stop_path,
                                                  {"clientAlgoId": client_id})
            except BinanceAPIError as exc:
                if exc.code != -2013 or attempt == 3:
                    raise
                await asyncio.sleep(0.2 * (attempt + 1))

    async def cancel_stop(self, client_id: str):
        found = await self.query_stop(client_id)
        if (found.get("clientAlgoId") == client_id
                and found.get("algoStatus") in ("CANCELED", "EXPIRED", "REJECTED")
                and not found.get("actualOrderId")):
            return {"complete": True, "algoStatus": found["algoStatus"]}
        try:
            result = await self.client._request_once("DELETE", self.stop_path,
                                                     {"clientAlgoId": client_id}, signed=True)
        except BinanceAPIError as exc:
            if exc.code != -2011:
                raise
            for attempt in range(4):
                found = await self.query_stop(client_id)
                if (found.get("clientAlgoId") == client_id
                        and found.get("algoStatus") in ("CANCELED", "EXPIRED", "REJECTED")
                        and not found.get("actualOrderId")):
                    return {"complete": True, "algoStatus": found["algoStatus"]}
                if found.get("clientAlgoId") != client_id or found.get("actualOrderId"):
                    raise
                if attempt < 3:
                    await asyncio.sleep(0.2 * (attempt + 1))
            evidence = {key: found.get(key) for key in (
                "clientAlgoId", "algoId", "algoStatus", "actualOrderId", "updateTime")}
            raise StopCancellationUncertain(
                "DELETE -2011; terminal query not confirmed: %r" % evidence) from exc
        if not self._cancel_confirmed(result, client_id):
            raise ValueError("stop cancellation unconfirmed")
        return result

    def _cancel_confirmed(self, result, client_id):
        return isinstance(result, dict) and result.get("complete") is True

    async def position(self):
        result = await self.client._request("GET", self.position_path)
        if not isinstance(result, list):
            raise ValueError("invalid position response")
        own = []
        try:
            for row in result:
                amount = Decimal(str(row["positionAmt"]))
                if not amount.is_finite():
                    raise ValueError("non-finite position")
                if row.get("symbol") == "ETHUSDT":
                    own.append(row)
                elif amount != 0:
                    raise AccountModeError("other symbol position in dedicated account")
        except (KeyError, TypeError, ValueError) as exc:
            raise AccountModeError("malformed account position") from exc
        if not own:
            if (self._one_way_checked_at is None
                    or time.monotonic() - self._one_way_checked_at >= 30):
                mode = await self.client._request("GET", self.mode_path)
                if not isinstance(mode, dict) or mode.get("dualSidePosition") is not False:
                    self._one_way_checked_at = None
                    raise AccountModeError("one-way mode required for flat position")
                self._one_way_checked_at = time.monotonic()
            return [{"symbol": "ETHUSDT", "positionSide": "BOTH",
                     "positionAmt": "0"}]
        if len(own) != 1 or own[0].get("positionSide") != "BOTH":
            raise AccountModeError("unexpected position side")
        return own

    async def ticker_price(self) -> Decimal:
        result = await self.client._request("GET", "/fapi/v2/ticker/price",
                                            {"symbol": "ETHUSDT"}, signed=False)
        if result.get("symbol") != "ETHUSDT":
            raise ValueError("wrong ticker symbol")
        price = Decimal(str(result["price"]))
        if not price.is_finite() or price <= 0:
            raise ValueError("invalid ticker price")
        return price

    async def mark_price(self) -> Decimal:
        result = await self.client._request("GET", "/fapi/v1/premiumIndex",
                                            {"symbol": "ETHUSDT"}, signed=False)
        price = Decimal(str(result["markPrice"]))
        if result.get("symbol") != "ETHUSDT" or not price.is_finite() or price <= 0:
            raise ValueError("invalid mark price")
        return price

    async def income(self, start_ms: int, end_ms: int):
        """Read PM income directly; a full page is incomplete evidence.

        A full 1000-row page cannot be safely advanced by timestamp because
        multiple facts can share a millisecond; fail closed instead.
        """
        if not 0 < start_ms <= end_ms:
            raise ValueError("invalid income window")
        result = await self.client._request("GET", self.income_path,
                                            {"startTime": start_ms,
                                             "endTime": end_ms, "limit": 1000})
        if not isinstance(result, list) or len(result) >= 1000:
            raise ValueError("income page incomplete")
        return result

    async def order_trades(self, order_id: int, start_ms: int, end_ms: int):
        """PM userTrades has no orderId query; fetch time windows and filter."""
        if order_id <= 0:
            raise ValueError("invalid order ID")
        return [row for row in await self.trade_history(start_ms, end_ms) if row["orderId"] == order_id]

    async def trade_history(self, start_ms: int, end_ms: int):
        """Read every ETHUSDT account trade in the window, failing on a full page."""
        return await self._order_history(self.trades_path, start_ms, end_ms)

    async def order_history(self, start_ms: int, end_ms: int):
        return await self._order_history(self.history_path, start_ms, end_ms)

    async def _order_history(self, endpoint, start_ms, end_ms):
        if not 0 < start_ms <= end_ms:
            raise ValueError("invalid history window")
        seven_days_ms = 7 * 24 * 60 * 60 * 1000
        matches = []
        window_start = start_ms
        while window_start <= end_ms:
            window_end = min(end_ms, window_start + seven_days_ms - 1)
            result = await self.client._request(
                "GET", endpoint,
                {"symbol": "ETHUSDT", "startTime": window_start,
                 "endTime": window_end, "limit": 1000})
            if not isinstance(result, list) or len(result) >= 1000:
                raise ValueError("order history page incomplete")
            if any(not isinstance(row, dict) or not isinstance(row.get("orderId"), int)
                   for row in result):
                raise ValueError("malformed PM order history response")
            matches.extend(result)
            window_start = window_end + 1
        return matches

    async def start_user_stream(self) -> str:
        result = await self.client._request_once("POST", self.listen_key_path,
                                                 signed=False)
        key = result.get("listenKey") if isinstance(result, dict) else None
        if not isinstance(key, str) or not key:
            raise ValueError("missing user stream listen key")
        self._listen_key = key
        return key

    async def keepalive_user_stream(self) -> str:
        result = await self.client._request_once("PUT", self.listen_key_path,
                                                 signed=False)
        if not isinstance(result, dict) or self._listen_key is None:
            raise ValueError("user stream renewal unconfirmed")
        key = result.get("listenKey", self._listen_key if self.pm else None)
        if key != self._listen_key:
            raise ValueError("user stream listen key changed")
        return key

    async def journal_identity(self) -> str:
        return "pm"

    async def account_info(self):
        result = await self.client._request("GET", "/papi/v1/account")
        if not isinstance(result, dict) or result.get("accountStatus") != "NORMAL":
            raise AccountModeError("PM account cannot trade")
        return result

    def can_start_batch(self, order_count, *, planning=True):
        budget = getattr(self.client, 'request_budget', None)
        # Admission is checked before planning, fresh account verification and
        # any exchange writes. Pending decisions remain pending while throttled.
        required = 300 + 60 * order_count if planning else 220 + 52 * order_count
        return budget is None or budget.has_capacity(required)

    async def verify_account(self, leverage: int) -> ExchangeFilters:
        self._one_way_checked_at = None
        mode = await self.client._request("GET", self.mode_path)
        if not isinstance(mode, dict) or mode.get("dualSidePosition") is not False:
            raise AccountModeError("one-way mode required")
        self._one_way_checked_at = time.monotonic()
        configurations = await self.client._request(
            "GET", self.config_path, {"symbol": "ETHUSDT"})
        if not isinstance(configurations, list):
            raise AccountModeError("invalid symbol configuration response")
        matching = [p for p in configurations if p.get("symbol") == "ETHUSDT"]
        if len(matching) != 1:
            raise AccountModeError("cannot confirm ETHUSDT symbol configuration")
        config = matching[0]
        if config.get("marginType", "").lower() != "crossed":
            raise AccountModeError("crossed margin required")
        if int(config.get("leverage", 0)) != leverage:
            raise AccountModeError("leverage mismatch")
        await self.account_info()
        await self.position()
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


class ClassicGridExchange(GridExchange):
    pm = False
    history_path = "/fapi/v1/allOrders"
    order_path = "/fapi/v1/order"
    stop_path = "/fapi/v1/algoOrder"
    open_orders_path = "/fapi/v1/openOrders"
    open_stops_path = "/fapi/v1/openAlgoOrders"
    query_stop_path = stop_path
    position_path = "/fapi/v3/positionRisk"
    mode_path = "/fapi/v1/positionSide/dual"
    income_path = "/fapi/v1/income"
    trades_path = "/fapi/v1/userTrades"
    listen_key_path = "/fapi/v1/listenKey"
    config_path = "/fapi/v1/symbolConfig"

    def _cancel_confirmed(self, result, client_id):
        return (isinstance(result, dict) and str(result.get("code")) == "200"
                and result.get("clientAlgoId") == client_id)

    async def account_info(self):
        config = await self.client._request("GET", "/fapi/v1/accountConfig")
        if (not isinstance(config, dict) or config.get("canTrade") is not True
                or config.get("multiAssetsMargin") is not False
                or config.get("dualSidePosition") is not False):
            raise AccountModeError("classic one-way, single-asset trading account required")
        account = await self.client._request("GET", "/fapi/v3/account")
        if not isinstance(account, dict):
            raise AccountModeError("invalid classic account response")
        # 单资产模式下，V3 的总保证金和可用余额以 USDT 计价。
        equity = Decimal(str(account["totalMarginBalance"]))
        available = Decimal(str(account["availableBalance"]))
        if not equity.is_finite() or not available.is_finite():
            raise AccountModeError("invalid classic account balances")
        result = {"accountEquity": equity, "totalAvailableBalance": available}
        for source, target in (("totalPositionInitialMargin", "positionInitialMargin"),
                               ("totalOpenOrderInitialMargin", "openOrderInitialMargin")):
            if source in account:
                value = Decimal(str(account[source]))
                if not value.is_finite() or value < 0:
                    raise AccountModeError("invalid classic margin balances")
                result[target] = value
        return result

    async def journal_identity(self) -> str:
        rows = await self.client._request("GET", "/fapi/v3/balance")
        if not isinstance(rows, list):
            raise AccountModeError("missing classic account balance")
        wallets = [row for row in rows if row.get("asset") == "USDT"]
        alias = wallets[0].get("accountAlias") if len(wallets) == 1 else None
        if not isinstance(alias, str) or not alias.strip():
            raise AccountModeError("missing USDT account alias")
        return "classic:" + alias
