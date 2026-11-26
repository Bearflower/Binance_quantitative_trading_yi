from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.accounting.account_cutover import FILL_SOURCES, INCOME_SOURCES, coverage_watermark
from shared.accounting.report_reader import read_monthly_economic_report
from scripts.audit_algo_attribution import _fetch_links


def cursor(source, start, end, kind=None, symbol=None):
    return dict(account_id="main", source=source, window_start_ms=start, covered_until_ms=end,
                income_type=kind, symbol=symbol, page_complete=True, request_status="ok")


def test_modes_cannot_certify_each_others_windows(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    rows = [cursor("um/income", 0, 500, "COMMISSION")]
    def watermark():
        return coverage_watermark(rows, "main", 10, 150, INCOME_SOURCES, "COMMISSION", None)
    assert watermark() == 100
    rows.append(cursor("fapi/income", 110, 150, "COMMISSION"))
    assert watermark() == 100
    rows[0] = cursor("um/income", 10, 99, "COMMISSION")
    rows.append(cursor("fapi/income", 100, 110, "COMMISSION"))
    assert watermark() == 150


@pytest.mark.asyncio
async def test_shared_uid_and_symbol_keep_three_strategies_separate_at_cutover(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    incomes, fills, owners, cursors = [], [], [], []
    for i, strategy in enumerate(("btc_eth", "btc_eth_aggressive", "hrs"), 1):
        stamp = datetime.fromtimestamp((98 + i) / 1000, timezone.utc)
        base = dict(account_id="main", symbol="BTCUSDT", trade_id=i)
        fills.append(dict(base, order_id=i, realized_pnl=i, commission=1, commission_asset="USDT",
                          trade_time_utc=stamp))
        owners.append(dict(account_id="main", symbol="BTCUSDT", order_id=i, strategy=strategy, executed_at=stamp))
        for kind, value in (("REALIZED_PNL", i), ("COMMISSION", -1)):
            incomes.append(dict(base, tran_id=i, income_type=kind, income=value, asset="USDT", event_time_utc=stamp))
    # The same numeric exchange order ID belonged to HRS in the prior PM epoch.
    owners.append(dict(owners[1], strategy="hrs", executed_at=fills[0]["trade_time_utc"]))
    incomes.append(dict(incomes[-1], income_type="FUNDING_FEE", income=Decimal("-0.5"), trade_id=None))
    for start,end,idx in ((90,99,0),(100,110,1)):
        cursors.append(cursor(FILL_SOURCES[idx], start,end,symbol="BTCUSDT"))
        for kind in ("REALIZED_PNL","COMMISSION","FUNDING_FEE"):
            cursors.append(cursor(INCOME_SOURCES[idx],start,end,kind))
    db = SimpleNamespace(fetch_all=AsyncMock(side_effect=[incomes, fills, owners, cursors]))
    report = await read_monthly_economic_report(db, "main",90,110)
    assert [row["income_count"] for row in report["mode_segments"]] == [2,5]
    assert [report["by_strategy"][s]["net_usdt"] for s in ("btc_eth", "btc_eth_aggressive", "hrs")] == ["0","1","2"]
    assert report["unattributed_usdt_total"] == "-0.5"
    assert report["account_coverage_complete"] and not report["strategy_attribution_complete"]


@pytest.mark.asyncio
async def test_classic_algo_links_never_query_pm_history(monkeypatch):
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    client=SimpleNamespace(use_unified_account=False,_request=AsyncMock(return_value=[]))
    assert await _fetch_links(client,["BTCUSDT"],10,120) == []
    args=client._request.await_args.args
    assert args[1] == "/fapi/v1/allAlgoOrders" and args[2]["startTime"] == 100


@pytest.mark.asyncio
async def test_classic_live_margin_requires_replay_of_exact_quantity_and_owner(monkeypatch):
    from shared.accounting.equity_bridge import classic_position_owner
    from shared.database import DatabaseManager

    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    stamp = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc)
    fill = dict(account_id="main", symbol="BTCUSDT", order_id=10, trade_id=20,
                trade_time_utc=stamp(120), side="BUY", qty="3")
    owner = dict(account_id="main", symbol="BTCUSDT", order_id="10", strategy="btc_eth_aggressive")
    position = dict(symbol="BTCUSDT", updateTime=120, positionSide="BOTH", positionAmt="3")

    async def check(linked, expected):
        db=SimpleNamespace(fetch_one=AsyncMock(return_value={"flat_at": stamp(110)}),
                           fetch_all=AsyncMock(side_effect=[[cursor("fapi/userTrades", 100, 130, symbol="BTCUSDT")],
                                                           [fill], [owner], linked]))
        assert await classic_position_owner(db, position, 100) == expected
        for call in db.fetch_one.await_args_list + db.fetch_all.await_args_list:
            DatabaseManager._validate_sql(None, call.args[0])
    await check([], "btc_eth_aggressive")
    await check([dict(owner, strategy="hrs")], None)
    position["positionAmt"] = "2"
    await check([], None)


@pytest.mark.asyncio
async def test_rolling_cutover_window_observes_late_income(monkeypatch):
    from scripts.export_execution_facts import ExecutionFactExporter

    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    late = dict(tranId=3, asset="USDT", incomeType="FUNDING_FEE", symbol="BTCUSDT", income="1", time=105)
    client = SimpleNamespace(use_unified_account=False, get_income_history=AsyncMock(side_effect=[[], [late]]))
    exporter = ExecutionFactExporter("main", client, refresh_completed=True)
    assert not (await exporter.export_income(100, 110, "FUNDING_FEE")).records
    assert len((await exporter.export_income(100, 120, "FUNDING_FEE")).records) == 1
    assert client.get_income_history.await_args.kwargs["start_time"] == 100


@pytest.mark.asyncio
async def test_dashboard_bridges_wallet_change_only_with_audited_anchors(monkeypatch):
    from pathlib import Path
    from datetime import date
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "dashboard/backend"))
    from dashboard.backend.services.data_service_docker import DataService
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1766620800000")
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    scope = DataService()
    scope._ensure_initialized = AsyncMock()
    scope._get_period_start_date = lambda period: date(2025, 1, 1)
    scope.get_account_equity = AsyncMock(return_value={"total_equity": "60", "account_mode": "classic"})
    scope._db_manager = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"total_equity": "100"}),
        fetch_all=AsyncMock(return_value=[dict(observation="pm_final", total_equity="110", exchange_uid_sha256="a"*64),
                                          dict(observation="classic_open", total_equity="50", exchange_uid_sha256="a"*64)]))
    result = await scope.get_account_returns("yearly")
    assert result["yield"] == 32 and result["period_pnl"] is None
    scope._db_manager.fetch_all.return_value = []
    assert (await scope.get_account_returns("yearly"))["yield_unavailable"] is True


def test_classic_algo_ownership_requires_same_mode_receipt_and_side():
    from shared.accounting.order_evidence import resolve_order_owner
    link = dict(account_mode="classic", account_id="main", symbol="ETHUSDT", side="SELL",
                algo_id=31, actual_order_id=211, payload_sha256="a" * 64)
    old = dict(account_id="main", symbol="ETHUSDT", algo_id=31, strategy_name="hrs")
    classic = dict(old, account_mode="classic", strategy_name="btc_eth_aggressive", side="SELL")
    args = ("main", "ETHUSDT", 211, [], [link])
    assert resolve_order_owner(*args, [old])[0] is None
    assert resolve_order_owner(*args, [dict(classic, side="BUY")])[0] is None
    strategy, evidence = resolve_order_owner(*args, [old, classic])
    assert strategy == "btc_eth_aggressive" and evidence["source_kind"] == "classic_algo_link"


@pytest.mark.asyncio
async def test_spot_uid_binding_uses_current_credentials_and_survives_key_rotation(monkeypatch):
    import hashlib
    from unittest.mock import MagicMock
    from shared.accounting.equity_bridge import signed_spot_uid_sha256
    spot = MagicMock()
    reader = SimpleNamespace(_request=AsyncMock(return_value={"uid": 41}))
    spot.return_value.__aenter__.return_value = reader
    monkeypatch.setattr("shared.readonly_client.ReadOnlyBinanceClient", spot)
    a = SimpleNamespace(_api_key="first", _api_secret="first-secret", testnet=False)
    digest = await signed_spot_uid_sha256(a)
    assert digest == hashlib.sha256(b"binance:uid:41").hexdigest()
    b = SimpleNamespace(_api_key="rotated", _api_secret="rotated-secret", testnet=False)
    assert await signed_spot_uid_sha256(b) == digest
    assert spot.call_args.args == ("rotated", "rotated-secret")
    assert spot.call_args.kwargs["base_url"] == "https://api.binance.com"
    reader._request.assert_awaited_with("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
    reader._request.return_value = {"uid": True}
    with pytest.raises(ValueError, match="UID"):
        await signed_spot_uid_sha256(b)


@pytest.mark.asyncio
async def test_capture_refuses_open_orders_and_persists_signed_uid(monkeypatch):
    from scripts import capture_account_mode_equity as cmd
    client = SimpleNamespace(use_unified_account=False, close=AsyncMock(),
                             get_account_info=AsyncMock(return_value=dict(totalMarginBalance="42", availableBalance="40")))
    db = SimpleNamespace(disconnect=AsyncMock())
    monkeypatch.setattr(cmd, "_build_client_from_env", lambda: (client, 0))
    monkeypatch.setattr(cmd, "_maybe_open_sink", AsyncMock(return_value=(object(), db)))
    inventory = AsyncMock(return_value=dict(flat=False))
    monkeypatch.setattr(cmd, "flat_inventory", inventory)
    monkeypatch.setattr(cmd, "signed_spot_uid_sha256", AsyncMock(return_value="a" * 64))
    sink = AsyncMock(return_value={})
    monkeypatch.setattr(cmd, "save_observation", sink)
    with pytest.raises(ValueError, match="挂单"):
        await cmd.capture("classic_open", "receipt")
    sink.assert_not_awaited()
    inventory.return_value = dict(flat=True)
    await cmd.capture("classic_open", "receipt")
    assert sink.await_args.args[-5:] == ("42", "40", 0, "receipt", "a" * 64)


@pytest.mark.asyncio
async def test_classic_income_must_not_match_pm_fill_with_reused_trade_id(monkeypatch):
    from scripts.reconcile_income_attribution import plan_reconciliation

    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    stamp = lambda n: datetime.fromtimestamp(n / 1000, timezone.utc)
    base = dict(account_id="main", symbol="BTCUSDT", trade_id=19)
    income = dict(base, event_time_utc=stamp(100), income_type="REALIZED_PNL",
                  tran_id=4, asset="USDT", income=3)
    fill = dict(base, order_id=14, trade_time_utc=stamp(99), realized_pnl=3)
    owner = dict(base, id=1, strategy="hrs", order_id=14, executed_at=stamp(99))
    db = SimpleNamespace(fetch_all=AsyncMock(side_effect=[[income], [fill], [owner], []]))
    report = await read_monthly_economic_report(db, "main", 90, 110)
    assert report["by_strategy"] == {}
    assert report["missing_income_fills"] == [dict(symbol="BTCUSDT", trade_id="19", income_type="REALIZED_PNL")]
    plan = plan_reconciliation([income], [fill], [owner], [], [], [], [], {})
    assert not plan["derived_rows"] and not plan["order_rows"]


@pytest.mark.asyncio
async def test_refresh_failure_retries_entire_window(monkeypatch):
    from scripts.export_execution_facts import ExecutionFactExporter

    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    row = dict(time=102, symbol="BTCUSDT", asset="USDT", incomeType="COMMISSION", income="-1", tranId=3)
    client = SimpleNamespace(use_unified_account=False, get_income_history=AsyncMock(side_effect=[[], [row], [row]]))
    sink = SimpleNamespace(upsert_income=AsyncMock(side_effect=[RuntimeError("disk"), None]))
    exporter = ExecutionFactExporter("main", client, sink=sink, refresh_completed=True)
    assert (await exporter.export_income(100, 110, "COMMISSION")).page_complete
    failed = await exporter.export_income(100, 120, "COMMISSION")
    assert not failed.page_complete and failed.cursor.covered_until_ms == 100
    assert (await exporter.export_income(100, 120, "COMMISSION")).page_complete
    assert client.get_income_history.await_args.kwargs["start_time"] == 100


@pytest.mark.asyncio
async def test_conflicting_duplicate_fill_invalidates_completed_cursor(monkeypatch):
    from scripts.export_execution_facts import ExecutionFactExporter

    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    fill = dict(symbol="BTCUSDT", id=1, orderId=5, commission=".1", time=100, side="BUY",
                qty="1", price="10", realizedPnl="0", commissionAsset="USDT")
    client = SimpleNamespace(use_unified_account=False, get_user_trades=AsyncMock(side_effect=[
        [fill, dict(fill, commission="0.10")], [fill, dict(fill, commission=".2")]]))
    exporter = ExecutionFactExporter("main", client, refresh_completed=True)
    first = await exporter.export_fills("BTCUSDT", 100, 110)
    assert first.page_complete and len(first.records) == 1
    conflict = await exporter.export_fills("BTCUSDT", 100, 110)
    assert not conflict.page_complete and conflict.errors and not conflict.records
    assert conflict.cursor.covered_until_ms == 100


def test_classic_algo_income_needs_signed_child_fill_side(monkeypatch):
    from scripts.reconcile_income_attribution import plan_reconciliation

    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    stamp = datetime.fromtimestamp(.101, timezone.utc)
    base = dict(account_id="main", symbol="BTCUSDT")
    fill = dict(base, order_id=44, trade_id=1, side="SELL", trade_time_utc=stamp,
                commission=".2", commission_asset="USDT")
    income = dict(base, asset="USDT", income_type="COMMISSION", income="-.2",
                  tran_id=2, trade_id=1, event_time_utc=stamp)
    link = dict(base, actual_order_id=44, algo_id=7, account_mode="classic", side="SELL", payload_sha256="a"*64)
    owner = dict(base, algo_id=7, strategy_name="hrs", account_mode="classic", side="SELL")
    def plan(child):
        return plan_reconciliation([income], [child], [], [link], [owner], [], [], {})
    assert plan(fill)["derived_rows"][0]["strategy"] == "hrs"
    assert not plan(dict(fill, side="BUY"))["derived_rows"]

@pytest.mark.asyncio
async def test_classic_fill_with_unknown_commission_cannot_certify_window(monkeypatch):
    from scripts.export_execution_facts import ExecutionFactExporter

    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    row = dict(symbol="BTCUSDT", id=1, orderId=5, time=100, side="BUY", qty="1",
               price="10", realizedPnl="0", commissionAsset="USDT")
    client = SimpleNamespace(use_unified_account=False, get_user_trades=AsyncMock(return_value=[row]))
    result = await ExecutionFactExporter("main", client).export_fills("BTCUSDT", 100, 110)
    assert not result.page_complete and result.errors and not result.records


@pytest.mark.asyncio
async def test_retry_cannot_replace_cutover_anchor(monkeypatch):
    from shared.accounting.equity_bridge import save_observation
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    stamp = datetime.fromtimestamp(2, timezone.utc)
    row = dict(total_equity=7, available_balance=6, open_positions=0, evidence_ref="receipt",
               exchange_uid_sha256="a"*64, observed_at=stamp)
    db = SimpleNamespace(execute=AsyncMock(), fetch_one=AsyncMock(return_value=row))
    args = (db, "classic_open", stamp, 7, 6, 0, "receipt", "a"*64)
    assert await save_observation(*args) == row
    for key, value in (("total_equity", 8), ("available_balance", 5), ("evidence_ref", "other")):
        db.fetch_one.return_value = {**row, key: value}
        with pytest.raises(ValueError, match="不一致"):
            await save_observation(*args)
