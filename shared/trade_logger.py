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
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        executed_at: Optional[datetime] = None,
        time_window: int = 300
    ) -> bool:
        """
        回写平仓盈亏到交易记录

        平仓后由策略层调用，将本笔平仓订单的已实现盈亏写入 trade_records.realized_pnl。
        支持两种匹配模式：
        - 模式一（order_id 精确匹配）：优先使用 order_id 精确匹配 trade_records 记录
        - 模式二（降级匹配）：当模式一失败或 order_id 为空时，按 (strategy, symbol, side, executed_at 范围) 匹配
        写入失败不影响主流程（异常被内部捕获）。

        Args:
            order_id: 币安订单ID（对应 trade_records.order_id），为空时直接尝试降级匹配
            realized_pnl: 已实现盈亏（USDT），盈利为正、亏损为负
            strategy: 策略名称，默认使用初始化时设置的策略名称
            symbol: 交易对（如 "BTCUSDT"），降级匹配时必填
            side: 平仓方向（BUY/SELL），降级匹配时必填，用于精确匹配平仓记录
            executed_at: 平仓成交时间，用于确定降级匹配的时间窗口
            time_window: 降级匹配的时间窗口（秒），默认前后各 300 秒（5 分钟）

        Returns:
            True 表示回写成功，False 表示未回写（订单不存在或失败）
        """
        try:
            strategy_name = strategy or self.strategy_name
            pnl_str = str(realized_pnl)

            # ---------- 模式一：order_id 精确匹配 ----------
            if order_id:
                # 模式一使用传入的 side，若未传入则默认 'BUY'（兼容旧调用方）
                match_side = side or 'BUY'
                result = await self.db.execute(
                    "UPDATE trading.trade_records "
                    "SET realized_pnl = $1 "
                    "WHERE order_id = $2 AND strategy = $3 AND side = $4",
                    pnl_str,
                    order_id,
                    strategy_name,
                    match_side
                )
                # 解析 "UPDATE N" 结果，判断是否更新成功
                rows_affected = self._parse_update_count(result)
                if rows_affected > 0:
                    logger.info(
                        "回写平仓盈亏成功",
                        match_mode="order_id",
                        order_id=order_id,
                        realized_pnl=pnl_str,
                        strategy=strategy_name,
                        side=match_side
                    )
                    return True

                logger.info(
                    "模式一（order_id匹配）未命中，准备降级匹配",
                    order_id=order_id,
                    strategy=strategy_name,
                    side=match_side
                )

            # ---------- 模式二：降级匹配（按 strategy + symbol + side + 时间范围） ----------
            if symbol and executed_at:
                start_time = executed_at - timedelta(seconds=time_window)
                end_time = executed_at + timedelta(seconds=time_window)

                # 降级匹配使用传入的 side，若未传入则默认 'BUY'（兼容旧调用方）
                match_side = side or 'BUY'

                result = await self.db.execute(
                    "UPDATE trading.trade_records "
                    "SET realized_pnl = $1 "
                    "WHERE id = ("
                    "    SELECT id FROM trading.trade_records"
                    "    WHERE strategy = $2 AND symbol = $3 AND side = $4"
                    "    AND executed_at BETWEEN $5 AND $6"
                    "    AND order_id IS NULL"
                    "    AND realized_pnl IS NULL"
                    "    ORDER BY executed_at DESC"
                    "    LIMIT 1"
                    ")",
                    pnl_str,
                    strategy_name,
                    symbol,
                    match_side,
                    start_time,
                    end_time
                )
                rows_affected = self._parse_update_count(result)
                if rows_affected > 0:
                    logger.info(
                        "回写平仓盈亏成功",
                        match_mode="fallback",
                        symbol=symbol,
                        side=match_side,
                        executed_at=executed_at.isoformat(),
                        time_window=time_window,
                        realized_pnl=pnl_str,
                        strategy=strategy_name
                    )
                    return True

                logger.warning(
                    "降级匹配未命中，无法回写平仓盈亏",
                    match_mode="fallback",
                    symbol=symbol,
                    side=match_side,
                    executed_at=executed_at.isoformat() if executed_at else None,
                    realized_pnl=pnl_str,
                    strategy=strategy_name
                )
            else:
                logger.warning(
                    "降级匹配参数不足，无法执行降级匹配",
                    symbol=symbol,
                    executed_at=executed_at.isoformat() if executed_at else None,
                    strategy=strategy_name
                )

            return False

        except Exception as e:
            logger.error(
                "回写平仓盈亏异常",
                order_id=order_id,
                strategy=strategy or self.strategy_name,
                realized_pnl=str(realized_pnl),
                error=str(e),
                exc_info=True
            )
            return False

    async def insert_pnl_summary(
        self,
        realized_pnl: Decimal,
        symbol: str,
        side: str,
        strategy: Optional[str] = None,
        executed_at: Optional[datetime] = None,
    ) -> bool:
        """
        插入一条 PnL 汇总记录（用于全部平仓场景）

        当条件单（TP1/TP2/止损）全部成交时，没有对应的 trade_records 可 UPDATE，
        直接 INSERT 一条汇总记录，不会触发模式二的降级匹配。

        Args:
            realized_pnl: 已实现盈亏（USDT），盈利为正、亏损为负
            symbol: 交易对（如 "BTCUSDT"）
            side: 平仓方向（BUY/SELL）
            strategy: 策略名称，默认使用初始化时设置的策略名称
            executed_at: 平仓成交时间，默认当前时间

        Returns:
            True 表示插入成功，False 表示插入失败
        """
        try:
            strategy_name = strategy or self.strategy_name
            pnl_str = str(realized_pnl)
            exec_time = executed_at or datetime.now(BEIJING_TZ).replace(tzinfo=None)

            await self.db.execute(
                "INSERT INTO trading.trade_records "
                "(strategy, symbol, order_id, side, order_type, quantity, price, "
                " commission, status, executed_at, realized_pnl) "
                "VALUES ($1, $2, '', $3, 'PNL_SUMMARY', 0, 0, 0, 'FILLED', $4, $5)",
                strategy_name,
                symbol,
                side,
                exec_time,
                pnl_str,
            )

            logger.info(
                "PnL汇总记录插入成功",
                strategy=strategy_name,
                symbol=symbol,
                side=side,
                realized_pnl=pnl_str,
            )
            return True

        except Exception as e:
            logger.warning(
                "PnL汇总记录插入失败",
                strategy=strategy or self.strategy_name,
                symbol=symbol,
                realized_pnl=str(realized_pnl),
                error=str(e),
            )
            return False

    @staticmethod
    def _parse_update_count(result: str) -> int:
        """
        解析 asyncpg execute 返回的 "UPDATE N" 字符串，提取影响行数

        Args:
            result: asyncpg 返回的命令标签字符串（如 "UPDATE 1", "UPDATE 0"）

        Returns:
            影响的行数，解析失败返回 0
        """
        if not isinstance(result, str):
            return 0
        try:
            # asyncpg execute 返回格式为 "TAG N"，如 "UPDATE 1"
            parts = result.split()
            if len(parts) == 2:
                return int(parts[1])
            return 0
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def calculate_pnl(
        direction: str,
        entry_price: Decimal,
        exit_price: Decimal,
        quantity: Decimal
    ) -> Decimal:
        """
        计算平仓盈亏（集中管理，避免各策略重复实现）

        Args:
            direction: 持仓方向（LONG/SHORT）
            entry_price: 入场价格
            exit_price: 出场价格
            quantity: 平仓数量

        Returns:
            已实现盈亏（USDT），盈利为正，亏损为负
        """
        if direction == 'LONG':
            return (exit_price - entry_price) * quantity
        elif direction == 'SHORT':
            return (entry_price - exit_price) * quantity
        else:
            raise ValueError(f"不支持的持仓方向: {direction}")

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