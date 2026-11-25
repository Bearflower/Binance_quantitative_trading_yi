"""在同一账户零敞口切点归档运行状态，并保留历史交易、计数与风控。"""
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

from shared.accounting.account_cutover import account_cutover_ms
from shared.accounting.collector_recovery import trading_recovery_configured, verify_trading_recovery
from shared.binance_account_mode import check_pm_claims

STRATEGIES = ("btc_eth", "btc_eth_aggressive", "new_coin")
_STATE_OWNER = "account_mode_runtime"
_READ = "SELECT state_data FROM public.strategy_states WHERE strategy_name=$1 AND state_key=$2"


def classic_epoch(client):
    if getattr(client, "use_unified_account", True) is not False or getattr(client, "testnet", False) is True:
        return None
    account = os.environ.get("TRADE_ACCOUNT_ID", "").strip()
    cutoff = account_cutover_ms(account)
    if not account or cutoff is None:
        raise ValueError("经典运行状态缺少账户或切点")
    return account, cutoff


def stop_journal_path(client, base):
    epoch = classic_epoch(client)
    if epoch is None:
        return base
    account, cutoff = epoch
    path = Path(base)
    identity = hashlib.sha256(account.encode()).hexdigest()[:16]
    return path.with_name(f"{path.stem}.classic-{identity}-{cutoff}{path.suffix}")


def runtime_start_ms(client):
    epoch = classic_epoch(client)
    return epoch[1] if epoch else None


async def require_flat(client):
    positions = await client.get_position()
    account = await client.get_um_account_positions()
    if not isinstance(positions, list) or not positions or not isinstance(account, list):
        raise RuntimeError("经典持仓快照不完整，无法初始化运行状态")
    for row in positions + account:
        qty = Decimal(str(row["positionAmt"]))
        if not row["symbol"] or not qty.is_finite() or qty != 0 or row.get("positionSide") != "BOTH":
            raise RuntimeError("经典账户尚有敞口或持仓模式不符，无法初始化运行状态")
    if await client.get_open_orders() != [] or await client.get_open_algo_orders() != []:
        raise RuntimeError("经典账户仍有挂单，无法初始化运行状态")


async def require_signed_classic_flat(client):
    """V3 只返回有持仓或挂单的标的；独立签名账户快照须显示正权益和零仓。"""
    from shared.binance_api import _position_rows
    from shared.accounting.equity_bridge import money
    risk = await client._request("GET", "/fapi/v3/positionRisk", signed=True)
    wallet = await client.get_account_info()
    if not isinstance(wallet, dict) or money(wallet["totalMarginBalance"]) <= 0:
        raise RuntimeError("经典实盘账户缺少正权益")
    for row in _position_rows(risk) + _position_rows(wallet.get("positions")):
        if row.get("positionSide") != "BOTH" or money(row["positionAmt"]) != 0:
            raise RuntimeError("经典账户尚有敞口或持仓模式不符，无法初始化运行状态")
    if await client.get_open_orders() != [] or await client.get_open_algo_orders() != []:
        raise RuntimeError("经典账户仍有挂单，无法初始化运行状态")


def matches_marker(row, account, cutoff, recovery_pin=None):
    if row is None:
        return False
    data = row['state_data']
    data = json.loads(data) if isinstance(data, str) else data
    if (data.get("account") != account or data.get("cutover_ms") != cutoff
            or data.get("version") != 1 or data.get("strategies") != list(STRATEGIES)
            or data.get("trading_recovery_sha256") != recovery_pin):
        raise ValueError("切换运行状态标记与账户、策略、切点或交易凭据不符")
    return True


def marker_key(account, cutoff):
    return f"classic:{cutoff}:{hashlib.sha256(account.encode()).hexdigest()[:32]}"


async def require_runtime_marker(client, db, *, recovery_pin=None):
    """每个主账户交易写入者启动时只读取标记；重启不触及正在运行的新仓。"""
    epoch = classic_epoch(client)
    if epoch is not None:
        account, cutoff = epoch
        row = await db.fetch_one(_READ, _STATE_OWNER, marker_key(account, cutoff))
        if not matches_marker(row, account, cutoff, recovery_pin):
            raise RuntimeError("切点运行状态尚未就绪，请在交易冻结时执行 scripts.initialize_classic_runtime")


async def initialize_classic_runtime(client, db):
    """冻结开仓期间核验同一账户的启动凭据，一次性处理三套运行缓存。"""
    epoch = classic_epoch(client)
    if epoch is None:
        return
    account, cutoff = epoch
    key = marker_key(account, cutoff)
    recovery = trading_recovery_configured()
    if not recovery and matches_marker(await db.fetch_one(_READ, _STATE_OWNER, key), account, cutoff):
        return
    async with db.pool.acquire() as conn, conn.transaction():
        # 其他策略重启需等待本次原子归档完成，避免一套策略先行开仓。
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"classic-runtime:{account}")
        pin = None
        if recovery:
            pin = await verify_trading_recovery(client, conn.fetch, account, cutoff)
            await check_pm_claims(conn.fetchrow, account, cutoff)
        if matches_marker(await conn.fetchrow(_READ, _STATE_OWNER, key), account, cutoff, pin):
            return
        if recovery:
            await require_signed_classic_flat(client)
            anchors = []
        else:
            await require_flat(client)
            anchors = await conn.fetch("""SELECT observation,evidence_ref,exchange_uid_sha256
                FROM trading.account_mode_equity WHERE account_id=$1 AND cutover_ms=$2 AND snapshot_date IS NULL
                AND open_positions=0""", account, cutoff)
            if ({r["observation"] for r in anchors} != {"pm_final", "classic_open"}
                    or any(not r["evidence_ref"] or not r["exchange_uid_sha256"] for r in anchors)
                    or len({r["exchange_uid_sha256"] for r in anchors}) != 1):
                raise RuntimeError("切点两侧缺少同一账户的空仓证明")
        names = list(STRATEGIES)
        newer = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM trading.trade_records
            WHERE account_id=$1 AND strategy=ANY($3::text[]) AND (order_id IS NOT NULL OR algo_id IS NOT NULL)
              AND executed_at >= timezone('Asia/Shanghai', to_timestamp($2::double precision/1000)))
            OR EXISTS(SELECT 1 FROM new_coin.short_positions WHERE status='open'
              AND (opened_at IS NULL OR opened_at >= timezone('UTC', to_timestamp($2::double precision/1000))))
            OR EXISTS(SELECT 1 FROM btc_eth.condition_orders WHERE strategy_name=ANY($4::text[])
              AND account_id=$1 AND status='OPEN'
              AND created_at >= to_timestamp($2::double precision/1000)::timestamp)
            OR EXISTS(SELECT 1 FROM public.condition_orders WHERE strategy_name=ANY($4::text[])
              AND account_id=$1 AND status='OPEN'
              AND created_at >= to_timestamp($2::double precision/1000)::timestamp)""", account, cutoff, names, names + ["hrs"])
        if newer:
            raise RuntimeError("存在切点后交易或无法归入旧账户的运行记录，须核对后再初始化")
        archive = {}
        archive["states"] = await conn.fetchval("""SELECT coalesce(jsonb_agg(to_jsonb(s)),'[]')::text
            FROM public.strategy_states s WHERE strategy_name=ANY($1::text[])
              AND (state_key='main' OR state_key LIKE 'position:%')""", names)
        archive["positions"] = await conn.fetchval("""SELECT coalesce(jsonb_agg(to_jsonb(p)),'[]')::text
            FROM trading.strategy_open_positions p WHERE strategy_id=ANY($1::text[])""", names)
        archive["short_positions"] = await conn.fetchval("""SELECT coalesce(jsonb_agg(to_jsonb(p)),'[]')::text
            FROM new_coin.short_positions p WHERE status='open'""")
        for schema, archive_key in (("public", "condition_orders"), ("btc_eth", "strategy_condition_orders")):
            table = f"{schema}.condition_orders"
            archive[archive_key] = await conn.fetchval(f"""SELECT coalesce(jsonb_agg(to_jsonb(o)),'[]')::text
                FROM {table} o WHERE strategy_name=ANY($1::text[]) AND status='OPEN'
                AND account_id=$2""", names + ["hrs"], account)
            await conn.execute(f"""UPDATE {table} SET status='CUTOVER_RETIRED',updated_at=now()
                WHERE strategy_name=ANY($1::text[]) AND status='OPEN' AND account_id=$2""",
                               names + ["hrs"], account)
        await conn.execute("""UPDATE public.strategy_states SET state_data=jsonb_set(state_data,'{positions}','{}'),
            updated_at=now() WHERE strategy_name=ANY($1::text[]) AND state_key='main'""", names)
        # 旧单币键的存在本身会触发激进版按入场单号恢复。完整原值保留在账户归档。
        await conn.execute("""DELETE FROM public.strategy_states WHERE strategy_name=ANY($1::text[])
            AND state_key LIKE 'position:%'""", names)
        await conn.execute("DELETE FROM trading.strategy_open_positions WHERE strategy_id=ANY($1::text[])", names)
        # 已核实切点为空仓，只结束运行标识，不填写交易价格、成交时间或收益。
        await conn.execute("""UPDATE new_coin.short_positions SET status='closed',
            metadata=coalesce(metadata,'{}'::jsonb)||jsonb_build_object('runtime_cutover_ms',$1::bigint)
            WHERE status='open'""", cutoff)
        payload = dict(account=account, cutover_ms=cutoff, strategies=names, version=1,
                       anchors=[dict(row) for row in anchors], archive={k: json.loads(v) for k, v in archive.items()})
        if pin is not None:
            payload["trading_recovery_sha256"] = pin
        await conn.execute("""INSERT INTO public.strategy_states(strategy_name,state_key,state_data)
            VALUES($1,$2,$3::jsonb)""", _STATE_OWNER, key, json.dumps(payload))
