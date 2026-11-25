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
