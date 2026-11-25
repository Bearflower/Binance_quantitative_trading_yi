"""切换窗口逐账户核对 U 本位仓位、普通委托和条件委托，仅调用签名 GET。"""
import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from shared.binance_account_mode import configured_pm, verify_trading_mode
from shared.database import DatabaseManager
from shared.accounting.equity_bridge import signed_spot_uid_sha256
from shared.readonly_client import ReadOnlyBinanceClient


async def flat_inventory(client):
    scoped = await client.get_position()
    account = await client.get_um_account_positions()
    ordinary = await client.get_open_orders()
    conditional = await client.get_open_algo_orders()
    if not all(isinstance(rows, list) for rows in (scoped, account, ordinary, conditional)):
        raise ValueError("交易所未返回完整列表")
    symbols = set()
    for row in scoped + account:
        amount = Decimal(str(row["positionAmt"]))
        if not amount.is_finite() or not row["symbol"]:
            raise ValueError("仓位快照格式异常")
        if amount:
            symbols.add(row["symbol"])
    return dict(open_symbols=sorted(symbols), ordinary_count=len(ordinary),
                algo_count=len(conditional), flat=not (symbols or ordinary or conditional))


async def before_close(client, subaccount):
    """保留人工平仓前的仓位、委托标识和策略认领候选，不推断平仓归属。"""
    positions = [row for row in await client.get_position() if Decimal(row["positionAmt"])]
    account_positions = [row for row in await client.get_um_account_positions() if Decimal(row["positionAmt"])]
    ordinary = await client.get_open_orders()
    algos = await client.get_open_algo_orders()
    result = dict(positions=positions, account_positions=account_positions, ordinary_orders=ordinary, algo_orders=algos)
    if subaccount:
        return result
    account = os.environ["TRADE_ACCOUNT_ID"]
    symbols = sorted({row["symbol"] for row in positions + account_positions + ordinary + algos})
    db = DatabaseManager(host=os.environ["DATABASE_HOST"], port=int(os.getenv("DATABASE_PORT", "5432")),
                         database=os.environ["DATABASE_NAME"], user=os.environ["DATABASE_USER"],
                         password=os.environ["DATABASE_PASSWORD"])
    await db.connect()
    try:
        async with db.pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                # position_claims 属于策略主账户，表本身不具备子账户维度。
                claims = await conn.fetch("""SELECT symbol,strategy,trade_intent_id,claim_state,
                    created_at,expires_at FROM trading.position_claims
                    WHERE symbol=ANY($1::text[]) AND claim_state IN ('PENDING','ACTIVE')""", symbols)
                intents = await conn.fetch("""SELECT r.symbol,r.strategy_id,r.client_order_id,
                    r.state,r.created_at,o.order_id,o.executed_qty,o.source
                    FROM trading.entry_risk_reservation r
                    LEFT JOIN LATERAL (SELECT order_id,executed_qty,source
                        FROM trading.entry_order_observation a WHERE a.account_id=r.account_id
                        AND a.symbol=r.symbol AND a.client_order_id=r.client_order_id
                        AND a.outcome='ok' ORDER BY a.observed_at DESC,a.id DESC LIMIT 1) o ON true
                    WHERE r.account_id=$1 AND r.symbol=ANY($2::text[]) AND r.state<>'released'""",
                    account, symbols)
        result.update(account_id=account, claims=[dict(row) for row in claims],
                      entry_intents=[dict(row) for row in intents])
    finally:
        await db.disconnect()
    return result


async def inspect(args):
    if args.account == "main":
        prefix, mode, account_id = "BINANCE", configured_pm(), os.environ["TRADE_ACCOUNT_ID"]
    elif args.account == "grid":
        prefix, account_id = "GRID_SUBACCOUNT", os.getenv("GRID_ACCOUNT_ID", "grid_pm_ethusdt_01")
        mode = configured_pm({"BINANCE_ACCOUNT_MODE": os.getenv("GRID_SUBACCOUNT_MODE", "pm")})
    else:
        prefix, account_id = "CUTOVER_SUBACCOUNT", os.environ["CUTOVER_SUBACCOUNT_ID"]
        mode = configured_pm({"BINANCE_ACCOUNT_MODE": os.environ["CUTOVER_SUBACCOUNT_MODE"]})
    if not account_id.strip():
        raise ValueError("切换证据缺少账户标识")
    async with ReadOnlyBinanceClient(os.environ[prefix + "_API_KEY"], os.environ[prefix + "_API_SECRET"],
                                     use_unified_account=mode, instance_id="cutover") as client:
        await verify_trading_mode(client)
        uid_hash = await signed_spot_uid_sha256(client)
        result = (await before_close(client, args.account != "main") if args.before_close
                  else await flat_inventory(client))
        result.update(account_id=account_id, exchange_uid_sha256=uid_hash)
        report = json.dumps(dict(account=args.account, mode="pm" if mode else "classic",
                                 observed_at=datetime.now(timezone.utc).isoformat(), **result), default=str)
        if args.output:
            with args.output.open("x", encoding="utf-8") as out:
                out.write(report + "\n")
        else:
            print(report)
        return 0 if args.before_close or result["flat"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", choices=("main", "grid", "other"), required=True)
    parser.add_argument("--before-close", action="store_true", help="平仓前留存交易所和策略归属证据")
    parser.add_argument("--output", type=Path, help="创建新 JSON 文件，已有文件拒绝覆盖")
    raise SystemExit(asyncio.run(inspect(parser.parse_args())))
