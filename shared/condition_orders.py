"""
条件单持久化记录模块

各策略创建条件单时调用 record_condition_order() 写入数据库，
orphan_cleanup 根据此表判断并清理孤儿条件单。

表结构：
- id: 自增主键
- strategy_name: 策略名称 (btc_eth/hrs/new_coin)
- symbol: 交易对 (BTCUSDT)
- algo_id: 条件单 ID（HRS/新币策略用，对应 Binance algoId）
- order_id: 普通订单 ID（BTC_ETH 策略用，部分场景可能用到）
- order_type: 类型 (STOP_LOSS/TAKE_PROFIT/ENTRY)
- status: 状态 (OPEN/CANCELED/EXECUTED)
- created_at/updated_at: 时间戳
"""

import os
import structlog

from shared.accounting.account_cutover import account_cutover_ms
from shared.binance_account_mode import configured_pm

logger = structlog.get_logger()

# 建表 DDL（幂等）
_CREATE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS btc_eth.condition_orders (
    id SERIAL PRIMARY KEY,
    strategy_name VARCHAR(50) NOT NULL,
    account_id TEXT,
    symbol VARCHAR(20) NOT NULL,
    algo_id BIGINT,
    order_id BIGINT,
    order_type VARCHAR(20) NOT NULL,
    tp_level SMALLINT,
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
)
"""

# 索引 DDL
_CREATE_INDEX_STATUS_DDL = """
CREATE INDEX IF NOT EXISTS idx_co_status ON btc_eth.condition_orders(status)
"""

_ADD_TP_LEVEL_DDL = "ALTER TABLE btc_eth.condition_orders ADD COLUMN IF NOT EXISTS tp_level SMALLINT"
_ADD_ACCOUNT_ID_DDL = "ALTER TABLE btc_eth.condition_orders ADD COLUMN IF NOT EXISTS account_id TEXT"

_CREATE_INDEX_STRATEGY_DDL = """
CREATE INDEX IF NOT EXISTS idx_co_strategy ON btc_eth.condition_orders(strategy_name)
"""

# B5 修复：联合索引，孤儿单清理高频查询 (strategy_name, status) 组合
_CREATE_INDEX_STRATEGY_STATUS_DDL = """
CREATE INDEX IF NOT EXISTS idx_co_strategy_status ON btc_eth.condition_orders(strategy_name, status)
"""

# 唯一约束：避免同一策略重复记录同一条件单
# 仅对 OPEN 状态的条件单做唯一约束，CANCELED/EXECUTED 允许重复
_CREATE_UNIQUE_ALGO_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_co_scoped_algo ON btc_eth.condition_orders(account_id, strategy_name, symbol, algo_id) NULLS NOT DISTINCT WHERE algo_id IS NOT NULL AND status = 'OPEN'
"""

_CREATE_UNIQUE_ORDER_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_co_scoped_order ON btc_eth.condition_orders(account_id, strategy_name, symbol, order_id) NULLS NOT DISTINCT WHERE order_id IS NOT NULL AND status = 'OPEN'
"""


async def ensure_table(db):
    """创建 condition_orders 表及相关索引（幂等操作）"""
    try:
        await db.execute_ddl(_CREATE_TABLE_DDL)
        await db.execute_ddl(_ADD_TP_LEVEL_DDL)
        await db.execute_ddl(_ADD_ACCOUNT_ID_DDL)
        await db.execute_ddl(_CREATE_INDEX_STATUS_DDL)
        await db.execute_ddl(_CREATE_INDEX_STRATEGY_DDL)
        # B5 修复：高频查询联合索引
        await db.execute_ddl(_CREATE_INDEX_STRATEGY_STATUS_DDL)
        await db.execute_ddl(_CREATE_UNIQUE_ALGO_DDL)
        await db.execute_ddl(_CREATE_UNIQUE_ORDER_DDL)
        logger.info("condition_orders 表已确保存在")
    except Exception as e:
        logger.error("创建 condition_orders 表失败", error=str(e))
        raise


async def record_condition_order(db, strategy_name, symbol, algo_id=None, order_id=None, order_type="STOP_LOSS", tp_level=None, account_id=None):
    """
    记录条件单到数据库，状态为 OPEN

    通过 strategy_name + algo_id 或 strategy_name + order_id 做去重检查，
    避免同一条件单被重复记录。

    Args:
        db: DatabaseManager 实例
        strategy_name: 策略名称 (btc_eth/hrs/new_coin)
        symbol: 交易对 (BTCUSDT)
        algo_id: 条件单 ID（Binance algoId，HRS/新币策略用）
        order_id: 普通订单 ID（部分场景备用）
        order_type: 类型 (STOP_LOSS/TAKE_PROFIT/ENTRY)
    """
    try:
        account_id = (account_id or os.environ.get("TRADE_ACCOUNT_ID") or "").strip() or None
        if tp_level is not None and (order_type != 'TAKE_PROFIT' or tp_level not in (1, 2)):
            raise ValueError('tp_level 只能用于 TAKE_PROFIT，值为 1 或 2')
        if algo_id is not None:
            # 条件单：使用 INSERT ON CONFLICT 避免 SELECT+INSERT 竞态条件
            await db.execute(
                """
                INSERT INTO btc_eth.condition_orders (strategy_name, symbol, algo_id, order_type, tp_level, account_id, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'OPEN')
                ON CONFLICT DO NOTHING
                """,
                strategy_name, symbol, algo_id, order_type, tp_level, account_id
            )
            logger.debug("条件单已记录", strategy=strategy_name, symbol=symbol, algo_id=algo_id)
        elif order_id is not None:
            # 普通订单：使用 INSERT ON CONFLICT 避免 SELECT+INSERT 竞态条件
            await db.execute(
                """
                INSERT INTO btc_eth.condition_orders (strategy_name, symbol, order_id, order_type, tp_level, account_id, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'OPEN')
                ON CONFLICT DO NOTHING
                """,
                strategy_name, symbol, order_id, order_type, tp_level, account_id
            )
            logger.debug("普通订单已记录", strategy=strategy_name, symbol=symbol, order_id=order_id)
        else:
            logger.warning("record_condition_order 调用时未提供 algo_id 或 order_id",
                           strategy=strategy_name, symbol=symbol)
    except Exception as e:
        logger.warning("记录条件单失败", strategy=strategy_name, symbol=symbol, error=str(e))


def _account_epoch(account_id=None, pm=None):
    account = account_id or os.environ.get("TRADE_ACCOUNT_ID") or "default"
    pm = configured_pm() if pm is None else pm
    cutoff = account_cutover_ms(account)
    if not pm and cutoff is None:
        raise ValueError("经典账户条件单缺少切换时间")
    return account, cutoff, pm


def _epoch_sql(column, cutoff, pm):
    # condition_orders.created_at uses the database session clock; trade_records is Beijing local time.
    return (f"({cutoff}::bigint IS NULL OR (({column} < "
            f"to_timestamp({cutoff}::double precision / 1000)::timestamp) = {pm}::boolean))")


async def _mark_order(db, status, algo_id, order_id, strategy_name, symbol):
    if (algo_id is None) == (order_id is None):
        raise ValueError("条件单终态必须提供唯一标识")
    col, value = ("algo_id", algo_id) if algo_id is not None else ("order_id", order_id)
    account, cutoff, pm = _account_epoch()
    # Older callers without a role or symbol can update only a uniquely scoped row.
    sql = f"""
        UPDATE btc_eth.condition_orders SET status=$1, updated_at=NOW() WHERE status='OPEN' AND id=(
            SELECT min(id) FROM btc_eth.condition_orders WHERE {col}=$2 AND account_id=$3
            AND ($4::text IS NULL OR strategy_name=$4) AND ($5::text IS NULL OR symbol=$5)
            AND {_epoch_sql("created_at", "$6", "$7")} HAVING count(*)=1)
    """
    return await db.execute(sql, status, value, account, strategy_name, symbol, cutoff, pm)


async def mark_order_canceled(db, order_id=None, algo_id=None, *, strategy_name=None, symbol=None):
    return await _mark_order(db, "CANCELED", algo_id, order_id, strategy_name, symbol)


async def mark_order_executed(db, algo_id=None, order_id=None, *, strategy_name=None, symbol=None):
    return await _mark_order(db, "EXECUTED", algo_id, order_id, strategy_name, symbol)


async def mark_open_orders_canceled(db, strategy_name, symbol):
    """
    将指定策略在指定交易对上所有 OPEN 条件单批量标记为 CANCELED

    用于「已对交易所执行整币种撤单」后的数据库状态同步：
    批量撤单（cancel_all_algo_orders）成功、或逐个撤单全部成功后，交易所上该 symbol
    已无条件单，若 DB 仍保留 OPEN 行，会沉积成「假孤儿单」——既污染持仓保护单计数，
    又会被孤儿清理任务反复「取消」（Binance 返回 -2011 被当作取消成功），产生误导性告警。

    仅在交易所确已撤单成功时调用；调用方需自行保证「部分失败不误标」。

    Args:
        db: DatabaseManager 实例
        strategy_name: 策略名称（如 hrs）
        symbol: 交易对（如 LITEUSDT）

    Returns:
        str: 数据库执行结果（形如 "UPDATE 3"）；异常时返回空字符串
    """
    try:
        account, cutoff, pm = _account_epoch()
        result = await db.execute(
            "UPDATE btc_eth.condition_orders SET status='CANCELED', updated_at=NOW() "
            "WHERE strategy_name=$1 AND symbol=$2 AND status='OPEN' AND account_id=$3 AND "
            + _epoch_sql("created_at", "$4", "$5"), strategy_name, symbol, account, cutoff, pm,
        )
        logger.info(
            "条件单状态已同步为已取消",
            strategy=strategy_name, symbol=symbol, result=result,
        )
        return result
    except Exception as e:
        logger.warning(
            "同步条件单取消状态失败（不阻断主流程）",
            strategy=strategy_name, symbol=symbol, error=str(e),
        )
        return ""


async def get_open_orders(db, strategy_name=None, *, account_id=None):
    """
    查询所有或指定策略的 OPEN 状态条件单

    Args:
        db: DatabaseManager 实例
        strategy_name: 可选，策略名称，不传则查询所有策略

    Returns:
        list[dict]: 条件单记录列表
    """
    account, cutoff, pm = _account_epoch(account_id)
    return await db.fetch_all(
        "SELECT * FROM btc_eth.condition_orders WHERE status='OPEN' AND account_id=$1 "
        "AND ($2::text IS NULL OR strategy_name=$2) AND " + _epoch_sql("created_at", "$3", "$4")
        + " ORDER BY created_at", account, strategy_name, cutoff, pm,
    )


async def strategy_owned_algos(client, *, symbol=None, managed_symbols=None, orders=None, owner=None):
    """用当前账户签名清单和同一账户模式的策略创建回执核对条件单。"""
    owner = owner or client.trade_logger
    if not owner or not owner.account_id or not owner.strategy_name:
        raise ValueError("无法确认策略条件单归属")
    pm = client.use_unified_account is not False
    _, cutoff, _ = _account_epoch(owner.account_id, pm)
    if orders is None:
        orders = await client.get_open_algo_orders(symbol)
    if not isinstance(orders, list):
        raise ValueError("交易所条件单列表无效")
    live = {}
    for order in orders:
        sym = order["symbol"]
        if symbol and sym != symbol:
            raise ValueError("条件单币种不符")
        if managed_symbols is not None and sym not in managed_symbols:
            continue
        key = (sym, str(order["algoId"]))
        if key in live or (cutoff is not None and (int(order["createTime"]) < cutoff) != pm):
            raise ValueError("条件单身份或交易所创建时间不符")
        live[key] = order
    if not live:
        return []
    # trade_records 是各策略共享的创建回执。其 executed_at 为北京时间无时区列。
    # condition_orders 只提供本策略角色与 TP 档位，不能单独证明交易所身份。
    rows = await owner.db.fetch_all(f"""
        SELECT tr.symbol, tr.algo_id, tr.strategy, tr.side, tr.executed_at AS created_at,
               co.order_type, co.tp_level
        FROM trading.trade_records tr
        LEFT JOIN btc_eth.condition_orders co
            ON co.account_id = tr.account_id AND co.symbol = tr.symbol
            AND co.strategy_name = tr.strategy AND co.algo_id::text = tr.algo_id
            AND {_epoch_sql("co.created_at", "$3", "$4")}
        WHERE tr.account_id = $1 AND tr.algo_id = ANY($2::text[])
            AND tr.order_type = 'CONDITIONAL_ORDER'
            AND ($3::bigint IS NULL OR ((tr.executed_at < timezone('Asia/Shanghai',
                to_timestamp($3::double precision / 1000))) = $4::boolean))
        ORDER BY tr.executed_at
    """, owner.account_id, [algo_id for _, algo_id in live], cutoff, pm)
    records = {}
    for row in rows:
        key = (row["symbol"], str(row["algo_id"]))
        if key in live:
            records.setdefault(key, []).append(row)
    owned = []
    for key, order in live.items():
        matches = records.get(key, [])
        if len(matches) != 1 or matches[0]["side"] != order["side"]:
            raise ValueError(f"{key[0]} 条件单归属不明或记录冲突")
        row = matches[0]
        if row["strategy"] != owner.strategy_name:
            continue
        order_type = order["orderType"]
        if order_type in ("STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"):
            types = ("STOP_LOSS", "TRAILING_STOP", "MANUAL_STOP")
        elif order_type in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            types = ("TAKE_PROFIT",)
        else:
            raise ValueError("交易所条件单类型无法核对")
        if row["order_type"] is not None and row["order_type"] not in types:
            raise ValueError("条件单角色与交易所类型冲突")
        owned.append({**order, **row, "algo_id": int(order["algoId"]), "order_type": row["order_type"] or types[0]})
    return sorted(owned, key=lambda row: row["created_at"])


# Compatibility for callers limited to the classic path.
classic_owned_algos = strategy_owned_algos


class IncompleteAlgoCancellation(RuntimeError):
    def __init__(self, symbol, cancelled, failed):
        self.result = {"complete": False, "total": len(cancelled) + len(failed),
                       "cancelled": len(cancelled), "failed": len(failed), "algo_ids": cancelled}
        super().__init__(f"{symbol} 有 {len(failed)} 笔条件单撤单结果需核对")


async def cancel_strategy_algos(client, symbol, *, tracked_ids=()):
    """逐笔撤销同一账户模式内已核实由当前策略创建的在挂 Algo。"""
    owned = await strategy_owned_algos(client, symbol=symbol)
    inactive = {int(value) for value in tracked_ids if value} - {row["algo_id"] for row in owned}
    for algo_id in inactive:
        if not await classic_algo_is_terminal(client, symbol, algo_id):
            raise ValueError("本地保护单已离开在挂列表，撤销或成交状态待核实")
    cancelled = []
    failed = []
    for row in owned:
        try:
            await client.cancel_algo_order(symbol, row["algo_id"])
        except Exception as exc:
            failed.append(exc)
        else:
            cancelled.append(row["algo_id"])
    if failed:
        raise IncompleteAlgoCancellation(symbol, cancelled, failed) from failed[0]
    return {"complete": True, "total": len(owned), "cancelled": len(cancelled),
            "failed": 0, "algo_ids": cancelled}


async def mark_owned_algo(client, symbol, algo_id, status):
    owner = client.trade_logger
    _, cutoff, pm = _account_epoch(owner.account_id, client.use_unified_account is not False)
    await owner.db.execute(
        "UPDATE btc_eth.condition_orders SET status=$7, updated_at=NOW() WHERE "
        "account_id=$1 AND strategy_name=$2 AND symbol=$3 AND algo_id=$4 AND status='OPEN' "
        "AND " + _epoch_sql("created_at", "$5", "$6"),
        owner.account_id, owner.strategy_name, symbol, int(algo_id), cutoff, pm, status,
    )


async def classic_algo_is_terminal(client, symbol, algo_id):
    """已取消/过期且从未触发，或真实成交已核实，才可清掉本地跟踪。"""
    previous = await client.get_algo_order(int(algo_id))
    if not await classic_owned_algos(client, symbol=symbol, orders=[previous]):
        return False
    if previous.get("algoStatus") in ("CANCELED", "EXPIRED", "REJECTED") and not int(previous.get("actualOrderId") or 0):
        await mark_owned_algo(client, symbol, algo_id, "CANCELED")
        return True
    return await confirm_classic_algo_fill(client, symbol, algo_id)


async def confirm_classic_algo_fill(client, symbol, algo_id, *, role=None, tp_level=None, return_quantity=False):
    """核对 Algo → 实际普通委托 → 完整逐笔成交，随后只标记本账户本策略的行。"""
    from .entry_fill_evidence import verified_fill

    algo = await client.get_algo_order(int(algo_id))
    if algo.get("algoStatus") not in ("TRIGGERED", "FINISHED"):
        return False
    rows = await classic_owned_algos(client, symbol=symbol, orders=[algo])
    if not rows or (role and rows[0]["order_type"] != role) or (tp_level and rows[0]["tp_level"] != tp_level):
        return False
    order_id = int(algo.get("actualOrderId") or 0)
    if order_id <= 0:
        return False
    order = await client.get_order(symbol, order_id)
    if order.get("status") != "FILLED":
        return False
    fills = await client.get_user_trades(symbol, order_id=str(order_id), limit=1000)
    quantity, _ = verified_fill({"symbol": symbol, "side": algo["side"], "order_id": order_id}, order, fills)
    await mark_owned_algo(client, symbol, algo_id, "EXECUTED")
    return quantity if return_quantity else True
