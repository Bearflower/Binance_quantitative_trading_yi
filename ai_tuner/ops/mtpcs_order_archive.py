"""Archive signed MTPCS entry order identities in PostgreSQL, including unfilled orders."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import sys
import uuid

START_BY_STRATEGY = {
    "btc_eth": 1795416151701,  # 2026-11-23 06:42:31.701 UTC
    "btc_eth_aggressive": 1795416173036,
}
START_MS = min(START_BY_STRATEGY.values())
ACCOUNT = "default"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
PAGE_LIMIT = 1000
DAY_MS = 24 * 60 * 60 * 1000
OVERLAP_MS = 15 * 60 * 1000
SETTLE_MS = 2 * 60 * 1000
ORIGINAL_CLIENT_ID = re.compile(r"mt1([tr])[0-9a-f]{32}")

# The aggressive strategy's ID does not encode the branch. Keep the exact
# reservation join available even if the operational reservation is retired.
_AGGRESSIVE_INTENTS = """
SELECT symbol,client_order_id,geometry->'signal'->>'entry_intent' AS intent
FROM trading.entry_risk_reservation
WHERE account_id=$1 AND strategy_id='btc_eth_aggressive'
  AND geometry->'signal'->>'entry_intent' IN ('trend','mean_reversion')
UNION
SELECT payload->>'symbol',payload->>'client_order_id',payload->'geometry'->'signal'->>'entry_intent'
FROM trading.entry_activity_source_fact
WHERE account_id=$1 AND kind='intent' AND payload->>'strategy_id'='btc_eth_aggressive'
  AND payload->'geometry'->'signal'->>'entry_intent' IN ('trend','mean_reversion')
"""

_HAS_ORDER = """
SELECT EXISTS (
    SELECT 1 FROM trading.entry_order_observation
    WHERE account_id=$1 AND symbol=$2 AND order_id=$3 AND client_order_id=$4
      AND outcome='ok' AND source='signed_pm_get_order_v1'
      AND raw_payload->>'reduceOnly'='false'
      AND coalesce(raw_payload->>'closePosition','false')='false'
)
"""


def identity(row):
    order_id = str(row["orderId"])
    if not re.fullmatch(r"[1-9][0-9]*", order_id) or not isinstance(row["clientOrderId"], str):
        raise ValueError("invalid order identity")
    return row["symbol"], order_id, row["clientOrderId"]


async def history_pages(venue, symbol, start, end):
    pending = [(start, end)]
    while pending:
        left, right = pending.pop()
        rows = await venue.history(symbol, left, right - 1)
        if not isinstance(rows, list):
            raise ValueError("missing signed order history")
        if len(rows) < PAGE_LIMIT:
            yield left, right, rows
            continue
        if right - left <= 1:
            raise ValueError("signed order history is saturated at one millisecond")
        middle = (left + right) // 2
        pending.extend(((middle, right), (left, middle)))


async def scan(store, venue, start, end, *, persist):
    """Read [start, end) in bounded pages. Failed reads leave the caller's cursor in place."""
    counts = dict.fromkeys(START_BY_STRATEGY, 0)
    result = dict(matched=counts, written=0, persisted=0, covered_until_ms=end)
    intents = await store.intents()
    seen = set()
    for window_start in range(start, end, DAY_MS):
        window_end = min(end, window_start + DAY_MS)
        for symbol in SYMBOLS:
            async for left, right, rows in history_pages(venue, symbol, window_start, window_end):
                for candidate in rows:
                    key = identity(candidate)
                    at = candidate["time"]
                    if key[0] != symbol or type(at) is not int or not left <= at < right:
                        raise ValueError("unexpected signed order history identity or time")
                    _, _, client_id = key
                    if ORIGINAL_CLIENT_ID.fullmatch(client_id):
                        strategy = "btc_eth"
                        if (symbol, client_id) in intents:
                            raise ValueError("client identity matches both MTPCS versions")
                    elif (symbol, client_id) in intents:
                        strategy = "btc_eth_aggressive"
                    else:
                        continue
                    if at < START_BY_STRATEGY[strategy] or candidate.get("reduceOnly") in (True, "true"):
                        continue
                    if candidate.get("reduceOnly") not in (False, "false") or candidate.get(
                            "closePosition", False) not in (False, "false"):
                        raise ValueError("signed entry order role is unverified")
                    if key in seen:
                        continue
                    seen.add(key)
                    counts[strategy] += 1
                    if await store.contains(candidate):
                        result["persisted"] += 1
                        continue
                    signed = await venue.get_order_by_client_id(symbol, client_id)
                    if (not isinstance(signed, dict) or identity(signed) != key or signed["time"] != at
                            or signed.get("reduceOnly") not in (False, "false")
                            or signed.get("closePosition", False) not in (False, "false")):
                        raise ValueError("signed entry lookup identity or order role differs")
                    if persist:
                        result["written"] += int(await store.append(signed))
                        result["persisted"] += 1
    return result


class PostgresOrderArchive:
    def __init__(self, db, protocol, credential_fingerprint):
        self.db = db
        self.protocol = protocol
        self.fingerprint = credential_fingerprint
        self.batch = str(uuid.uuid4())

    async def intents(self):
        intents = {}
        for row in await self.db.fetch(_AGGRESSIVE_INTENTS, ACCOUNT):
            key = row["symbol"], row["client_order_id"]
            if key in intents and intents[key] != row["intent"]:
                raise ValueError("conflicting aggressive opening intent")
            intents[key] = row["intent"]
        return intents

    async def contains(self, row):
        return await self.db.fetchval(_HAS_ORDER, ACCOUNT, *identity(row))

    async def append(self, row):
        p = self.protocol
        raw = {key: value for key, value in row.items() if key in p._ORDER_FIELDS}
        symbol, order_id, client_id = identity(raw)
        original_qty, executed_qty = p._number(raw["origQty"]), p._number(raw["executedQty"])
        digest = p._digest(raw)
        p._valid_order(dict(
            account_id=ACCOUNT, client_order_id=client_id, order_id=order_id,
            symbol=symbol, side=raw["side"], status=raw["status"],
            orig_qty=original_qty, executed_qty=executed_qty, raw_payload=raw,
            payload_sha256=digest, credential_fingerprint=self.fingerprint, source=p.SOURCE,
            outcome="ok", observed_at=datetime.now(timezone.utc),
        ))
        async with self.db.transaction():
            # Use the identity lock already enforced by the observation trigger.
            await self.db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"entry-client:{ACCOUNT}:{client_id}",
            )
            if await self.contains(row):
                return False
            await self.db.execute(
                p._INSERT_ORDER, ACCOUNT, client_id, symbol, order_id, raw["side"], raw["status"],
                original_qty, executed_qty, "ok", p._json(raw), digest, self.fingerprint, p.SOURCE, self.batch,
            )
        return True


async def main(cursor, persist):
    import asyncpg
    import structlog

    from ai_tuner.allocation import activity_evidence
    from shared.readonly_client import ReadOnlyBinanceClient

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    if os.getenv("TRADE_ACCOUNT_ID", ACCOUNT) != ACCOUNT:
        raise ValueError("MTPCS PM account binding differs")

    class ReadOnlyPMOrders(ReadOnlyBinanceClient):
        async def history(self, symbol, start, end):
            # The general history wrapper converts a non-list response to an empty
            # page. The archive must distinguish an empty page from a failed read.
            return await self._request("GET", "/papi/v1/um/allOrders", {
                "symbol": symbol, "startTime": start, "endTime": end, "limit": PAGE_LIMIT,
            }, signed=True)

    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if not START_MS <= cursor <= now:
        raise ValueError("invalid MTPCS order archive cursor")
    end = max(cursor, min(now - SETTLE_MS, cursor + 2 * DAY_MS))
    start = max(START_MS, cursor - OVERLAP_MS)
    db = await asyncpg.connect(
        host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]), database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"], command_timeout=10,
    )
    api = ReadOnlyPMOrders(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"],
                           use_unified_account=True, testnet=False, instance_id="mtpcs-order-archive")
    store = PostgresOrderArchive(db, activity_evidence, hashlib.sha256(api._api_key.encode()).hexdigest())
    try:
        if persist:
            result = await scan(store, api, start, end, persist=True)
        else:
            async with db.transaction(readonly=True):
                result = await scan(store, api, start, end, persist=False)
        return dict(result, up_to_date=end >= now - SETTLE_MS)
    finally:
        await api.close()
        await db.close()


if __name__ == "__main__":
    start = int(sys.argv[1]) if sys.argv[1] else START_MS
    print(json.dumps(asyncio.run(main(start, sys.argv[2] == "persist"))))
