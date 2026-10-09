"""清单测试：provenance 去重保留首次、空库统计、落盘往返、开发/验证段口径。"""
from services.aggtrade_collector import manifest as m
from services.aggtrade_collector import repository as repo

SYMBOL = "ETHUSDT"
START_BEIJING = "2026-10-08T00:00:00+08:00"
MIN_FULL_DAYS = 30


def test_append_source_dedup_keeps_first_and_none_key(tmp_path):
    path = tmp_path / "m.json"
    data = m.load_or_create(path, tmp_path / "db.sqlite", START_BEIJING, MIN_FULL_DAYS)
    first = {"name": "a.json", "trade_rows_new": 164071}
    dup = {"name": "a.json", "trade_rows_new": 0}
    other = {"name": "b.json", "trade_rows_new": 39}
    assert m.append_source(data, first, "name") is True
    # 幂等导入命中同名来源：返回 False 且保留首次 provenance，不被 0 覆盖
    assert m.append_source(data, dup, "name") is False
    # 未命中时扫描完整列表后追加
    assert m.append_source(data, other, "name") is True
    # dedup_key=None：不做去重直接追加
    assert m.append_source(data, {"name": "rest_backfill"}, None) is True
    sources = data["development_segment"]["sources"]
    assert [s["name"] for s in sources] == ["a.json", "b.json", "rest_backfill"]
    assert sources[0]["trade_rows_new"] == 164071


def test_save_and_reload_roundtrip(tmp_path):
    path = tmp_path / "nested" / "m.json"
    db_path = tmp_path / "db.sqlite"
    data = m.load_or_create(path, db_path, START_BEIJING, MIN_FULL_DAYS)
    m.append_source(data, {"name": "a.json"}, "name")
    m.save(path, data)
    reloaded = m.load_or_create(path, db_path, "IGNORED_BY_EXISTING_FILE", 99)
    assert reloaded["development_segment"]["sources"][0]["name"] == "a.json"
    # 既有清单不被新建口径覆盖（保留首次写入值）
    assert reloaded["validation_segment"]["start_rule_beijing"] == START_BEIJING
    assert reloaded["validation_segment"]["min_full_days"] == MIN_FULL_DAYS
    assert not (path.parent / ".m.json.tmp").exists()  # 原子写临时文件已 rename
    assert reloaded["validation_segment"]["collected_in_this_db"] is False
    assert reloaded["updated_at_beijing"] is not None


def test_refresh_db_stats_empty_table_omits_range(tmp_path):
    """空表统计：rows=0 且不带 range 字段（None 分支，与有数据形态区分）。"""
    conn = repo.open_db(tmp_path / "empty.sqlite", 1000)
    try:
        data = m.load_or_create(tmp_path / "m.json", tmp_path / "empty.sqlite",
                                START_BEIJING, MIN_FULL_DAYS)
        m.refresh_db_stats(data, conn, SYMBOL)
        stats = data["db_stats"]
        assert stats["agg_trades"]["rows"] == 0
        assert "range_beijing" not in stats["agg_trades"]
        assert stats["price_samples_1s"]["rows"] == 0
        assert "range_beijing" not in stats["price_samples_1s"]
    finally:
        conn.close()


def test_refresh_db_stats_with_data(tmp_path):
    """有数据统计：两表范围与空缺点数实读自库。"""
    conn = repo.open_db(tmp_path / "db.sqlite", 1000)
    try:
        with conn:
            repo.upsert_agg_trades(conn, [
                {"agg_trade_id": 1, "symbol": SYMBOL, "price": "100",
                 "quantity": "1", "first_trade_id": 1, "last_trade_id": 1,
                 "trade_time_ms": 1000, "is_buyer_maker": 0,
                 "received_at_ms": 9},
                {"agg_trade_id": 2, "symbol": SYMBOL, "price": "101",
                 "quantity": "1", "first_trade_id": 2, "last_trade_id": 2,
                 "trade_time_ms": 1500, "is_buyer_maker": 0,
                 "received_at_ms": 9},
                {"agg_trade_id": 3, "symbol": SYMBOL, "price": "102",
                 "quantity": "1", "first_trade_id": 3, "last_trade_id": 3,
                 "trade_time_ms": 2500, "is_buyer_maker": 0,
                 "received_at_ms": 9}])
            repo.materialize_closed_seconds(conn, SYMBOL, 9)
        data = m.load_or_create(tmp_path / "m.json", tmp_path / "db.sqlite",
                                START_BEIJING, MIN_FULL_DAYS)
        m.refresh_db_stats(data, conn, SYMBOL)
        agg = data["db_stats"]["agg_trades"]
        assert agg["rows"] == 3
        assert agg["range_ms"] == {"start": 1000, "end": 2500}
        assert data["db_stats"]["price_samples_1s"]["rows"] >= 1
    finally:
        conn.close()
