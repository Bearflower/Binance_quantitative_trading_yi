import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import collect_execution_facts as collector
from shared.accounting import collector_recovery as recovery
from shared.binance_account_mode import verify_cutover_ledger
from shared.readonly_client import ReadOnlyBinanceClient, ReadOnlyViolationError

UID = "a" * 64


@pytest.fixture
def receipts():
    old_position = dict(symbol="BTCUSDT", positionSide="BOTH", positionAmt="0.25")
    pm = dict(account="main", mode="pm", start_ms=40, end_ms=50, uid_hash=UID, open=[],
              um=[old_position], um_account=[old_position.copy()], algo=[dict(symbol="BTCUSDT", algoId=6)])
    summary = dict(pm_final_anchor="missing", classic_runtime="frozen", frozen=["trading_system-btc_eth"],
                   cancelled_previous_algos=1, closed_fills=1, attributed_close_income_rows=2, accounts={
                       "main": dict(classic=True, can_trade=True, positions=0, ordinary_orders=0,
                                    algo_orders=0, available_usdt="12.50")})
    data = dict(uid=UID, account_info=dict(isPortfolioMarginRetailEnabled=False),
                classic_config=dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False),
                classic_account=dict(totalMarginBalance="12.50", availableBalance="12.50", positions=[]),
                classic_position=[], classic_orders=[], classic_algos=[],
                close_order_BTCUSDT=dict(symbol="BTCUSDT", side="SELL", positionSide="BOTH", status="FILLED",
                                         reduceOnly=True, orderId=11, executedQty="0.25", updateTime=78),
                classic_fills_BTCUSDT=[dict(symbol="BTCUSDT", id=17, orderId=11, side="SELL", positionSide="BOTH",
                                            qty="0.25", time=78, commissionAsset="USDT", commission="0.1",
                                            realizedPnl="1.2")],
                classic_income=[dict(symbol="BTCUSDT", tradeId="17", incomeType=kind, income=amount, asset="USDT", time=78)
                                for kind, amount in [("COMMISSION", "-0.1"), ("REALIZED_PNL", "1.2")]],
                classic_algo_6=dict(algoId=6, symbol="BTCUSDT", algoStatus="CANCELED", actualOrderId="", updateTime=80))
    classic = dict(account="main", start_ms=110, end_ms=115, uid_matches=True,
                   queries={key: {"data": value} for key, value in data.items()})
    return pm, summary, classic


@pytest.fixture
def stage(tmp_path, monkeypatch, receipts):
    def write(artifact, name=None):
        data = json.dumps(artifact).encode()
        sha = hashlib.sha256(data).hexdigest()
        path = tmp_path / (name or sha + ".json")
        path.write_bytes(data)
        return path, sha

    def bundle(edit=None):
        pm, summary, classic = receipts
        manifest = dict(version=1, scope="execution_facts_collector", reason="missing_pm_final",
                        account_id="default", cutover_ms=100, pm=dict(uid_sha256=UID, identity_observed_ms=50,
                            identity_evidence_sha256=write(pm)[1], closure_evidence_sha256=write(summary)[1],
                            writers_frozen=True, closure_reconciled=True),
                        classic=dict(uid_sha256=UID, observed_ms=115, snapshot_evidence_sha256=write(classic)[1],
                                     total_margin_balance="12.5", open_symbols=[], ordinary_count=0, algo_count=0))
        if edit:
            edit(manifest)
        path, sha = write(manifest, "manifest.json")
        monkeypatch.setenv("CLASSIC_COLLECTOR_RECOVERY_MANIFEST", str(path))
        monkeypatch.setenv("CLASSIC_COLLECTOR_RECOVERY_SHA256", sha)
        return path, sha
    return bundle


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "default")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    monkeypatch.setenv("EXECUTION_FACTS_DB_WRITE", "true")
    uid = AsyncMock(return_value=UID)
    monkeypatch.setattr(recovery, "signed_spot_uid_sha256", uid)
    monkeypatch.setattr("shared.accounting.equity_bridge.signed_spot_uid_sha256", uid)
    client = SimpleNamespace(use_unified_account=False, testnet=False, close=AsyncMock())
    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
    return client, db, uid


@pytest.mark.asyncio
async def test_only_collector_can_start_and_it_rechecks_before_each_collection(stage, context, monkeypatch, capsys):
    path, pin = stage()
    api, db, uid = context
    args = SimpleNamespace(start_ms=100, end_ms=160, page_limit=1000, account_id="default", pause_seconds=0)
    monkeypatch.setattr(collector, "_maybe_open_sink", AsyncMock(return_value=(object(), db)))
    monkeypatch.setattr(collector, "_maybe_close_sink", AsyncMock())
    monkeypatch.setattr(collector, "_build_client_from_env", lambda *, read_only: (api, 0) if read_only else (None, 2))
    collect = AsyncMock(return_value=dict(complete=True))
    monkeypatch.setattr(collector, "collect_window", collect)
    assert await collector._run(args) == 0
    assert json.loads(capsys.readouterr().out)["collector_recovery_sha256"] == pin
    with pytest.raises(ValueError, match="权益"):
        await verify_cutover_ledger(api, db, trading_writer=True)
    uid.return_value = "b" * 64
    with pytest.raises(ValueError, match="UID"):
        await collector._run(args)
    uid.return_value = UID
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="摘要"):
        await collector._run(args)
    collect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("source,path,value", [
    (0, ("mode",), "classic"),
    (0, ("uid_hash",), "b"*64),
    (0, ("um_account", 0, "positionAmt"), "0.1"),
    (0, ("end_ms",), 100),
    (1, ("pm_final_anchor",), "captured"),
    (1, ("classic_runtime",), "running"),
    (1, ("attributed_close_income_rows",), 0),
    (2, ("start_ms",), 98),
    (2, ("queries","account_info","data","isPortfolioMarginRetailEnabled"), True),
    (2, ("queries","account_info","data"), {}),
    (2, ("queries","classic_position","data"), [{"symbol":"ETHUSDT","positionSide":"BOTH","positionAmt":"0.2"}]),
    (2, ("queries","classic_algos","data"), [{}]),
    (2, ("queries","classic_account","data","totalMarginBalance"), "NaN"),
    (2, ("queries","classic_account","data","totalMarginBalance"), "0"),
    (2, ("queries","classic_fills_BTCUSDT","data",0,"qty"), "0.125"),
    (2, ("queries","classic_income","data",0,"income"), "-0.2"),
    (2, ("queries","classic_income","data",0,"time"), 101),
    (2, ("queries","classic_income","data",0,"time"), 79),
    (2, ("queries","close_order_BTCUSDT","data","reduceOnly"), False),
    (2, ("queries","close_order_BTCUSDT","data","updateTime"), 102),
    (2, ("queries","classic_algo_6","data","algoStatus"), "TRIGGERED"),
])
async def test_conflicting_archived_receipts_block_even_when_rehashed(stage, receipts, context, source, path, value):
    doc = receipts[source]
    for name in path[:-1]:
        doc = doc[name]
    doc[path[-1]] = value
    stage()
    api, db, _ = context
    with pytest.raises(ValueError):
        await recovery.verify_collector_ledger(api, db, "default", 100, 160)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("scope","trading_writer"), ("account_id","another"), ("cutover_ms",99),
                                       ("version",True), ("reason","general")])
async def test_manifest_scope_account_cutoff_are_bound(stage, context, key, value):
    stage(lambda d: d.update({key:value}))
    api, db, _ = context
    with pytest.raises(ValueError):
        await recovery.verify_collector_ledger(api, db, "default", 100, 160)


@pytest.mark.asyncio
async def test_config_and_database_admit_no_fallback(stage, context, monkeypatch, tmp_path):
    api, db, uid = context
    with pytest.raises(ValueError, match="权益"):
        await recovery.verify_collector_ledger(api, db, "default", 100, 160)
    path, _ = stage()
    for phase in ("classic_open","pm_final"):
        db.fetch_all.return_value = [{"observation":phase}]
        with pytest.raises(ValueError, match="已有"):
            await recovery.verify_collector_ledger(api, db, "default", 100, 160)
    db.fetch_all.return_value = []
    with pytest.raises(ValueError, match="时间"):
        await recovery.verify_collector_ledger(api, db, "default", 100, 110)
    for name in ("testnet","use_unified_account"):
        setattr(api, name, True)
        with pytest.raises(ValueError, match="实盘"):
            await recovery.verify_collector_ledger(api, db, "default", 100, 160)
        setattr(api, name, False)
    original = tmp_path / (json.loads(path.read_bytes())["pm"]["identity_evidence_sha256"] + ".json")
    original.write_bytes(b"{}")
    with pytest.raises(ValueError, match="摘要"):
        await recovery.verify_collector_ledger(api, db, "default", 100, 160)
    monkeypatch.delenv("CLASSIC_COLLECTOR_RECOVERY_SHA256")
    with pytest.raises(ValueError, match="SHA256"):
        await recovery.verify_collector_ledger(api, db, "default", 100, 160)


@pytest.mark.asyncio
async def test_collector_transport_denies_exchange_writes(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")
    api, code = collector._build_client_from_env(read_only=True)
    assert code == 0 and isinstance(api, ReadOnlyBinanceClient)
    with pytest.raises(ReadOnlyViolationError):
        await api._request("POST", "/fapi/v1/order")


@pytest.fixture
def trading_evidence(stage, monkeypatch):
    path, pin = stage(lambda m: m.update(scope="classic_trading_runtime"))
    monkeypatch.setenv("CLASSIC_TRADING_RECOVERY_MANIFEST", str(path))
    monkeypatch.setenv("CLASSIC_TRADING_RECOVERY_SHA256", pin)
    return pin


@pytest.mark.asyncio
async def test_trading_recovery_requires_matching_marker_and_keeps_ledger_gates(trading_evidence, context):
    from shared.accounting.runtime_cutover import STRATEGIES

    api, db, uid = context
    api._request = AsyncMock(return_value=dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False))
    clear = dict(reservations=False, claims=False)
    marker = dict(account="default", cutover_ms=100, version=1, strategies=list(STRATEGIES),
                  trading_recovery_sha256=trading_evidence)
    db.fetch_one = AsyncMock(side_effect=[clear, None, clear, {"state_data":dict(marker, trading_recovery_sha256="b"*64)},
                                          dict(clear, claims=True), clear, {"state_data":marker}])
    with pytest.raises(RuntimeError, match="运行状态"):
        await verify_cutover_ledger(api, db, trading_writer=True)
    with pytest.raises(ValueError, match="交易凭据"):
        await verify_cutover_ledger(api, db, trading_writer=True)
    with pytest.raises(RuntimeError, match="认领"):
        await verify_cutover_ledger(api, db, trading_writer=True)
    await verify_cutover_ledger(api, db, trading_writer=True)
    uid.assert_awaited_with(api, require_classic_trading=True)
    with pytest.raises(ValueError, match="权益"):
        await verify_cutover_ledger(api, db)
    with pytest.raises(ValueError, match="用途"):
        await recovery.verify_collector_ledger(api, db, "default", 100, 160)
    db.fetch_all.return_value = [dict(observation="pm_final")]
    with pytest.raises(ValueError, match="已有"):
        await verify_cutover_ledger(api, db, trading_writer=True)


@pytest.mark.asyncio
async def test_trading_recovery_cannot_use_collector_manifest(stage, context, monkeypatch):
    path, pin = stage()
    api, db, _ = context
    api._request = AsyncMock(return_value=dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False))
    monkeypatch.setenv("CLASSIC_TRADING_RECOVERY_MANIFEST", str(path))
    with pytest.raises(ValueError, match="SHA256"):
        await verify_cutover_ledger(api, db, trading_writer=True)
    monkeypatch.setenv("CLASSIC_TRADING_RECOVERY_SHA256", pin)
    with pytest.raises(ValueError, match="用途"):
        await verify_cutover_ledger(api, db, trading_writer=True)
    for name in ("use_unified_account", "testnet"):
        setattr(api, name, True)
        with pytest.raises(RuntimeError, match="实盘"):
            await verify_cutover_ledger(api, db, trading_writer=True)
        setattr(api, name, False)


@pytest.mark.asyncio
async def test_trading_recovery_rechecks_futures_permissions_and_live_membership(monkeypatch):
    from unittest.mock import MagicMock
    from shared.accounting.equity_bridge import signed_spot_uid_sha256

    api = SimpleNamespace(_api_key="test-key", _api_secret="test-secret", testnet=False)
    constructor = MagicMock()
    request = constructor.return_value.__aenter__.return_value._request = AsyncMock()
    monkeypatch.setattr("shared.readonly_client.ReadOnlyBinanceClient", constructor)
    for pm, read, futures in [(True, True, True), (False, True, False), (False, None, True), (None, True, True)]:
        request.side_effect = [dict(uid=12), dict(isPortfolioMarginRetailEnabled=pm),
                               dict(enableReading=read, enableFutures=futures)]
        with pytest.raises(ValueError, match="权限"):
            await signed_spot_uid_sha256(api, require_classic_trading=True)
    request.side_effect = [dict(uid=12), dict(isPortfolioMarginRetailEnabled=False),
                           dict(enableReading=True, enableFutures=True)]
    assert await signed_spot_uid_sha256(api, require_classic_trading=True) == hashlib.sha256(b"binance:uid:12").hexdigest()


@pytest.mark.asyncio
async def test_unfreeze_checks_live_flat_only_once_and_records_pin_in_transaction(trading_evidence, context):
    from unittest.mock import MagicMock
    from shared.accounting.runtime_cutover import initialize_classic_runtime

    api, db, _ = context
    api._request = AsyncMock(side_effect=lambda method, path, **kw: {
        "/fapi/v1/accountConfig": dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False),
        "/fapi/v3/positionRisk": [],
    }[path])
    api.get_account_info = AsyncMock(return_value=dict(totalMarginBalance="12.50", positions=[]))
    api.get_open_orders = AsyncMock(return_value=[])
    api.get_open_algo_orders = AsyncMock(return_value=[])
    db.pool = MagicMock()
    conn = db.pool.acquire.return_value.__aenter__.return_value
    conn.transaction = MagicMock()
    conn.fetch.return_value = []
    clear = dict(reservations=False, claims=False)
    conn.fetchrow.side_effect = [dict(clear, reservations=True)]
    with pytest.raises(RuntimeError, match="预留"):
        await initialize_classic_runtime(api, db)
    assert all("pg_advisory_xact_lock" in call.args[0] for call in conn.execute.await_args_list)

    conn.fetchrow.side_effect = [clear, None]
    conn.fetchval.side_effect = [True]
    with pytest.raises(RuntimeError, match="切点后交易"):
        await initialize_classic_runtime(api, db)
    conn.fetchrow.side_effect = [clear, None]
    conn.fetchval.side_effect = [False] + ["[]"] * 5
    await initialize_classic_runtime(api, db)
    sql, _, _, raw = conn.execute.await_args.args
    marker = json.loads(raw)
    assert "INSERT INTO public.strategy_states" in sql
    assert marker["trading_recovery_sha256"] == trading_evidence and marker["anchors"] == []
    assert all("INSERT INTO trading.account_mode_equity" not in call.args[0] for call in conn.execute.await_args_list)
    api.get_open_orders.return_value = [dict(orderId=5)]
    conn.fetchrow.side_effect = [clear, dict(state_data=marker)]
    calls = conn.execute.await_count
    await initialize_classic_runtime(api, db)
    assert conn.execute.await_count == calls + 1
    assert api.get_account_info.await_count == 2
