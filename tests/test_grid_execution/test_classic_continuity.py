import hashlib
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution import classic_continuity as bridge
from strategies.grid.execution.risk import RiskLimits, RiskStatus, beijing_day
from strategies.grid.execution.risk_tracker import RiskTracker
from strategies.grid.execution.store import DecisionStore, migrate


NOW = 1_800_000_000_000
DAY = beijing_day(NOW)
ID = "classic:wallet"
UID = "a" * 64


def transfer(tx, at, value):
    return {
        "incomeType": "TRANSFER",
        "asset": "USDT",
        "income": value,
        "time": at,
        "tranId": tx,
        "symbol": "",
    }


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_ACCOUNT_ID", "grid")
    monkeypatch.setattr(bridge, "signed_spot_uid_sha256", AsyncMock(return_value=UID))
    old = tmp_path / "pm.sqlite3"
    migrate(old)
    pm_store = DecisionStore(old)
    pm_store.record_equity_checkpoint(
        DAY,
        Decimal("99"),
        Decimal("98"),
        Decimal("110"),
        NOW - 2000,
        verified=True,
        transfer_fingerprint="none",
    )
    prior, opening = transfer(1, NOW - 3000, "1"), transfer(2, NOW - 600, "100")
    old_leg = dict(
        asset="USDT",
        amount="1",
        status="CONFIRMED",
        timestamp=NOW - 3000,
        txId=1,
        transFrom="CROSS_MARGIN",
        transTo="FUTURES",
        type="ROLL_OUT",
    )
    pm_store.conn.execute(
        "CREATE TABLE reviewed_settlement(tran_id TEXT,event_ms,asset,amount,reason,reviewed_ms)"
    )
    pm_store.conn.execute(
        "INSERT INTO reviewed_settlement VALUES(?,?,?,?,?,?)",
        (
            "1",
            NOW - 3000,
            "USDT",
            "1",
            json.dumps({"source": "confirmed_same_account_margin_to_futures", "counterparty": old_leg}),
            NOW - 2500,
        ),
    )
    pm_store.close()
    path = tmp_path / "exec.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    store.bind_account(ID)
    cash = dict(
        asset="USDT",
        totalWalletBalance="100",
        crossMarginFree="100",
        crossMarginAsset="100",
        crossMarginBorrowed="0",
        crossMarginInterest="0",
        crossMarginLocked="0",
        umWalletBalance="0",
        umUnrealizedPNL="0",
        cmWalletBalance="0",
        cmUnrealizedPNL="0",
        negativeBalance="0",
    )
    pm = dict(
        account="grid",
        mode="pm",
        uid_hash=UID,
        start_ms=NOW - 1800,
        end_ms=NOW - 1700,
        pm_account_status="NORMAL",
        nonzero_pm_wallets=[cash],
        um=[],
        um_account=[],
        open=[],
        algo=[],
        cm=[],
        cm_open=[],
        cm_cond=[],
        margin_open=[],
        spot_open=[],
        liabilities=[],
    )
    payload = dict(
        spot={"uid_hash": UID},
        info={"isPortfolioMarginRetailEnabled": False},
        um_positions=[],
        open=[],
        algo=[],
        um_wallet=[dict(asset="USDT", balance="100", availableBalance="100")],
        um_account={
            "positions": [],
            "usdt": [dict(asset="USDT", marginBalance="100", availableBalance="100")],
        },
        transfers=[opening],
    )
    classic = dict(
        account="grid",
        start_ms=NOW - 500,
        end_ms=NOW - 400,
        queries={k: {"data": v} for k, v in payload.items()},
    )
    leg = dict(
        tranId=2,
        asset="USDT",
        amount="100",
        createTimeStamp=NOW - 600,
        status="SUCCESS",
        fromAccountType="SPOT",
        toAccountType="USDT_FUTURE",
        from_grid=True,
        to_grid=True,
    )
    master = dict(
        grid_uid=UID,
        at_ms=NOW - 200,
        universal=[leg.copy(), leg.copy()],
        margin_to_spot=[
            dict(status="CONFIRMED", type="MARGIN_MAIN", asset="USDT", amount="100", timestamp=NOW - 650)
        ],
    )

    def write(payload, ext):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        h = hashlib.sha256(data).hexdigest()
        (tmp_path / (h + ext)).write_bytes(data)
        return h

    def seal():
        manifest = dict(
            scope="grid_classic_pm_risk_v1",
            account_id="grid",
            uid_sha256=UID,
            cutover_ms=NOW - 1000,
            transfers=[prior, opening],
            pm_flat_sha256=write(pm, ".json"),
            classic_funding_sha256=write(classic, ".json"),
            master_transfer_sha256=write(master, ".json"),
            pm_journal_sha256=write(old.read_bytes(), ".sqlite3"),
        )
        pin = write(manifest, ".json")
        return tmp_path / (pin + ".json"), pin

    api = SimpleNamespace(
        pm=False,
        client=SimpleNamespace(testnet=False),
        income=AsyncMock(return_value=[prior, opening]),
        account_info=AsyncMock(return_value=dict(accountEquity="100", totalAvailableBalance="100")),
        position=AsyncMock(return_value=[{"positionAmt": "0"}]),
        open_orders=AsyncMock(return_value=[]),
        open_stops=AsyncMock(return_value=[]),
        trade_history=AsyncMock(return_value=[]),
        order_history=AsyncMock(return_value=[]),
        journal_identity=AsyncMock(return_value=ID),
    )
    yield SimpleNamespace(
        store=store, api=api, seal=seal, master=master, pm=pm, old=old, opening=opening
    )
    store.close()


@pytest.mark.asyncio
async def test_import_preserves_loss_and_peak_through_restart_and_rollover(case, monkeypatch):
    path, pin = case.seal()
    apply = lambda: bridge.prepare(case.store, case.api, path, pin, NOW)
    record = await apply()
    assert record == await apply()
    assert Decimal(record["baseline"]) >= Decimal("100") * 99 / 98
    assert Decimal(record["peak"]) >= Decimal("100") * 110 / 98
    assert case.store.latest_equity_checkpoint(DAY) is None
    monkeypatch.setenv("GRID_CLASSIC_RISK_CONTINUITY_SHA256", pin)

    def tracker():
        return RiskTracker(case.store, case.api, RiskLimits(Decimal("15"), Decimal("30")))

    risk = await tracker().assess(NOW)
    assert risk.status == RiskStatus.ALLOW and risk.daily_loss_usdt >= 1 and risk.drawdown_usdt >= 12
    case.api.income.return_value.append(transfer(3, NOW - 40, "100"))
    assert (await tracker().assess(NOW)).status == RiskStatus.UNKNOWN_LOCKED
    case.api.income.return_value = []
    assert (await tracker().assess(NOW)).status == RiskStatus.UNKNOWN_LOCKED
    case.api.account_info.return_value = dict(accountEquity="93", totalAvailableBalance="93")
    assert (await tracker().assess(NOW + 86_400_000)).status == RiskStatus.EXIT_LOCKED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "old_lock",
        "new_lock",
        "completed_epoch",
        "other_child",
        "uid",
        "changed_amount",
        "wrong_cash_leg",
        "unreviewed_pm",
        "unexpected_income",
        "new_position",
        "canceled_order",
        "missing_transfer",
    ],
)
async def test_requires_flat_unused_account_and_exact_historical_evidence(case, bad, monkeypatch):
    if bad == "old_lock" or bad == "unreviewed_pm":
        with sqlite3.connect(case.old) as old:
            old.execute(
                "UPDATE engine_state SET state='LOCKED'"
                if bad == "old_lock"
                else "DELETE FROM reviewed_settlement"
            )
            old.commit()
        old.close()
    elif bad == "new_lock":
        case.store.set_engine_state("LOCKED", "daily loss")
    elif bad == "completed_epoch":
        case.store.conn.execute(
            "INSERT INTO grid_epoch(decision_id,plan_digest,status,created_at_ms) VALUES('x','y','CLOSED',?)",
            (NOW - 5,),
        )
    elif bad == "other_child":
        case.master["universal"][0]["to_grid"] = False
    elif bad == "uid":
        monkeypatch.setattr(bridge, "signed_spot_uid_sha256", AsyncMock(return_value="b" * 64))
    elif bad == "changed_amount":
        case.pm["nonzero_pm_wallets"][0]["crossMarginFree"] = "101"
    elif bad == "wrong_cash_leg":
        case.master["margin_to_spot"][0]["type"] = "UMFUTURE_MAIN"
    elif bad == "unexpected_income":
        case.api.income.return_value.append(dict(incomeType="FUNDING_FEE", time=NOW - 10, asset="USDT"))
    elif bad == "canceled_order":
        case.api.order_history.return_value = [{"status": "CANCELED"}]
    elif bad == "new_position":
        case.api.position.return_value = [{"positionAmt": "1"}]
    else:
        case.api.income.return_value = []
    with pytest.raises(ValueError):
        await bridge.prepare(case.store, case.api, *case.seal(), NOW)
    assert case.store.latest_equity_checkpoint(DAY) is None
    assert not case.store.conn.execute("SELECT 1 FROM schema_meta WHERE key=?", (bridge.KEY,)).fetchone()
