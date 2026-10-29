"""A database raw export is read-only evidence, never a replay qualification."""

import json
from datetime import datetime, timezone

import pytest

from ai_tuner.replay.raw_export import export_raw_snapshot


AS_OF = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
START = datetime(2026, 10, 22, 0, tzinfo=timezone.utc)


class FakeTransaction:
    def __init__(self, db, **kwargs):
        self.db = db
        self.kwargs = kwargs

    async def __aenter__(self):
        self.db.tx_entered = True

    async def __aexit__(self, *_):
        self.db.tx_exited = True


class FakeDB:
    def __init__(self, rows=None, columns=None):
        self.rows = rows or {}
        self.columns = columns or {}
        self.calls = []
        self.tx_entered = False
        self.tx_exited = False
        self.tx_options = None

    def transaction(self, **kwargs):
        self.tx_options = kwargs
        return FakeTransaction(self, **kwargs)

    async def fetch(self, sql, *args):
        assert self.tx_entered and not self.tx_exited
        assert sql.lstrip().upper().startswith("SELECT")
        self.calls.append((sql, args))
        if "information_schema.columns" in sql:
            schema, table = args
            return [dict(column_name=name, data_type=kind) for name, kind in self.columns[(schema, table)].items()]
        table = sql.split("FROM ", 1)[1].split()[0]
        return self.rows[table]


def fake_db():
    return FakeDB(
        rows={
            "btc_eth.kline_btcusdt_1h": [{"id": 1, "open_time": datetime(2026, 10, 22), "close_time": datetime(2026, 10, 22, 1), "open_price": 1}],
            "btc_eth_aggressive.trade_signals": [{"id": 7, "created_at": datetime(2026, 10, 22, 1), "status": "pending"}],
            "hrs.hrs_signals": [{"id": 8, "triggered_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc)}],
            "public.ai_tuner_universe_scan_runs": [{"strategy_id": "hrs", "observed_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "created_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc)}],
            "public.ai_tuner_universe_snapshots": [{"strategy_id": "hrs", "observed_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "created_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc)}],
            "public.ai_tuner_market_snapshots": [{"strategy_id": "hrs", "observed_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "source_observed_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "collected_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "bucket_start": datetime(2026, 10, 22, 0, tzinfo=timezone.utc)}],
        },
        columns={
            ("btc_eth", "kline_btcusdt_1h"): {"open_time": "timestamp without time zone", "close_time": "timestamp without time zone", "open_price": "numeric", "high_price": "numeric", "low_price": "numeric", "close_price": "numeric", "volume": "numeric", "quote_volume": "numeric", "trade_count": "integer"},
            ("btc_eth_aggressive", "trade_signals"): {"id": "integer", "created_at": "timestamp without time zone", "status": "character varying", "symbol": "character varying", "direction": "character varying", "entry_price": "numeric"},
            ("hrs", "hrs_signals"): {"id": "bigint", "triggered_at": "timestamp with time zone", "status": "text", "symbol": "text", "direction": "text"},
            ("public", "ai_tuner_universe_scan_runs"): {"strategy_id": "character varying", "observed_at": "timestamp with time zone", "created_at": "timestamp with time zone", "source_run_id": "character varying", "config_hash": "character"},
            ("public", "ai_tuner_universe_snapshots"): {"strategy_id": "character varying", "observed_at": "timestamp with time zone", "created_at": "timestamp with time zone", "symbol": "character varying", "membership": "character varying"},
            ("public", "ai_tuner_market_snapshots"): {"strategy_id": "character varying", "observed_at": "timestamp with time zone", "source_observed_at": "timestamp with time zone", "collected_at": "timestamp with time zone", "symbol": "character varying", "bucket_start": "timestamp with time zone"},
        },
    )


@pytest.mark.asyncio
async def test_export_is_snapshot_read_only_deterministic_and_never_passes(tmp_path):
    one = await export_raw_snapshot(fake_db(), tmp_path / "a", AS_OF, START,
                                    strategy_id="btc_eth_aggressive",
                                    kline_pairs=[("BTCUSDT", "1h")],
                                    verified_naive_timezone="UTC", timezone_evidence="DBA-reviewed configuration")
    two = await export_raw_snapshot(fake_db(), tmp_path / "b", AS_OF, START,
                                    strategy_id="btc_eth_aggressive",
                                    kline_pairs=[("BTCUSDT", "1h")],
                                    verified_naive_timezone="UTC", timezone_evidence="DBA-reviewed configuration")
    assert one["decision"] == two["decision"] == "BLOCKED_DATA"
    assert "real_funding_settlements_missing" in one["gaps"]
    assert "real_exchange_info_missing" in one["gaps"]
    assert one["source_provenance"] == "unverified"
    first = tmp_path / "a" / "raw_manifest.json"
    assert first.read_bytes() == (tmp_path / "b" / "raw_manifest.json").read_bytes()
    manifest = json.loads(first.read_text())
    assert manifest["queries"]["btc_eth.kline_btcusdt_1h"]["row_count"] == 1
    assert manifest["queries"]["btc_eth.kline_btcusdt_1h"]["min_time"] == "2026-10-22T00:00:00Z"
    assert manifest["queries"]["btc_eth.kline_btcusdt_1h"]["max_time"] == "2026-10-22T00:00:00Z"


@pytest.mark.asyncio
async def test_naive_timezone_without_evidence_blocks_before_any_query(tmp_path):
    db = fake_db()
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")])
    assert result["decision"] == "BLOCKED_DATA"
    assert "naive_db_timezone_unverified" in result["gaps"]
    assert not db.calls
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_injection_in_dynamic_table_name_blocks_before_query(tmp_path):
    db = fake_db()
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive",
                                       kline_pairs=[("BTCUSDT;DROP", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert result["decision"] == "BLOCKED_DATA"
    assert not db.calls


@pytest.mark.asyncio
async def test_missing_table_and_missing_column_block_without_empty_file(tmp_path):
    db = fake_db()
    db.columns.pop(("btc_eth", "kline_btcusdt_1h"))
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive",
                                       kline_pairs=[("BTCUSDT", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert result["decision"] == "BLOCKED_DATA"
    assert any("kline_btcusdt_1h" in gap for gap in result["gaps"])
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_utc_cutoff_and_status_are_parameterized(tmp_path):
    db = fake_db()
    await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                              strategy_id="btc_eth_aggressive",
                              kline_pairs=[("BTCUSDT", "1h")],
                              verified_naive_timezone="UTC", timezone_evidence="checked")
    assert db.tx_options == {"readonly": True, "isolation": "repeatable_read"}
    assert db.tx_exited
    data_calls = [(sql, args) for sql, args in db.calls if "information_schema" not in sql]
    assert data_calls and all("$1" in sql and "$2" in sql for sql, _ in data_calls)
    assert any("status IN" in sql and "pending" in args and "executed" in args for sql, args in data_calls)
    assert all("2026-10-22" not in sql for sql, _ in data_calls)


@pytest.mark.asyncio
async def test_future_source_row_is_blocked_even_if_adapter_returns_it(tmp_path):
    db = fake_db()
    db.rows["public.ai_tuner_market_snapshots"][0]["source_observed_at"] = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs",
                                       kline_pairs=[("BTCUSDT", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert result["decision"] == "BLOCKED_DATA"
    assert any("future" in gap for gap in result["gaps"])
    assert not (tmp_path / "raw").exists()


@pytest.mark.asyncio
async def test_confirmed_empty_table_is_gap_without_fabricated_empty_file(tmp_path):
    db = fake_db()
    db.rows["hrs.hrs_signals"] = []
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs",
                                       kline_pairs=[("BTCUSDT", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert "dataset_empty:hrs.hrs_signals" in result["gaps"]
    assert not (tmp_path / "raw" / "hrs__hrs_signals.jsonl").exists()
    manifest = json.loads((tmp_path / "raw" / "raw_manifest.json").read_text())
    assert "hrs.hrs_signals" not in manifest["queries"]


@pytest.mark.asyncio
async def test_missing_column_blocks_without_partial_export(tmp_path):
    db = fake_db()
    db.columns[("public", "ai_tuner_market_snapshots")].pop("source_observed_at")
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs",
                                       kline_pairs=[("BTCUSDT", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert "columns_missing:public.ai_tuner_market_snapshots:source_observed_at" in result["gaps"]
    assert not (tmp_path / "raw").exists()


@pytest.mark.asyncio
async def test_naive_time_bound_converts_to_verified_zone(tmp_path):
    db = fake_db()
    await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                              strategy_id="btc_eth_aggressive",
                              kline_pairs=[("BTCUSDT", "1h")],
                              verified_naive_timezone="Asia/Shanghai", timezone_evidence="verified DB timezone")
    query = next((args for sql, args in db.calls if "FROM btc_eth.kline_btcusdt_1h" in sql))
    assert query[0] == datetime(2026, 10, 22, 8)
    assert query[1] == datetime(2026, 10, 22, 10)


@pytest.mark.asyncio
async def test_tracked_output_location_is_rejected_before_database_access(tmp_path):
    from pathlib import Path

    db = fake_db()
    repo = Path(__file__).resolve().parents[3]
    result = await export_raw_snapshot(db, repo / "docs" / "raw-test", AS_OF, START,
                                       strategy_id="btc_eth_aggressive",
                                       kline_pairs=[("BTCUSDT", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert result["decision"] == "BLOCKED_DATA"
    assert "raw_output_not_safe_or_git_ignored" in result["gaps"]
    assert not db.calls


@pytest.mark.asyncio
async def test_strategy_selection_does_not_require_other_strategy_tables(tmp_path):
    db = fake_db()
    for key in list(db.columns):
        if key[0] in ("hrs", "public"):
            db.columns.pop(key)
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive",
                                       kline_pairs=[("BTCUSDT", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert result["decision"] == "BLOCKED_DATA"
    assert (tmp_path / "raw" / "raw_manifest.json").exists()
    assert not any("hrs." in gap or "public." in gap for gap in result["gaps"])
    assert not any("FROM hrs." in sql or "FROM public." in sql for sql, _ in db.calls)


@pytest.mark.asyncio
async def test_hrs_selection_does_not_require_aggressive_signal_table(tmp_path):
    db = fake_db()
    db.columns.pop(("btc_eth_aggressive", "trade_signals"))
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs", kline_pairs=[("BTCUSDT", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert (tmp_path / "raw" / "raw_manifest.json").exists()
    assert not any("btc_eth_aggressive" in gap for gap in result["gaps"])
    assert not any("FROM btc_eth_aggressive." in sql for sql, _ in db.calls)
    manifest = json.loads((tmp_path / "raw" / "raw_manifest.json").read_text())
    assert manifest["strategy_id"] == "hrs"
    assert manifest["queries"]["public.ai_tuner_market_snapshots"]["closed_bucket_rule"] == "bucket_start + 1 hour <= as_of"
    assert manifest["queries"]["public.ai_tuner_market_snapshots"]["sort_key"]


@pytest.mark.asyncio
async def test_unclosed_hrs_market_bucket_is_blocked(tmp_path):
    db = fake_db()
    db.rows["public.ai_tuner_market_snapshots"][0]["bucket_start"] = datetime(2026, 10, 22, 1, 30, tzinfo=timezone.utc)
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs", kline_pairs=[("BTCUSDT", "1h")],
                                       verified_naive_timezone="UTC", timezone_evidence="checked")
    assert result["decision"] == "BLOCKED_DATA"
    assert any("market_bucket_not_closed" in gap for gap in result["gaps"])
    assert not (tmp_path / "raw").exists()
