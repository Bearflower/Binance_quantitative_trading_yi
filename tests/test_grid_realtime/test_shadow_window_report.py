"""M3 数据报告必须区分完整秒轴与逐笔留存不足。"""
import json

from services.aggtrade_collector import repository as repo
from scripts.audit_grid_shadow_window import main, summarize_market_window
from strategies.grid.realtime.queue_audit import QueueAudit
from strategies.grid.realtime.reference_store import connect, ensure_schema


def _trade(trade_id, at):
    return {
        "agg_trade_id": trade_id, "symbol": "ETHUSDT", "price": "100",
        "quantity": "1", "first_trade_id": trade_id,
        "last_trade_id": trade_id, "trade_time_ms": at,
        "is_buyer_maker": 0, "received_at_ms": at,
    }


def test_market_window_dense_samples_and_contiguous_raw_ids(tmp_path):
    conn = repo.open_db(tmp_path / "agg.sqlite", 1000)
    with conn:
        repo.upsert_agg_trades(conn, [
            _trade(10, 100), _trade(11, 1200), _trade(12, 2200),
            _trade(13, 3200), _trade(14, 4200)])
        repo.materialize_closed_seconds(conn, "ETHUSDT", 5000)
    report = summarize_market_window(conn, 1000, 5000, "ETHUSDT")
    assert report["sample_coverage_complete"] is True
    assert report["raw_id_contiguous"] is True
    assert report["raw_window_full"] is True

    with conn:
        conn.execute("DELETE FROM agg_trades WHERE agg_trade_id=10")
    retained = summarize_market_window(conn, 1000, 5000, "ETHUSDT")
    assert retained["raw_id_contiguous"] is True
    assert retained["raw_window_full"] is False


def test_market_window_accepts_persisted_overlapping_id_attestations(tmp_path):
    conn = repo.open_db(tmp_path / "agg.sqlite", 1000)
    with conn:
        repo.upsert_agg_trades(conn, [
            _trade(10, 100), _trade(11, 1200), _trade(12, 2200),
            _trade(13, 3200), _trade(14, 4200)])
        repo.materialize_closed_seconds(conn, "ETHUSDT", 5000)
    repo.materialize_and_cleanup(conn, "ETHUSDT", 2, 3_600_500)
    with conn:
        conn.execute("DELETE FROM agg_trades WHERE agg_trade_id=10")
    report = summarize_market_window(conn, 1000, 5000, "ETHUSDT")
    assert report["raw_window_full"] is False
    assert report["raw_history_attested"] is True


def test_market_window_rejects_discontinuous_attestation_chain(tmp_path):
    conn = repo.open_db(tmp_path / "agg.sqlite", 1000)
    with conn:
        repo.upsert_agg_trades(conn, [
            _trade(10, 100), _trade(11, 1200), _trade(12, 2200),
            _trade(13, 3200), _trade(14, 4200)])
        repo.materialize_closed_seconds(conn, "ETHUSDT", 5000)
        conn.execute(
            "INSERT INTO aggtrade_integrity_checkpoint VALUES"
            "(10000,'ETHUSDT',10,11,2,100,1200,1),"
            "(20000,'ETHUSDT',13,14,2,3200,4200,1)")
    with conn:
        conn.execute("DELETE FROM agg_trades WHERE agg_trade_id=10")
    report = summarize_market_window(conn, 1000, 5000, "ETHUSDT")
    assert report["raw_history_attested"] is False


def test_market_window_rejects_sample_hole_and_raw_id_gap(tmp_path):
    conn = repo.open_db(tmp_path / "agg.sqlite", 1000)
    with conn:
        repo.upsert_agg_trades(conn, [
            _trade(10, 100), _trade(12, 1200), _trade(13, 2200),
            _trade(14, 3200), _trade(15, 4200)])
        repo.materialize_closed_seconds(conn, "ETHUSDT", 5000)
        conn.execute("DELETE FROM price_samples_1s WHERE sample_ms=3000")
    report = summarize_market_window(conn, 1000, 5000, "ETHUSDT")
    assert report["sample_coverage_complete"] is False
    assert report["raw_id_contiguous"] is False


def test_read_only_cli_reports_data_gate_without_claiming_m3(tmp_path, capsys):
    grid_path = tmp_path / "grid.sqlite3"
    grid = connect(str(grid_path), 1000)
    ensure_schema(grid)
    audit = QueueAudit(grid, "one", 1000)
    for at in range(1000, 5001, 1000):
        audit.checkpoint(
            at, 0, reference_status="VALID", feed_healthy=True,
            storage_degraded=False)
    grid.close()

    agg_path = tmp_path / "agg.sqlite3"
    agg = repo.open_db(agg_path, 1000)
    with agg:
        repo.upsert_agg_trades(agg, [
            _trade(10, 100), _trade(11, 1200), _trade(12, 2200),
            _trade(13, 3200), _trade(14, 4200)])
        repo.materialize_closed_seconds(agg, "ETHUSDT", 5000)
    agg.close()

    assert main([
        "--grid-db", str(grid_path), "--agg-db", str(agg_path),
        "--start-ms", "1000", "--end-ms", "5000"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["data_window_eligible"] is True
    assert report["formal_m3_eligible"] is False
