from types import SimpleNamespace

import pytest

from scripts.collect_execution_facts import collect_window, MAX_CHUNK_MS


class FakeDb:
    def __init__(self, *, persisted_income_symbols=()):
        self.queries = []
        self.persisted_income_symbols = persisted_income_symbols

    async def fetch_all(self, query, *args):
        self.queries.append((query, args))
        if "execution_fact_income" in query:
            return [{"symbol": symbol} for symbol in self.persisted_income_symbols]
        return [{"symbol": "BTCUSDT"}]


class FakeExporter:
    def __init__(self):
        self.account_id = "default"
        self.income_calls = []
        self.fill_calls = []

    async def export_income(self, start, end, *, income_type):
        self.income_calls.append((start, end, income_type))
        records = [SimpleNamespace(symbol="GRIDUSDT")] if income_type == "COMMISSION" else []
        return SimpleNamespace(records=records, page_complete=True, errors=[])

    async def export_fills(self, symbol, start, end):
        self.fill_calls.append((symbol, start, end))
        return SimpleNamespace(records=[], page_complete=True, errors=[])


@pytest.mark.asyncio
async def test_collect_window_splits_pm_trade_limit_and_covers_income_and_db_symbols():
    exporter, db = FakeExporter(), FakeDb()
    start, end = 1000, 1000 + MAX_CHUNK_MS + 4000
    summary = await collect_window(exporter, db, start, end, pause_seconds=0)
    assert len(exporter.income_calls) == 6
    assert len(exporter.fill_calls) == 4
    assert {(symbol) for symbol, _, _ in exporter.fill_calls} == {"BTCUSDT", "GRIDUSDT"}
    assert all(stop - begin <= MAX_CHUNK_MS for begin, stop, _ in exporter.income_calls)
    assert all(stop - begin <= MAX_CHUNK_MS for _, begin, stop in exporter.fill_calls)
    assert summary["complete"] is True
    assert db.queries[0][1][0].tzinfo is None  # trade_records uses Beijing-naive timestamps


@pytest.mark.asyncio
async def test_collect_window_reports_incomplete_income_without_marking_success():
    class IncompleteExporter(FakeExporter):
        async def export_income(self, start, end, *, income_type):
            if income_type == "FUNDING_FEE":
                return SimpleNamespace(records=[], page_complete=False, errors=[])
            return await super().export_income(start, end, income_type=income_type)

    summary = await collect_window(IncompleteExporter(), FakeDb(), 1000, 2000, pause_seconds=0)
    assert summary["complete"] is False
    assert any("FUNDING_FEE" in item for item in summary["gaps"])


@pytest.mark.asyncio
async def test_collect_window_retries_fills_for_persisted_income_symbol():
    class CursorCompletedExporter(FakeExporter):
        async def export_income(self, start, end, *, income_type):
            return SimpleNamespace(records=[], page_complete=True, errors=[])

    exporter = CursorCompletedExporter()
    db = FakeDb(persisted_income_symbols=("RETRYUSDT",))
    await collect_window(exporter, db, 1000, 2000, pause_seconds=0)
    assert {symbol for symbol, _, _ in exporter.fill_calls} == {"BTCUSDT", "RETRYUSDT"}
    assert db.queries[1][1][0] == "default"
