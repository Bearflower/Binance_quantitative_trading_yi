"""落库层测试：幂等、S3 决策点物化、稠密空缺点、断点续物化、清理、缺口判定。"""
import pytest

from services.aggtrade_collector import repository as repo

SYMBOL = "ETHUSDT"
NOW = 10_000_000


def make_trade(trade_id, trade_time_ms, price, first_id=None, last_id=None,
               maker=0, qty="1"):
    """构造库内成交记录（已归一化形态）。"""
    return {
        "agg_trade_id": trade_id, "symbol": SYMBOL, "price": str(price),
        "quantity": qty,
        "first_trade_id": first_id if first_id is not None else trade_id,
        "last_trade_id": last_id if last_id is not None else trade_id,
        "trade_time_ms": trade_time_ms, "is_buyer_maker": maker,
        "received_at_ms": NOW,
    }


@pytest.fixture()
def conn(tmp_path):
    c = repo.open_db(tmp_path / "test.sqlite", busy_timeout_ms=1000)
    yield c
    c.close()


def _samples_map(conn):
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute(
        "SELECT sample_ms, price, trade_count, last_trade_id, anchor_time_ms "
        "FROM price_samples_1s ORDER BY sample_ms")}


def test_upsert_idempotent_and_latest_id(conn):
    trades = [make_trade(1, 100, "10"), make_trade(2, 200, "11")]
    with conn:
        assert repo.upsert_agg_trades(conn, trades) == 2
    with conn:
        # 重复主键全部忽略，新增 0
        assert repo.upsert_agg_trades(conn, trades) == 0
    assert repo.get_latest_agg_trade_id(conn, SYMBOL) == 2
    assert repo.get_latest_agg_trade_id(conn, "OTHER") is None


def test_materialize_decision_point_semantics_with_empty_windows(conn):
    """S3：p(t)=不晚于 t.000 最后一笔；空窗口 count=0 沿用；首笔恰 .000 提升首点。"""
    trades = [
        make_trade(1, 1000, "100"),    # 整秒边界，首笔
        make_trade(2, 1500, "101"),
        make_trade(3, 2000, "102"),    # 整点归属 t=2000（ceil，不推到 3000）
        make_trade(4, 4500, "99"),     # 属未关闭点 5000
    ]
    with conn:
        repo.upsert_agg_trades(conn, trades)
        added = repo.materialize_closed_seconds(conn, SYMBOL, NOW)
    assert added == 3
    samples = _samples_map(conn)
    # 首笔 .000 计入首点；窗口 (1000,2000] 含 id2/id3，bisect 末笔 id3
    assert samples[2000] == ("102", 3, 3, 2000)
    # 无成交窗口：count=0，价格/锚点沿用
    assert samples[3000] == ("102", 0, 3, 2000)
    assert samples[4000] == ("102", 0, 3, 2000)
    # close=floor(maxT)=4000；t=5000 未关闭，id4 不进入
    assert 5000 not in samples


def test_materialize_resume_after_more_trades(conn):
    """追加成交后从最后决策点续物化，旧点不改、锚点正确沿用。"""
    with conn:
        repo.upsert_agg_trades(conn, [
            make_trade(1, 1000, "100"), make_trade(2, 1500, "101"),
            make_trade(3, 2000, "102"), make_trade(4, 4500, "99")])
        repo.materialize_closed_seconds(conn, SYMBOL, NOW)
    with conn:
        added = repo.materialize_closed_seconds(conn, SYMBOL, NOW + 1)
    assert added == 0  # 无新成交关闭新点
    with conn:
        repo.upsert_agg_trades(conn, [
            make_trade(5, 5000, "98"), make_trade(6, 5600, "97")])
        added = repo.materialize_closed_seconds(conn, SYMBOL, NOW + 2)
    assert added == 1
    samples = _samples_map(conn)
    # t=5000 窗口 (4000,5000]：id4(4500)/id5(5000)，末笔 id5@98
    assert samples[5000] == ("98", 2, 5, 5000)
    # 旧点不被回改
    assert samples[2000] == ("102", 3, 3, 2000)


def test_late_trades_do_not_rewrite_materialized(conn):
    """迟到成交不回改已物化样本（需求 §8.1）。"""
    with conn:
        repo.upsert_agg_trades(conn, [make_trade(1, 1000, "100"),
                                      make_trade(2, 2500, "101")])
        repo.materialize_closed_seconds(conn, SYMBOL, NOW)
    with conn:
        # 迟到：同窗口插入更晚 id，但物化已存在 -> INSERT OR IGNORE 不改
        repo.upsert_agg_trades(conn, [make_trade(3, 1900, "120")])
        added = repo.materialize_closed_seconds(conn, SYMBOL, NOW + 1)
    assert added == 0
    assert _samples_map(conn)[2000][0] == "100"  # 价格未被回改为 120


def test_materialize_and_cleanup_single_transaction(conn):
    """清理与物化同事务：保留窗外逐笔删除，样本保留，锚距列可独立判定（S3-iii）。"""
    old_t, new_t = 1_000_000, 2_000_000
    with conn:
        repo.upsert_agg_trades(conn, [make_trade(1, old_t, "100"),
                                      make_trade(2, new_t, "101")])
    # cutoff=now-1h=1_400_000：old(1.0M) 删、new(2.0M) 留
    now_ms = 5_000_000
    result = repo.materialize_and_cleanup(conn, SYMBOL, 1, now_ms)
    assert result["deleted"] == 1
    # 旧逐笔已删；样本仍在且 anchor_time_ms 自带，不依赖逐笔 join
    assert conn.execute("SELECT COUNT(*) FROM agg_trades").fetchone()[0] == 1
    rows = conn.execute(
        "SELECT sample_ms, anchor_time_ms FROM price_samples_1s").fetchall()
    assert rows and all(r[1] <= r[0] for r in rows)


def test_has_gap_detects_id_hole(conn):
    with conn:
        repo.upsert_agg_trades(conn, [make_trade(1, 1000, "1"),
                                      make_trade(2, 2000, "1"),
                                      make_trade(4, 4000, "1")])
    assert repo.has_gap(conn, SYMBOL, 0) is True
    assert repo.has_gap(conn, SYMBOL, 3000) is False  # 3000 之后只有 id4，无前驱不判缝


def test_schema_is_without_rowid_and_indexes(conn):
    """两表均 WITHOUT ROWID（计划 §1.2 DDL 形态）。"""
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'")}
    for table in ("agg_trades", "price_samples_1s"):
        assert "WITHOUT ROWID" in rows[table]


def test_upsert_empty_batch_is_noop(conn):
    """空批次不写任何行，直接返回 0（防御分支）。"""
    with conn:
        assert repo.upsert_agg_trades(conn, []) == 0
    assert conn.execute("SELECT COUNT(*) FROM agg_trades").fetchone()[0] == 0


def test_materialize_empty_table_returns_zero(conn):
    """空表物化：max_t 为空直接返回 0，不落任何样本。"""
    assert repo.materialize_closed_seconds(conn, SYMBOL, NOW) == 0
    assert conn.execute("SELECT COUNT(*) FROM price_samples_1s").fetchone()[0] == 0


def test_materialize_up_to_ms_caps_close(conn):
    """up_to_ms 早于 floor(maxT) 时只物化到指定上界（§1.3 可注入上界）。"""
    with conn:
        repo.upsert_agg_trades(conn, [
            make_trade(1, 1000, "100"), make_trade(2, 9500, "101")])
        # 默认上界 floor(9500)=9000 可物化 2~9 共 8 点；截到 3000 只物化 2、3 两点
        added = repo.materialize_closed_seconds(conn, SYMBOL, NOW, up_to_ms=3000)
    assert added == 2
    samples = _samples_map(conn)
    assert set(samples) == {2000, 3000}
    assert samples[3000][0] == "100"  # id2@9500 不在上界内，价格沿用


def test_append_closed_without_anchor_drops_point():
    """S3-ii 守卫：count=0 且无前锚点时不落行（首笔之前的决策点无价格可沿用）。"""
    samples = []
    repo._append_closed(samples, 2000, 0, None, None)
    assert samples == []


def test_insert_samples_empty_returns_zero(conn):
    """空样本列表不执行写库（防御分支）。"""
    assert repo._insert_samples(conn, SYMBOL, NOW, []) == 0
