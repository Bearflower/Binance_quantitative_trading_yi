"""Import verified PM risk usage into a fresh, flat classic grid journal."""

import argparse
import asyncio
import hashlib
import json
import os
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import sqlite3
import time
import tempfile

from shared.accounting.equity_bridge import signed_spot_uid_sha256
from .exchange import GridExchange
from .risk import beijing_day, beijing_day_start_ms
from .store import DecisionStore, ExecutionLock

KEY = "classic_risk_continuity_v1"


def require(condition, message):
    if not condition:
        raise ValueError("classic grid continuity: " + message)


def money(value):
    number = Decimal(str(value))
    require(number.is_finite(), "invalid amount")
    return number


def transfer_key(row):
    require(
        row["incomeType"] == "TRANSFER"
        and row["asset"] == "USDT"
        and not row.get("symbol")
        and type(row["time"]) is int
        and type(row["tranId"]) is int
        and money(row["income"]) > 0,
        "invalid USDT wallet transfer",
    )
    return row["tranId"], row["time"], money(row["income"])


def accounted_transfers(record, day, transfers):
    if not record or record["day"] != day:
        return transfers
    expected = sorted(transfer_key(r) for r in record["transfers"])
    actual = sorted(transfer_key(r) for r in transfers)
    require(actual == expected, "missing, extra or changed transfer on cutover day")
    return []


def carry_reference(reference, old_equity, new_equity):
    # Preserve both absolute and percentage usage under the new wallet valuation.
    return max(
        new_equity, new_equity + reference - old_equity, reference * new_equity / old_equity
    ).quantize(Decimal(".00000001"), rounding=ROUND_CEILING)


def read_sealed(path, digest):
    raw = path.read_bytes()
    require(
        isinstance(digest, str) and len(digest) == 64 and hashlib.sha256(raw).hexdigest() == digest,
        "evidence checksum mismatch",
    )
    return raw


def archived_risk(db, day, cutover):
    bound = db.execute("SELECT value FROM schema_meta WHERE key='account_identity'").fetchone()
    require(bound is None or bound[0] == "pm", "archive has a non-PM wallet binding")
    state = db.execute("SELECT state,lock_reason FROM engine_state WHERE id=1").fetchone()
    require(state is not None and tuple(state) == ("FLAT", None), "PM risk or engine lock still open")
    for table, allowed in (
        ("grid_epoch", "'CLOSED'"),
        ("order_intent", "'FILLED','CANCELED','EXPIRED','REJECTED'"),
        ("protection", "'CANCELED'"),
    ):
        require(
            not db.execute(f"SELECT 1 FROM {table} WHERE status NOT IN ({allowed}) LIMIT 1").fetchone(),
            f"open PM {table}",
        )
    last = db.execute(
        "SELECT * FROM equity_checkpoint ORDER BY created_at_ms DESC,id DESC LIMIT 1"
    ).fetchone()
    require(
        last
        and last["baseline_verified"] == 1
        and last["day_beijing"] == day
        and last["created_at_ms"] < cutover,
        "verified final same-day PM risk observation required",
    )
    peak = max(
        money(row[0])
        for row in db.execute("SELECT peak_equity FROM equity_checkpoint WHERE baseline_verified=1")
    )
    baseline, equity = money(last["baseline_equity"]), money(last["equity"])
    require(peak >= max(baseline, equity) and min(baseline, equity) > 0, "invalid PM risk history")
    return baseline, equity, peak, last["created_at_ms"]


def verify_settlement(db, row, last_ms):
    tran_id, stamp, amount = transfer_key(row)
    match = db.execute(
        "SELECT event_ms,asset,amount,reason,reviewed_ms FROM reviewed_settlement WHERE tran_id=?",
        (str(tran_id),),
    ).fetchone()
    require(
        match
        and match["event_ms"] == stamp
        and match["asset"] == "USDT"
        and money(match["amount"]) == amount
        and stamp <= match["reviewed_ms"] <= last_ms,
        "pre-cutover transfer lacks PM journal review",
    )
    proof = json.loads(match["reason"])
    leg = proof["counterparty"]
    require(
        proof["source"] == "confirmed_same_account_margin_to_futures"
        and leg["status"] == "CONFIRMED"
        and leg["transFrom"] == "CROSS_MARGIN"
        and leg["transTo"] == "FUTURES"
        and leg["asset"] == "USDT"
        and leg["type"] == "ROLL_OUT"
        and leg["txId"] == tran_id
        and money(leg["amount"]) == amount
        and abs(leg["timestamp"] - stamp) <= 1000,
        "unconfirmed PM internal transfer",
    )


def verify_wallets(pm, classic, master, uid, cutover, opening, now_ms):
    tran_id, stamp, amount = transfer_key(opening)
    require(
        pm["account"] == classic["account"] == "grid"
        and pm["mode"] == "pm"
        and pm["uid_hash"] == uid
        and pm["start_ms"]
        <= pm["end_ms"]
        < cutover
        <= stamp
        <= classic["start_ms"]
        <= classic["end_ms"]
        <= now_ms,
        "wrong UID or cutover interval",
    )
    require(
        pm["pm_account_status"] == "NORMAL"
        and all(
            pm[k] == []
            for k in (
                "um",
                "um_account",
                "cm",
                "open",
                "algo",
                "cm_open",
                "cm_cond",
                "margin_open",
                "spot_open",
                "liabilities",
            )
        ),
        "PM grid was not flat",
    )
    (wallet,) = pm["nonzero_pm_wallets"]
    require(
        wallet["asset"] == "USDT"
        and all(
            money(wallet[k]) == amount
            for k in ("totalWalletBalance", "crossMarginFree", "crossMarginAsset")
        )
        and all(
            money(wallet[k]) == 0
            for k in (
                "crossMarginBorrowed",
                "crossMarginInterest",
                "crossMarginLocked",
                "umWalletBalance",
                "umUnrealizedPNL",
                "cmWalletBalance",
                "cmUnrealizedPNL",
                "negativeBalance",
            )
        ),
        "initial credit differs from flat PM assets",
    )
    q = {k: v["data"] for k, v in classic["queries"].items()}
    require(
        q["spot"]["uid_hash"] == uid
        and q["info"]["isPortfolioMarginRetailEnabled"] is False
        and q["um_positions"] == q["um_account"]["positions"] == q["open"] == q["algo"] == [],
        "classic snapshot identity or exposure mismatch",
    )
    (balance,) = q["um_wallet"]
    (asset,) = q["um_account"]["usdt"]
    require(
        balance["asset"] == asset["asset"] == "USDT"
        and all(
            money(value) == amount
            for value in (
                balance["balance"],
                balance["availableBalance"],
                asset["marginBalance"],
                asset["availableBalance"],
            )
        )
        and [transfer_key(r) for r in q["transfers"]] == [transfer_key(opening)],
        "classic opening wallet or transfer mismatch",
    )
    require(
        master["grid_uid"] == uid and stamp <= master["at_ms"] <= now_ms,
        "master lookup UID or time mismatch",
    )
    (margin_leg,) = master["margin_to_spot"]
    require(
        margin_leg["status"] == "CONFIRMED"
        and margin_leg["type"] == "MARGIN_MAIN"
        and margin_leg["asset"] == "USDT"
        and money(margin_leg["amount"]) == amount
        and cutover <= margin_leg["timestamp"] <= stamp,
        "PM margin cash did not move to spot",
    )
    require(len(master["universal"]) == 2, "missing directional child lookups")
    leg = master["universal"][0]
    require(
        all(r == leg for r in master["universal"])
        and leg["tranId"] == tran_id
        and leg["asset"] == "USDT"
        and leg["from_grid"] is True
        and leg["to_grid"] is True
        and leg["status"] == "SUCCESS"
        and leg["fromAccountType"] == "SPOT"
        and leg["toAccountType"] == "USDT_FUTURE"
        and money(leg["amount"]) == amount
        and abs(leg["createTimeStamp"] - stamp) <= 1000,
        "initial funding lacks matching same-child SPOT to USDT_FUTURE transfer",
    )


async def prepare(store, exchange, manifest_path, pin, now_ms):
    require(exchange.pm is False and not exchange.client.testnet, "requires classic production adapter")
    manifest = json.loads(read_sealed(manifest_path, pin))
    require(
        manifest["scope"] == "grid_classic_pm_risk_v1"
        and manifest["account_id"] == os.environ["GRID_ACCOUNT_ID"]
        and manifest["account_id"] not in ("", "main", "default"),
        "wrong scope or account",
    )
    uid = await signed_spot_uid_sha256(exchange.client)
    require(uid == manifest["uid_sha256"], "live signed account mismatch")
    identity = await exchange.journal_identity()

    def file(key, suffix=".json"):
        digest = manifest[key]
        require(
            isinstance(digest, str)
            and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest),
            "invalid sealed artifact digest",
        )
        path = manifest_path.with_name(digest + suffix)
        return read_sealed(path, digest)

    pm, classic, master = [
        json.loads(file(k))
        for k in ("pm_flat_sha256", "classic_funding_sha256", "master_transfer_sha256")
    ]
    raw = file("pm_journal_sha256", ".sqlite3")
    # Read exactly the pinned standalone backup, never an unverified WAL sidecar.
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "archived.sqlite3"
        archive.write_bytes(raw)
        archived = sqlite3.connect(archive.as_uri() + "?immutable=1", uri=True)
        try:
            archived.row_factory = sqlite3.Row
            cutover = manifest["cutover_ms"]
            day = beijing_day(now_ms)
            require(type(cutover) is int and beijing_day(cutover) == day, "requires cutover day")
            baseline, old_equity, peak, last_ms = archived_risk(archived, day, cutover)
            require(last_ms <= pm["start_ms"], "old risk observation postdates the flat PM handoff")
            rows = await exchange.income(beijing_day_start_ms(now_ms), now_ms)
            transfers = sorted(
                (r for r in rows if r["incomeType"] == "TRANSFER"), key=lambda r: r["time"]
            )
            prior, opening = transfers
            require(
                [transfer_key(r) for r in transfers] == [transfer_key(r) for r in manifest["transfers"]],
                "live cashflows differ from approved transfers",
            )
            require(
                all(
                    r["asset"] == "USDT"
                    and beijing_day_start_ms(now_ms) <= r["time"] <= now_ms
                    and (
                        r["incomeType"] == "TRANSFER"
                        or (
                            r["incomeType"] in ("REALIZED_PNL", "FUNDING_FEE", "COMMISSION")
                            and r["time"] <= last_ms
                        )
                    )
                    for r in rows
                ),
                "unreconciled cashflow after final risk observation",
            )
            verify_settlement(archived, prior, last_ms)
        finally:
            archived.close()
    verify_wallets(pm, classic, master, uid, cutover, opening, now_ms)
    amount = money(opening["income"])
    account = await exchange.account_info()
    positions = await exchange.position()
    require(
        money(account["accountEquity"]) == money(account["totalAvailableBalance"]) == amount
        and len(positions) == 1
        and money(positions[0]["positionAmt"]) == 0
        and await exchange.open_orders() == []
        and await exchange.open_stops() == []
        and await exchange.trade_history(cutover, now_ms) == []
        and await exchange.order_history(cutover, now_ms) == [],
        "classic account is not unused and flat",
    )
    record = dict(
        day=day,
        manifest_sha256=pin,
        identity=identity,
        applied_ms=now_ms,
        transfers=transfers,
        baseline=str(carry_reference(baseline, old_equity, amount)),
        peak=str(carry_reference(peak, old_equity, amount)),
        verified_pm_baseline=str(baseline),
        verified_pm_equity=str(old_equity),
        verified_pm_peak=str(peak),
    )
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        bound = conn.execute("SELECT value FROM schema_meta WHERE key='account_identity'").fetchone()
        require(bound and bound[0] == identity, "fresh journal is not bound to signed wallet")
        previous = conn.execute("SELECT value FROM schema_meta WHERE key=?", (KEY,)).fetchone()
        if previous:
            record = json.loads(previous[0])
            require(
                record["manifest_sha256"] == pin and record["identity"] == identity,
                "different continuity already applied",
            )
        else:
            state = conn.execute("SELECT state,lock_reason FROM engine_state WHERE id=1").fetchone()
            require(
                tuple(state) == ("FLAT", None)
                and not conn.execute("""SELECT
                EXISTS(SELECT 1 FROM equity_checkpoint) OR EXISTS(SELECT 1 FROM grid_epoch)
                OR EXISTS(SELECT 1 FROM execution_cursor WHERE decision_id IS NOT NULL)""").fetchone()[
                    0
                ],
                "classic journal has prior risk, lock or execution history",
            )
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?)",
                (KEY, json.dumps(record, sort_keys=True)),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return record


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    exchange = GridExchange.readonly_from_env(os.environ)
    try:
        with ExecutionLock(os.environ["GRID_EXECUTION_LOCK"]):
            store = DecisionStore(os.environ["GRID_EXECUTION_DB"])
            try:
                record = await prepare(
                    store, exchange, args.manifest, args.sha256, int(time.time() * 1000)
                )
                print(json.dumps({k: record[k] for k in ("manifest_sha256", "day", "baseline", "peak")}))
            finally:
                store.close()
    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
