import pytest

from ai_tuner.ops import mtpcs_order_archive as archive


def order(number, client_id, timestamp, *, reduce_only=False, status="FILLED"):
    return dict(orderId=number, clientOrderId=client_id, time=timestamp, symbol="ETHUSDT",
                side="BUY", reduceOnly=reduce_only, positionSide="BOTH", status=status,
                origQty="0.5", executedQty="0.5" if status == "FILLED" else "0")


class Venue:
    def __init__(self, rows):
        self.rows = rows
        self.lookups = []

    async def history(self, symbol, start, end):
        return self.rows if symbol == "ETHUSDT" else []

    async def get_order_by_client_id(self, symbol, client_id):
        self.lookups.append(client_id)
        return next(row for row in self.rows if row["clientOrderId"] == client_id)


class Store:
    def __init__(self, aggressive_id):
        self.aggressive_id = aggressive_id
        self.durable_rows = {}

    async def intents(self):
        return {("ETHUSDT", self.aggressive_id): "mean_reversion"}

    async def contains(self, row):
        return (row["symbol"], str(row["orderId"]), row["clientOrderId"]) in self.durable_rows

    async def append(self, row):
        self.durable_rows[(row["symbol"], str(row["orderId"]), row["clientOrderId"])] = row
        return True


@pytest.mark.asyncio
async def test_distinct_same_hour_orders_and_cancelled_order_survive_restart():
    cutoff = archive.START_MS + 60_000
    trend = "mt1t" + "0" * 32
    reversion = "mt1r" + "1" * 32
    aggressive = "risk-controlled"
    close = "mt1r" + "2" * 32
    rows = [
        order(11, trend, cutoff),
        order(12, reversion, cutoff + 100, status="CANCELED"),
        order(13, aggressive, cutoff + 200),
        order(14, close, cutoff + 300, reduce_only=True),
        order(15, "other-strategy", cutoff + 400),
    ]
    venue, database = Venue(rows), Store(aggressive)
    result = await archive.scan(database, venue, archive.START_MS, cutoff + 1000, persist=True)
    assert result["written"] == 3
    assert result["matched"] == {"btc_eth": 2, "btc_eth_aggressive": 1}
    assert set(database.durable_rows) == {
        ("ETHUSDT", "11", trend), ("ETHUSDT", "12", reversion), ("ETHUSDT", "13", aggressive),
    }

    restarted_venue = Venue(rows)
    retry = await archive.scan(database, restarted_venue, archive.START_MS, cutoff + 1000, persist=True)
    assert retry["written"] == 0
    assert retry["persisted"] == 3
    assert restarted_venue.lookups == []


@pytest.mark.asyncio
async def test_preview_never_appends_and_identity_mismatch_fails_closed():
    cutoff = archive.START_MS + 60_000
    client_id = "mt1t" + "f" * 32
    marked = order(16, client_id, cutoff)
    database, venue = Store("unused"), Venue([marked])
    result = await archive.scan(database, venue, archive.START_MS, cutoff + 1000, persist=False)
    assert result["matched"]["btc_eth"] == 1
    assert result["persisted"] == result["written"] == 0
    assert not database.durable_rows

    async def wrong_order(symbol, cid):
        return order(17, cid, cutoff)

    venue.get_order_by_client_id = wrong_order
    with pytest.raises(ValueError, match="identity"):
        await archive.scan(database, venue, archive.START_MS, cutoff + 1000, persist=True)
    assert not database.durable_rows


@pytest.mark.asyncio
async def test_capped_or_unexpected_history_does_not_report_coverage():
    venue, database = Venue([{}] * archive.PAGE_LIMIT), Store("unused")
    with pytest.raises(ValueError, match="history"):
        await archive.scan(database, venue, archive.START_MS, archive.START_MS + 60_000, persist=True)
    assert not database.durable_rows


@pytest.mark.asyncio
async def test_saturated_day_is_split_before_advancing_checkpoint():
    start = archive.START_MS
    marked = order(18, "mt1r" + "c" * 32, start + 90_000)

    class SaturatedVenue(Venue):
        async def history(self, symbol, left, right):
            if symbol != "ETHUSDT":
                return []
            if right - left >= 120_000:
                return [{}] * archive.PAGE_LIMIT
            return [row for row in self.rows if left <= row["time"] <= right]

    end = start + 240_000
    database = Store("unused")
    result = await archive.scan(database, SaturatedVenue([marked]), start, end, persist=True)
    assert result["written"] == 1
    assert result["covered_until_ms"] == end
