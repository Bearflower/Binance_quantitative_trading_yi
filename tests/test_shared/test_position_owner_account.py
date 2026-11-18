import sqlite3
import pytest
from shared.position_ownership import _open_entry_sql

@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("account,expected", [
    ("default", "btc_eth_aggressive"), ("grid_pm_ethusdt_01", "grid"),
    ("unrelated", None),
])
def test_owner_query_isolates_accounts_and_legacy_default(monkeypatch, filtered, account, expected):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", account)
    db = sqlite3.connect(":memory:")
    db.execute("ATTACH DATABASE ':memory:' AS trading")
    db.execute("CREATE TABLE trading.trade_records(id, symbol, strategy, account_id, order_type, realized_pnl, status, executed_at)")
    db.executemany("INSERT INTO trading.trade_records VALUES(?,?,?,?,?,?,?,?)", [
        (1, "ETHUSDT", "btc_eth_aggressive", None, "LIMIT", None, "FILLED", 1),
        (2, "ETHUSDT", "grid", "grid_pm_ethusdt_01", "LIMIT", None, "FILLED", 2),
    ])
    owner = db.execute(_open_entry_sql(filtered), ("ETHUSDT",)).fetchone()
    assert (owner[0] if owner else None) == expected
    db.close()
