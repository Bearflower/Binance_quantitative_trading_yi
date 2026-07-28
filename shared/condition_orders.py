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

import structlog

logger = structlog.get_logger()

# 建表 DDL（幂等）
_CREATE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS condition_orders (
    id SERIAL PRIMARY KEY,
    strategy_name VARCHAR(50) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    algo_id BIGINT,
    order_id BIGINT,
    order_type VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
)
"""

# 索引 DDL
_CREATE_INDEX_STATUS_DDL = """
CREATE INDEX IF NOT EXISTS idx_co_status ON condition_orders(status)
"""

_CREATE_INDEX_STRATEGY_DDL = """
CREATE INDEX IF NOT EXISTS idx_co_strategy ON condition_orders(strategy_name)
"""

# 唯一约束：避免同一策略重复记录同一条件单
# 仅对 OPEN 状态的条件单做唯一约束，CANCELED/EXECUTED 允许重复
_CREATE_UNIQUE_ALGO_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_co_algo_unique ON condition_orders(strategy_name, algo_id) WHERE algo_id IS NOT NULL AND status = 'OPEN'
"""

_CREATE_UNIQUE_ORDER_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_co_order_unique ON condition_orders(strategy_name, order_id) WHERE order_id IS NOT NULL AND status = 'OPEN'
"""


async def ensure_table(db):
    """创建 condition_orders 表及相关索引（幂等操作）"""
    try:
        await db.execute_ddl(_CREATE_TABLE_DDL)
        await db.execute_ddl(_CREATE_INDEX_STATUS_DDL)
        await db.execute_ddl(_CREATE_INDEX_STRATEGY_DDL)
        await db.execute_ddl(_CREATE_UNIQUE_ALGO_DDL)
        await db.execute_ddl(_CREATE_UNIQUE_ORDER_DDL)
        logger.info("condition_orders 表已确保存在")
    except Exception as e:
        logger.error("创建 condition_orders 表失败", error=str(e))
        raise


async def record_condition_order(db, strategy_name, symbol, algo_id=None, order_id=None, order_type="STOP_LOSS"):
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
        if algo_id is not None:
            # 条件单：使用 INSERT ON CONFLICT 避免 SELECT+INSERT 竞态条件
            await db.execute(
                """
                INSERT INTO condition_orders (strategy_name, symbol, algo_id, order_type, status)
                VALUES ($1, $2, $3, $4, 'OPEN')
                ON CONFLICT (strategy_name, algo_id) WHERE algo_id IS NOT NULL AND status = 'OPEN' DO NOTHING
                """,
                strategy_name, symbol, algo_id, order_type
            )
            logger.debug("条件单已记录", strategy=strategy_name, symbol=symbol, algo_id=algo_id)
        elif order_id is not None:
            # 普通订单：使用 INSERT ON CONFLICT 避免 SELECT+INSERT 竞态条件
            await db.execute(
                """
                INSERT INTO condition_orders (strategy_name, symbol, order_id, order_type, status)
                VALUES ($1, $2, $3, $4, 'OPEN')
                ON CONFLICT (strategy_name, order_id) WHERE order_id IS NOT NULL AND status = 'OPEN' DO NOTHING
                """,
                strategy_name, symbol, order_id, order_type
            )
            logger.debug("普通订单已记录", strategy=strategy_name, symbol=symbol, order_id=order_id)
        else:
            logger.warning("record_condition_order 调用时未提供 algo_id 或 order_id",
                           strategy=strategy_name, symbol=symbol)
    except Exception as e:
        logger.warning("记录条件单失败", strategy=strategy_name, symbol=symbol, error=str(e))


async def mark_order_canceled(db, order_id=None, algo_id=None):
    """
    标记条件单为已取消，status=CANCELED

    Args:
        db: DatabaseManager 实例
        order_id: 普通订单 ID
        algo_id: 条件单 ID
    """
    try:
        if algo_id is not None:
            await db.execute(
                "UPDATE condition_orders SET status='CANCELED', updated_at=NOW() WHERE algo_id=$1 AND status='OPEN'",
                algo_id
            )
        elif order_id is not None:
            await db.execute(
                "UPDATE condition_orders SET status='CANCELED', updated_at=NOW() WHERE order_id=$1 AND status='OPEN'",
                order_id
            )
    except Exception as e:
        logger.warning("标记条件单已取消失败", error=str(e))


async def mark_order_executed(db, algo_id=None, order_id=None):
    """
    标记条件单为已执行，status=EXECUTED

    Args:
        db: DatabaseManager 实例
        algo_id: 条件单 ID
        order_id: 普通订单 ID
    """
    try:
        if algo_id is not None:
            await db.execute(
                "UPDATE condition_orders SET status='EXECUTED', updated_at=NOW() WHERE algo_id=$1 AND status='OPEN'",
                algo_id
            )
        elif order_id is not None:
            await db.execute(
                "UPDATE condition_orders SET status='EXECUTED', updated_at=NOW() WHERE order_id=$1 AND status='OPEN'",
                order_id
            )
    except Exception as e:
        logger.warning("标记条件单已执行失败", error=str(e))


async def get_open_orders(db, strategy_name=None):
    """
    查询所有或指定策略的 OPEN 状态条件单

    Args:
        db: DatabaseManager 实例
        strategy_name: 可选，策略名称，不传则查询所有策略

    Returns:
        list[dict]: 条件单记录列表
    """
    if strategy_name:
        return await db.fetch_all(
            "SELECT * FROM condition_orders WHERE status='OPEN' AND strategy_name=$1 ORDER BY created_at",
            strategy_name
        )
    return await db.fetch_all(
        "SELECT * FROM condition_orders WHERE status='OPEN' ORDER BY strategy_name, created_at"
    )