import json

from dashboard.backend.services.grid_subaccount import read_grid_snapshot


def payload():
    return {"version": 1, "account_scope": "grid_subaccount_pm", "symbol": "ETHUSDT",
            "as_of_ms": 1000, "cumulative_pnl_usdt": "0", "stage_pnl_usdt": "0",
            "net_qty": "0", "entry_price": "0", "mark_price": "0",
            "unrealized_pnl_usdt": "0", "realized_net_usdt": "0", "funding_usdt": "0",
            "fill_count": 0, "order_count": 0, "open_order_count": 0,
            "engine_state": "FLAT", "risk_status": "ALLOW"}


def test_reader_rejects_stale_or_malformed_snapshot(tmp_path):
    file = tmp_path / "grid.json"
    file.write_text(json.dumps(payload()))
    assert read_grid_snapshot(file, now_ms=2000)["status"] == "ok"
    assert read_grid_snapshot(file, now_ms=200_000)["reason"] == "snapshot_stale"
    file.write_text('{')
    assert read_grid_snapshot(file, now_ms=2000)["reason"] == "snapshot_invalid"


def test_reader_never_returns_unexpected_or_secret_fields(tmp_path):
    file = tmp_path / "grid.json"
    file.write_text(json.dumps(dict(payload(), api_key="secret")))
    result = read_grid_snapshot(file, now_ms=2000)
    assert "api_key" not in result
