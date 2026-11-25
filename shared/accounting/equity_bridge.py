"""保存 PM 期末和经典合约期初证据，按权益计量范围分段计算收益。"""
from datetime import timedelta, timezone
from decimal import Decimal
import hashlib
import os

from .account_cutover import classic_cutover_ms

BEIJING = timezone(timedelta(hours=8))
_INSERT = """
INSERT INTO trading.account_mode_equity
(account_id,cutover_ms,observation,snapshot_date,observed_at,total_equity,available_balance,open_positions,evidence_ref,exchange_uid_sha256)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
ON CONFLICT (account_id,cutover_ms,observation,snapshot_date) DO NOTHING
"""
_SELECT = """
SELECT observed_at,total_equity,available_balance,open_positions,evidence_ref,exchange_uid_sha256
FROM trading.account_mode_equity
WHERE account_id=$1 AND cutover_ms=$2 AND observation=$3 AND snapshot_date IS NOT DISTINCT FROM $4
"""


def main_account():
    account = os.getenv("TRADE_ACCOUNT_ID", "default")
    if not account.strip():
        raise ValueError("TRADE_ACCOUNT_ID 不能为空")
    return account


def money(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("权益或余额不是有限数")
    return amount


async def signed_spot_uid_sha256(client, *, require_classic_trading=False):
    """复用当前签名凭据查询 Binance 账户 UID；只保存域分隔的摘要。"""
    from shared.readonly_client import ReadOnlyBinanceClient
    if client.testnet:
        raise ValueError("实盘切换锚点需要生产账户身份")
    async with ReadOnlyBinanceClient(client._api_key, client._api_secret, use_unified_account=False,
                                     base_url="https://api.binance.com", instance_id="account-identity") as spot:
        result = await spot._request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        if require_classic_trading:
            account = await spot._request("GET", "/sapi/v1/account/info", signed=True)
            permissions = await spot._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)
            if (not isinstance(account, dict) or account.get("isPortfolioMarginRetailEnabled") is not False
                    or not isinstance(permissions, dict) or permissions.get("enableFutures") is not True
                    or permissions.get("enableReading") is not True):
                raise ValueError("当前币安账户模式或 API Key 的经典合约读写权限不符")
    uid = result.get("uid") if isinstance(result, dict) else None
    if type(uid) is not int or uid <= 0:
        raise ValueError("签名现货账户响应缺少有效 Binance UID")
    return hashlib.sha256(f"binance:uid:{uid}".encode()).hexdigest()


async def save_observation(db, observation, observed_at, total, available, count, evidence=None, uid_hash=None):
    cutover = classic_cutover_ms()
    if cutover is None:
        raise ValueError("缺少 BINANCE_CLASSIC_CUTOVER_MS")
    if (observation == "pm_final") != (observed_at.timestamp() * 1000 < cutover):
        raise ValueError("采样时间与账户模式生效区间不符")
    if observation != "classic_daily" and (count != 0 or not evidence or not evidence.strip()):
        raise ValueError("切换锚点要求账户已清仓并提供核对凭据")
    day = observed_at.astimezone(BEIJING).date() if observation == "classic_daily" else None
    key = (main_account(), cutover, observation, day)
    await db.execute(_INSERT, *key, observed_at, money(total), money(available), count, evidence, uid_hash)
    row = await db.fetch_one(_SELECT, *key)
    if not row:
        raise RuntimeError("无法读回已保存的权益证据")
    if row["exchange_uid_sha256"] != uid_hash:
        raise ValueError("已持久化的锚点属于不同的 Binance UID")
    if observation in {"pm_final", "classic_open"} and (
        money(row["total_equity"]) != money(total) or money(row["available_balance"]) != money(available)
        or row["open_positions"] != count or row["evidence_ref"] != evidence
    ):
        raise ValueError("本次权益或凭据与已保存的切换锚点不一致")
    return dict(row)


async def read_anchors(db, cutover, expected_uid=None):
    rows = await db.fetch_all(
        "SELECT observation,total_equity,exchange_uid_sha256 FROM trading.account_mode_equity "
        "WHERE account_id=$1 AND cutover_ms=$2 AND snapshot_date IS NULL",
        main_account(), cutover,
    )
    anchors = {row["observation"]: money(row["total_equity"]) for row in rows}
    if set(anchors) != {"pm_final", "classic_open"} or min(anchors.values()) <= 0:
        raise ValueError("缺少有效的 PM 期末或经典期初权益")
    identities = {row.get("exchange_uid_sha256") for row in rows}
    if len(identities) != 1 or None in identities or (expected_uid is not None and expected_uid not in identities):
        raise ValueError("切换权益锚点与 Binance UID 不符")
    return anchors["pm_final"], anchors["classic_open"]


async def classic_daily_equity(db, cutover, start, end):
    return await db.fetch_all(
        "SELECT snapshot_date,total_equity, "
        "((observed_at AT TIME ZONE 'Asia/Shanghai')::time >= TIME '23:30' AND "
        "(observed_at AT TIME ZONE 'Asia/Shanghai')::time < TIME '23:31') AS scheduled_utc "
        "FROM trading.account_mode_equity "
        "WHERE account_id=$1 AND cutover_ms=$2 AND observation='classic_daily' "
        "AND snapshot_date >= $3 AND snapshot_date <= $4 ORDER BY snapshot_date",
        main_account(), cutover, start, end,
    )


async def classic_position_owner(db, position, cutover):
    """从已证实空仓锚点重放当前净仓；有缺口、错量或多策略冲突即保留未归属。"""
    from datetime import datetime
    from .account_cutover import FILL_SOURCES, coverage_watermark
    from .position_replay import replay_position
    from .report_reader import _CURSOR_SQL
    symbol = position["symbol"]
    if cutover is None or position.get("positionSide") != "BOTH" or not position.get("updateTime"):
        return None
    end_ms = int(position["updateTime"]) + 1
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    account = main_account()
    anchor = await db.fetch_one(
        "SELECT GREATEST((SELECT observed_at FROM trading.account_mode_equity "
        "WHERE account_id=$1 AND cutover_ms=$2 AND observation='classic_open' AND observed_at < $4),"
        "(SELECT MAX(flat_at) FROM trading.position_flat_baseline "
        "WHERE account_id=$1 AND symbol=$3 AND exchange_qty=0 AND flat_at >= to_timestamp($2::bigint/1000.0) "
        "AND flat_at < $4)) AS flat_at", account, cutover, symbol, end,
    )
    if not anchor or not anchor["flat_at"]:
        return None
    start = anchor["flat_at"]
    start_ms = int(start.timestamp() * 1000)
    cursors = await db.fetch_all(_CURSOR_SQL, account, start_ms, end_ms)
    if coverage_watermark(cursors, account, start_ms, end_ms, FILL_SOURCES, None, symbol) < end_ms:
        return None
    fills = await db.fetch_all(
        "SELECT account_id,symbol,trade_id,order_id,side,qty,trade_time_utc FROM trading.execution_fact_fill "
        "WHERE account_id=$1 AND symbol=$2 AND trade_time_utc >= $3 AND trade_time_utc < $4",
        account, symbol, start, end,
    )
    order_ids = [str(row["order_id"]) for row in fills if row["order_id"] is not None]
    rows = await db.fetch_all(
        "SELECT account_id,symbol,order_id,strategy FROM trading.trade_records "
        "WHERE account_id=$1 AND symbol=$2 AND order_id=ANY($3::text[]) "
        "AND executed_at >= timezone('Asia/Shanghai',to_timestamp($4::bigint / 1000.0))",
        account, symbol, order_ids, cutover,
    )
    linked = await db.fetch_all(
        "SELECT account_id,symbol,order_id,strategy FROM trading.order_strategy_evidence "
        "WHERE account_id=$1 AND symbol=$2 AND order_id=ANY($3::bigint[]) AND source_kind='classic_algo_link'",
        account, symbol, [int(i) for i in order_ids],
    )
    evidence = {}
    for row in (*rows, *linked):
        key = str(row["order_id"])
        if key in evidence and evidence[key]["strategy"] != row["strategy"]:
            return None
        evidence[key] = row
    result = replay_position(dict(account_id=account, symbol=symbol, flat_at=start, exchange_qty=0),
                             fills, evidence, end, position["positionAmt"])
    if result.verified and result.intervals and result.intervals[-1].end == end:
        return result.intervals[-1].strategy
    return None
