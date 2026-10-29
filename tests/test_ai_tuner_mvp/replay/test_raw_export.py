"""A database raw export is read-only evidence, never a replay qualification."""

import json
import importlib
import subprocess
import sys
import types
from datetime import datetime, timezone

import pytest

from ai_tuner.replay.raw_export import export_raw_snapshot


AS_OF = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
START = datetime(2026, 10, 22, 0, tzinfo=timezone.utc)
TZ = {
    "btc_eth.kline_btcusdt_1h.open_time": {"zone": "UTC", "evidence": "verified kline writer"},
    "btc_eth.kline_btcusdt_1h.close_time": {"zone": "UTC", "evidence": "verified kline writer"},
    "btc_eth_aggressive.trade_signals.created_at": {"zone": "UTC", "evidence": "verified signal writer"},
}


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
            "btc_eth.kline_btcusdt_1h": [{"id": 1, "open_time": datetime(2026, 10, 22), "close_time": datetime(2026, 10, 22, 1), "open_price": 1, "high_price": 2, "low_price": 1, "close_price": 2, "volume": 1, "quote_volume": 2, "trade_count": 1}],
            "btc_eth_aggressive.trade_signals": [{"id": 7, "created_at": datetime(2026, 10, 22, 1), "status": "pending", "symbol": "BTCUSDT", "direction": "LONG"}],
            "hrs.hrs_signals": [{"id": 8, "triggered_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "symbol": "BTCUSDT", "direction": "SHORT", "status": "executed", "resolution": "executed"}],
            "public.ai_tuner_universe_scan_runs": [{"strategy_id": "hrs", "observed_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "created_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "source_run_id": "run-1", "config_hash": "a" * 64, "symbol_count": 1}],
            "public.ai_tuner_universe_snapshots": [{"strategy_id": "hrs", "observed_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "created_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "symbol": "BTCUSDT", "membership": "short", "source_run_id": "run-1", "config_hash": "a" * 64}],
            "public.ai_tuner_market_snapshots": [{"strategy_id": "hrs", "observed_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "source_observed_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "collected_at": datetime(2026, 10, 22, 1, tzinfo=timezone.utc), "bucket_start": datetime(2026, 10, 22, 0, tzinfo=timezone.utc), "symbol": "BTCUSDT", "open_interest_usd": 100, "funding_rate": 0, "market_cap_usd": 1000, "oi_market_cap_ratio": 0.1, "sentiment_score": 0, "oi_source": "binance", "funding_source": "binance", "market_cap_source": "coingecko", "payload_hash": "b" * 64}],
        },
        columns={
            ("btc_eth", "kline_btcusdt_1h"): {"id": "integer", "open_time": "timestamp without time zone", "close_time": "timestamp without time zone", "open_price": "numeric", "high_price": "numeric", "low_price": "numeric", "close_price": "numeric", "volume": "numeric", "quote_volume": "numeric", "trade_count": "integer"},
            ("btc_eth_aggressive", "trade_signals"): {"id": "integer", "created_at": "timestamp without time zone", "status": "character varying", "symbol": "character varying", "direction": "character varying", "entry_price": "numeric"},
            ("hrs", "hrs_signals"): {"id": "bigint", "triggered_at": "timestamp with time zone", "status": "text", "symbol": "text", "direction": "text"},
            ("public", "ai_tuner_universe_scan_runs"): {"strategy_id": "character varying", "observed_at": "timestamp with time zone", "created_at": "timestamp with time zone", "source_run_id": "character varying", "config_hash": "character", "symbol_count": "integer"},
            ("public", "ai_tuner_universe_snapshots"): {"strategy_id": "character varying", "observed_at": "timestamp with time zone", "created_at": "timestamp with time zone", "symbol": "character varying", "membership": "character varying", "source_run_id": "character varying", "config_hash": "character"},
            ("public", "ai_tuner_market_snapshots"): {"strategy_id": "character varying", "observed_at": "timestamp with time zone", "source_observed_at": "timestamp with time zone", "collected_at": "timestamp with time zone", "symbol": "character varying", "bucket_start": "timestamp with time zone", "open_interest_usd": "numeric", "funding_rate": "numeric", "market_cap_usd": "numeric", "oi_market_cap_ratio": "numeric", "sentiment_score": "numeric", "oi_source": "character varying", "funding_source": "character varying", "market_cap_source": "character varying", "payload_hash": "character"},
        },
    )


@pytest.mark.asyncio
async def test_export_is_snapshot_read_only_deterministic_and_never_passes(tmp_path):
    one = await export_raw_snapshot(fake_db(), tmp_path / "a", AS_OF, START,
                                    strategy_id="btc_eth_aggressive",
                                    kline_pairs=[("BTCUSDT", "1h")],
                                    timezone_bindings=TZ)
    two = await export_raw_snapshot(fake_db(), tmp_path / "b", AS_OF, START,
                                    strategy_id="btc_eth_aggressive",
                                    kline_pairs=[("BTCUSDT", "1h")],
                                    timezone_bindings=TZ)
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
async def test_naive_timezone_without_evidence_blocks_before_data_query(tmp_path):
    db = fake_db()
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")])
    assert result["decision"] == "BLOCKED_DATA"
    assert "naive_column_timezone_unverified:btc_eth.kline_btcusdt_1h.open_time" in result["gaps"]
    assert all("information_schema" in sql for sql, _ in db.calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_injection_in_dynamic_table_name_blocks_before_query(tmp_path):
    db = fake_db()
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive",
                                       kline_pairs=[("BTCUSDT;DROP", "1h")],
                                       timezone_bindings=TZ)
    assert result["decision"] == "BLOCKED_DATA"
    assert not db.calls


@pytest.mark.asyncio
async def test_missing_table_and_missing_column_block_without_empty_file(tmp_path):
    db = fake_db()
    db.columns.pop(("btc_eth", "kline_btcusdt_1h"))
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive",
                                       kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert result["decision"] == "BLOCKED_DATA"
    assert any("kline_btcusdt_1h" in gap for gap in result["gaps"])
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_utc_cutoff_is_parameterized_without_mutable_status_filter(tmp_path):
    db = fake_db()
    await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                              strategy_id="btc_eth_aggressive",
                              kline_pairs=[("BTCUSDT", "1h")],
                              timezone_bindings=TZ)
    assert db.tx_options == {"readonly": True, "isolation": "repeatable_read"}
    assert db.tx_exited
    data_calls = [(sql, args) for sql, args in db.calls if "information_schema" not in sql]
    assert data_calls and all("$1" in sql and "$2" in sql for sql, _ in data_calls)
    assert all("status IN" not in sql for sql, _ in data_calls)
    assert "historical_signal_status_unavailable" in json.loads((tmp_path / "raw" / "raw_manifest.json").read_text())["gaps"]
    assert all("2026-10-22" not in sql for sql, _ in data_calls)


@pytest.mark.asyncio
async def test_future_source_row_is_blocked_even_if_adapter_returns_it(tmp_path):
    db = fake_db()
    db.rows["public.ai_tuner_market_snapshots"][0]["source_observed_at"] = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs",
                                       kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
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
                                       timezone_bindings=TZ)
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
                                       timezone_bindings=TZ)
    assert "columns_missing:public.ai_tuner_market_snapshots:source_observed_at" in result["gaps"]
    assert not (tmp_path / "raw").exists()


@pytest.mark.asyncio
async def test_naive_time_bound_converts_to_verified_zone(tmp_path):
    db = fake_db()
    await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                              strategy_id="btc_eth_aggressive",
                              kline_pairs=[("BTCUSDT", "1h")],
                              timezone_bindings={key: {"zone": "Asia/Shanghai", "evidence": "verified DB timezone"} for key in TZ})
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
                                       timezone_bindings=TZ)
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
                                       timezone_bindings=TZ)
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
                                       timezone_bindings=TZ)
    assert (tmp_path / "raw" / "raw_manifest.json").exists()
    assert not any("btc_eth_aggressive" in gap for gap in result["gaps"])
    assert not any("FROM btc_eth_aggressive." in sql for sql, _ in db.calls)
    manifest = json.loads((tmp_path / "raw" / "raw_manifest.json").read_text())
    assert manifest["strategy_id"] == "hrs"
    assert "market_snapshot_historical_version_unavailable" in manifest["gaps"]
    assert "historical_signal_status_unavailable" in manifest["gaps"]
    assert "hrs_signal_attribution_unavailable" in manifest["gaps"]
    assert "source_observed_at <= bucket_start + 1 hour <= as_of" in manifest["queries"]["public.ai_tuner_market_snapshots"]["closed_bucket_rule"]
    assert manifest["queries"]["public.ai_tuner_market_snapshots"]["sort_key"]


@pytest.mark.asyncio
async def test_unclosed_hrs_market_bucket_is_blocked(tmp_path):
    db = fake_db()
    db.rows["public.ai_tuner_market_snapshots"][0]["bucket_start"] = datetime(2026, 10, 22, 1, 30, tzinfo=timezone.utc)
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert result["decision"] == "BLOCKED_DATA"
    assert any("market_bucket_not_hour_aligned" in gap for gap in result["gaps"])
    assert not (tmp_path / "raw").exists()


@pytest.mark.asyncio
async def test_signal_export_never_selects_mutable_status_or_resolution(tmp_path):
    db = fake_db()
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert result["decision"] == "BLOCKED_DATA"
    assert "historical_signal_status_unavailable" in result["gaps"]
    signal_sql = next(sql for sql, _ in db.calls if "FROM hrs.hrs_signals" in sql)
    assert "SELECT *" not in signal_sql
    assert "status" not in signal_sql and "resolution" not in signal_sql
    row = json.loads((tmp_path / "raw" / "hrs__hrs_signals.jsonl").read_text().splitlines()[0])
    assert "status" not in row and "resolution" not in row


@pytest.mark.asyncio
async def test_aggressive_signal_export_does_not_use_current_status_or_metadata(tmp_path):
    db = fake_db()
    db.rows["btc_eth_aggressive.trade_signals"][0]["metadata"] = {"future": "update"}
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert "historical_signal_status_unavailable" in result["gaps"]
    sql = next(sql for sql, _ in db.calls if "FROM btc_eth_aggressive.trade_signals" in sql)
    assert "SELECT *" not in sql and "status" not in sql and "metadata" not in sql
    row = json.loads((tmp_path / "raw" / "btc_eth_aggressive__trade_signals.jsonl").read_text())
    assert "status" not in row and "metadata" not in row and "executed_at" not in row


@pytest.mark.asyncio
async def test_market_source_observed_after_its_bucket_end_is_blocked(tmp_path):
    db = fake_db()
    db.rows["public.ai_tuner_market_snapshots"][0]["source_observed_at"] = datetime(2026, 10, 22, 1, 30, tzinfo=timezone.utc)
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="hrs", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert any("market_source_after_bucket_end" in gap for gap in result["gaps"])
    assert not (tmp_path / "raw").exists()
    sql = next(sql for sql, _ in db.calls if "FROM public.ai_tuner_market_snapshots" in sql)
    assert "source_observed_at <= bucket_start + INTERVAL '1 hour'" in sql


@pytest.mark.asyncio
async def test_every_naive_time_column_needs_own_zone_evidence(tmp_path):
    db = fake_db()
    missing_close = {key: value for key, value in TZ.items() if not key.endswith("close_time")}
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=missing_close)
    assert "naive_column_timezone_unverified:btc_eth.kline_btcusdt_1h.close_time" in result["gaps"]
    assert not (tmp_path / "raw").exists()


@pytest.mark.asyncio
async def test_aggressive_created_at_needs_independent_zone_evidence(tmp_path):
    db = fake_db()
    only_kline = {key: value for key, value in TZ.items() if key.startswith("btc_eth.")}
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=only_kline)
    assert "naive_column_timezone_unverified:btc_eth_aggressive.trade_signals.created_at" in result["gaps"]
    assert not (tmp_path / "raw").exists()


@pytest.mark.asyncio
async def test_equal_event_time_rows_sort_by_numeric_id_not_fetch_order(tmp_path):
    db = fake_db()
    first = db.rows["btc_eth.kline_btcusdt_1h"][0]
    db.rows["btc_eth.kline_btcusdt_1h"] = [{**first, "id": 10}, {**first, "id": 2}]
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert result["decision"] == "BLOCKED_DATA"
    rows = [json.loads(line) for line in (tmp_path / "raw" / "btc_eth__kline_btcusdt_1h.jsonl").read_text().splitlines()]
    assert [row["id"] for row in rows] == [2, 10]
    manifest = json.loads((tmp_path / "raw" / "raw_manifest.json").read_text())
    assert manifest["queries"]["btc_eth.kline_btcusdt_1h"]["sort_key"] == "open_time,id,canonical_row"
    sql = next(sql for sql, _ in db.calls if "FROM btc_eth.kline_btcusdt_1h" in sql)
    assert "ORDER BY open_time, id, to_jsonb(ROW(" in sql


@pytest.mark.asyncio
async def test_only_manifest_ignored_in_foreign_repo_rejects_before_db(tmp_path):
    db = fake_db()
    repo = tmp_path / "foreign"
    repo.mkdir()
    assert subprocess.run(["git", "init", "-q", str(repo)], capture_output=True).returncode == 0
    (repo / ".gitignore").write_text("manifest.json\n", encoding="utf-8")
    result = await export_raw_snapshot(db, repo / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert "raw_output_not_safe_or_git_ignored" in result["gaps"]
    assert not db.calls
    assert not (repo / "raw").exists()


@pytest.mark.asyncio
async def test_row_cap_plus_one_blocks_without_partial_output(tmp_path, monkeypatch):
    import ai_tuner.replay.raw_export as module

    monkeypatch.setattr(module, "MAX_ROWS_PER_SOURCE", 1)
    db = fake_db()
    row = db.rows["btc_eth.kline_btcusdt_1h"][0]
    db.rows["btc_eth.kline_btcusdt_1h"] = [row, {**row, "id": 2}]
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert any("row_limit_exceeded" in gap for gap in result["gaps"])
    sql, args = next((sql, args) for sql, args in db.calls if "FROM btc_eth.kline_btcusdt_1h" in sql)
    assert "LIMIT $" in sql and args[-1] == 2
    assert not (tmp_path / "raw").exists()


@pytest.mark.asyncio
async def test_window_and_pair_caps_block_before_database_access(tmp_path, monkeypatch):
    import ai_tuner.replay.raw_export as module

    db = fake_db()
    too_long = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START.replace(year=2025),
                                         strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")],
                                         timezone_bindings=TZ)
    assert any("window_limit_exceeded" in gap for gap in too_long["gaps"])
    monkeypatch.setattr(module, "MAX_KLINE_PAIRS", 1)
    too_many = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                         strategy_id="btc_eth_aggressive",
                                         kline_pairs=[("BTCUSDT", "1h"), ("ETHUSDT", "1h")],
                                         timezone_bindings=TZ)
    assert any("kline_pair_limit_exceeded" in gap for gap in too_many["gaps"])
    assert not db.calls


@pytest.mark.asyncio
async def test_total_estimated_bytes_cap_blocks_without_output(tmp_path, monkeypatch):
    import ai_tuner.replay.raw_export as module

    monkeypatch.setattr(module, "MAX_TOTAL_ESTIMATED_BYTES", 1)
    db = fake_db()
    result = await export_raw_snapshot(db, tmp_path / "raw", AS_OF, START,
                                       strategy_id="btc_eth_aggressive", kline_pairs=[("BTCUSDT", "1h")],
                                       timezone_bindings=TZ)
    assert any("total_estimated_bytes_exceeded" in gap for gap in result["gaps"])
    assert not (tmp_path / "raw").exists()


def test_cli_classifies_connection_error_without_leaking_dsn(monkeypatch, capsys):
    script = importlib.import_module("scripts.export_replay_raw")

    async def fail_connect(*, dsn):
        raise ConnectionError("secret in " + dsn)

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=fail_connect))
    monkeypatch.setenv("REPLAY_READONLY_DSN", "password=very-secret")
    code = script.main([
        "--as-of", "2026-10-22T02:00:00Z", "--window-start", "2026-10-22T00:00:00Z",
        "--strategy", "btc_eth_aggressive", "--kline", "BTCUSDT:1h",
    ])
    printed = capsys.readouterr().out
    assert code == 2
    assert json.loads(printed)["gaps"] == ["export_failed:connection"]
    assert "very-secret" not in printed and "password=" not in printed
