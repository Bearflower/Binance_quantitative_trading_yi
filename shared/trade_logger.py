"""
统一交易记录器
在 BinanceClient 层面 hook，自动记录所有策略的每笔成交订单。
新策略无需任何额外的日志代码，只要通过 BinanceClient 下单即可。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Dict, List
import structlog

from .database import DatabaseManager

logger = structlog.get_logger()

# 北京时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))


@dataclass
class TradeRecord:
    """
    交易记录数据类

    Attributes:
        strategy: 策略名称（如 "MTPCS策略"、"网格交易策略"、"新币做空策略"）
        symbol: 交易对（如 "BTCUSDT"）
        order_id: 币安订单ID
        side: 买卖方向（BUY/SELL）
        order_type: 订单类型（MARKET/LIMIT/STOP_MARKET 等）
        quantity: 成交数量
        price: 成交均价
        commission: 手续费
        status: 订单状态
        executed_at: 成交时间（北京时间，无时区）
    """
    strategy: str
    symbol: str
    order_id: Optional[str]
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    status: str
    executed_at: datetime


class TradeLogger:
    """
    统一交易记录器

    在 BinanceClient 层面 hook，自动记录所有策略的每笔成交订单。
    新策略无需任何额外的日志代码，只要通过 BinanceClient 下单即可。

    使用方式:
        db = DatabaseManager(...)
        await db.connect()

        trade_logger = TradeLogger(db, "MTPCS策略")
        await trade_logger.ensure_table_exists()

        client = BinanceClient(...)
        client.set_trade_logger(trade_logger)
    """

    # 建表 DDL
    _CREATE_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS trading.trade_records (
        id SERIAL PRIMARY KEY,
        strategy VARCHAR(50) NOT NULL,
        symbol VARCHAR(20) NOT NULL,
        order_id VARCHAR(100),
        side VARCHAR(10) NOT NULL,
        order_type VARCHAR(20) NOT NULL,
        quantity DECIMAL(20,8) NOT NULL DEFAULT 0,
        price DECIMAL(20,8) NOT NULL DEFAULT 0,
        commission DECIMAL(20,8) NOT NULL DEFAULT 0,
        status VARCHAR(20) NOT NULL DEFAULT 'NEW',
        executed_at TIMESTAMP NOT NULL DEFAULT NOW(),
        realized_pnl DECIMAL(20,8)
    )
"""

    _CREATE_INDEX_1_DDL = """
    CREATE INDEX IF NOT EXISTS idx_trade_records_strategy_date 
        ON trading.trade_records(strategy, executed_at)
    """

    _CREATE_INDEX_2_DDL = """
    CREATE INDEX IF NOT EXISTS idx_trade_records_date 
        ON trading.trade_records(executed_at)
    """

    # 增量添加 realized_pnl 列（兼容已存在的生产表）
    _ALTER_ADD_REALIZED_PNL_DDL = """
    ALTER TABLE trading.trade_records 
        ADD COLUMN IF NOT EXISTS realized_pnl DECIMAL(20,8)
    """

    def __init__(self, db_manager: DatabaseManager, strategy_name: str):
        """
        初始化交易记录器

        Args:
            db_manager: 数据库管理器实例（需已建立连接）
            strategy_name: 策略名称（如 "MTPCS策略"、"网格交易策略"、"新币做空策略"）
        """
        if not strategy_name or not strategy_name.strip():
            raise ValueError("策略名称不能为空")

        self.db = db_manager
        self.strategy_name = strategy_name.strip()

        logger.info(
            "交易记录器初始化",
            strategy=self.strategy_name
        )

    async def ensure_table_exists(self) -> None:
        """
        确保交易记录表存在（自动建表）

        使用 execute_ddl 绕过 SQL 安全校验，仅用于初始化阶段。
        已在生产环境运行的策略可多次安全调用（IF NOT EXISTS）。
        """
        try:
            await self.db.execute_ddl(self._CREATE_TABLE_DDL)
            # 增量为已存在的生产表补齐 realized_pnl 列
            await self.db.execute_ddl(self._ALTER_ADD_REALIZED_PNL_DDL)
            await self.db.execute_ddl(self._CREATE_INDEX_1_DDL)
            await self.db.execute_ddl(self._CREATE_INDEX_2_DDL)
            logger.info(
                "交易记录表已就绪",
                schema="trading",
                table="trade_records"
            )
        except Exception as e:
            logger.error(
                "交易记录表创建失败",
                error=str(e),
                exc_info=True
            )
            raise

    async def log_order(
        self,
        order_result: Dict,
        symbol: str,
        side: str,
        order_type: str
    ) -> bool:
        """
        记录下单结果（记录所有状态订单，不限 FILLED）
        写入失败不影响正常交易流程（异常被内部捕获）。

        Args:
            order_result: 币安API返回的订单结果字典。
                          示例字段: orderId, avgPrice, executedQty, commission, status
            symbol: 交易对（如 "BTCUSDT"）
            side: 买卖方向（BUY/SELL）
            order_type: 订单类型（MARKET/LIMIT/STOP_MARKET 等）

        Returns:
            True 表示写入成功，False 表示未写入（跳过或失败）
        """
        try:
            status = order_result.get("status", "NEW")

            # 提取字段并转换类型
            order_id = str(order_result.get("orderId", "")) if order_result.get("orderId") else None
            avg_price = Decimal(str(order_result.get("avgPrice", "0")))
            executed_qty = Decimal(str(order_result.get("executedQty", "0")))

            # 手续费可能为 None
            commission_raw = order_result.get("commission")
            commission = Decimal(str(commission_raw)) if commission_raw else Decimal("0")

            # 成交时间使用当前北京时间（数据库列无时区，含义是 UTC+8）
            executed_at = datetime.now(BEIJING_TZ).replace(tzinfo=None)

            await self.db.execute(
                "INSERT INTO trading.trade_records "
                "(strategy, symbol, order_id, side, order_type, quantity, price, commission, status, executed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                self.strategy_name,
                symbol,
                order_id,
                side,
                order_type,
                str(executed_qty),
                str(avg_price),
                str(commission),
                status,
                executed_at
            )

            logger.info(
                "交易记录已写入",
                strategy=self.strategy_name,
                symbol=symbol,
                order_id=order_id,
                side=side,
                order_type=order_type,
                quantity=str(executed_qty),
                price=str(avg_price),
                commission=str(commission)
            )
            return True

        except Exception as e:
            logger.error(
                "交易记录写入失败",
                strategy=self.strategy_name,
                symbol=symbol,
                error=str(e),
                exc_info=True
            )
            return False

    async def update_realized_pnl(
        self,
        order_id: str,
        realized_pnl: Decimal,
        strategy: Optional[str] = None
    ) -> bool:
        """
        回写平仓盈亏到交易记录

        平仓后由策略层调用，将本笔平仓订单的已实现盈亏写入 trade_records.realized_pnl。
        写入失败不影响主流程（异常被内部捕获）。

        Args:
            order_id: 币安订单ID（必填，对应 trade_records.order_id）
            realized_pnl: 已实现盈亏（USDT），盈利为正、亏损为负
            strategy: 策略名称，默认使用初始化时设置的策略名称

        Returns:
            True 表示回写成功，False 表示未回写（订单不存在或失败）
        """
        try:
            if not order_id:
                logger.warning("回写平仓盈亏失败：order_id 为空")
                return False

            strategy_name = strategy or self.strategy_name
            result = await self.db.execute(
                "UPDATE trading.trade_records "
                "SET realized_pnl = $1 "
                "WHERE order_id = $2 AND strategy = $3 AND side = 'BUY'",
                str(realized_pnl),
                order_id,
                strategy_name
            )

            logger.info(
                "回写平仓盈亏成功",
                order_id=order_id,
                realized_pnl=str(realized_pnl),
                strategy=strategy_name
            )
            return True

        except Exception as e:
            logger.error(
                "回写平仓盈亏失败",
                order_id=order_id,
                strategy=strategy_name,
                error=str(e),
                exc_info=True
            )
            return False

    async def get_daily_stats(
        self,
        date: datetime
    ) -> Dict[str, Dict]:
        """
        获取指定日期的所有策略交易统计（供日报使用）

        查询 trading.trade_records 表，按策略分组统计。
        executed_at 列存储的是北京时间（无时区），查询时直接使用北京时间范围。

        Args:
            date: 要查询的日期。支持：
                  - 带时区：自动转为北京时间后取日期
                  - 不带时区：直接视为北京时间

        Returns:
            各策略统计字典，格式如：
            {
                "MTPCS策略": {
                    "detection_count": 0,
                    "valid_signals": 0,
                    "executed_count": 42,
                    "trade_count": 42,
                    "win_count": 0,
                    "loss_count": 0,
                    "total_count": 42,
                    "win_rate": 0.0
                },
                ...
            }
        """
        # 将输入 date 转为北京时间范围
        if date.tzinfo is not None:
            date_beijing = date.astimezone(BEIJING_TZ)
        else:
            date_beijing = date.replace(tzinfo=BEIJING_TZ)

        day_start = datetime.combine(
            date_beijing.date(),
            datetime.min.time(),
            tzinfo=BEIJING_TZ
        ).replace(tzinfo=None)

        day_end = datetime.combine(
            date_beijing.date(),
            datetime.max.time().replace(microsecond=0),
            tzinfo=BEIJING_TZ
        ).replace(tzinfo=None)

        logger.debug(
            "查询日报统计数据",
            date=date_beijing.strftime("%Y-%m-%d"),
            start=day_start.isoformat(),
            end=day_end.isoformat()
        )

        # 查询所有策略在该日期的成交笔数
        rows = await self.db.fetch_all(
            "SELECT strategy, COUNT(*) as trade_count "
            "FROM trading.trade_records "
            "WHERE executed_at BETWEEN $1 AND $2 "
            "GROUP BY strategy",
            day_start, day_end
        )

        result: Dict[str, Dict] = {}
        for row in rows:
            strategy = row["strategy"]
            trade_count = row["trade_count"]

            # 盈亏数据暂用占位值，待后续版本联合持仓盈亏表计算
            result[strategy] = {
                "detection_count": 0,
                "valid_signals": 0,
                "executed_count": trade_count,
                "trade_count": trade_count,
                "win_count": 0,
                "loss_count": 0,
                "total_count": trade_count,
                "win_rate": 0.0
            }

        # 确保三个主要策略都有条目（即使当天无交易）
        for default_strategy in ["MTPCS策略", "新币做空策略", "网格交易策略"]:
            if default_strategy not in result:
                result[default_strategy] = {
                    "detection_count": 0,
                    "valid_signals": 0,
                    "executed_count": 0,
                    "trade_count": 0,
                    "win_count": 0,
                    "loss_count": 0,
                    "total_count": 0,
                    "win_rate": 0.0
                }

        logger.info(
            "日报统计查询完成",
            date=date_beijing.strftime("%Y-%m-%d"),
            strategy_count=len(result)
        )
        return result