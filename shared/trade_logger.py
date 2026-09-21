"""
统一交易记录器
在 BinanceClient 层面 hook，自动记录所有策略的每笔成交订单。
新策略无需任何额外的日志代码，只要通过 BinanceClient 下单即可。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from os import environ
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

    # 平仓原因常量：止损触发（看板据此统计止损次数）
    CLOSE_REASON_STOP_LOSS = "STOP_LOSS"

    # 订单类型常量：条件单创建（用于区分真实成交记录与条件单创建记录）
    # B3 修复：条件单返回 algoId 而非 orderId，用此标记避免污染真实成交统计
    ORDER_TYPE_CONDITIONAL = "CONDITIONAL_ORDER"

    # 止损打标匹配真实平仓记录的窗口（单位：秒，可通过环境变量 STOP_LOSS_MATCH_WINDOW_SECONDS 覆盖）
    # 止损平仓后本地与交易所记录落库存在延迟，向前后各 10 分钟匹配刚发生的平仓记录，
    # 避免误匹配到历史其他平仓；为全局统一口径的参数（同仓同侧同窗口多笔场景已在 SQL 中以 LIMIT 1 兜住）。
    STOP_LOSS_MATCH_WINDOW_SECONDS = int(
        environ.get("STOP_LOSS_MATCH_WINDOW_SECONDS", "600")
    )

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

    # 增量添加 close_reason 列（标识平仓原因，用于看板统计止损次数）
    _ALTER_ADD_CLOSE_REASON_DDL = """
    ALTER TABLE trading.trade_records 
        ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20)
    """

    # 增量添加 algo_id 列（条件单 algoId，PM 账户条件单返回 algoId 而非 orderId）
    # B3 修复：让条件单创建记录不再是 order_id=NULL, price=0, qty=0 的脏数据
    _ALTER_ADD_ALGO_ID_DDL = """
    ALTER TABLE trading.trade_records 
        ADD COLUMN IF NOT EXISTS algo_id VARCHAR(100)
    """

    # 佣金回填：待回填订单查询 SQL
    # 最近 N 小时、commission=0、order_id 非空，(symbol, order_id) 去重
    _PENDING_RECORDS_SQL = """
    SELECT DISTINCT symbol, order_id
    FROM trading.trade_records
    WHERE executed_at >= $1 AND commission = 0 AND order_id IS NOT NULL
    """

    # 佣金回填：仅更新仍为 0 的行，天然幂等
    # （已回填非 0 的行不再命中待回填，不会重复覆盖）
    _UPDATE_COMMISSION_SQL = """
    UPDATE trading.trade_records
    SET commission = $1
    WHERE symbol = $2 AND order_id = $3 AND commission = 0
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
            # 增量补齐 close_reason 列（止损打标，看板统计止损次数用）
            await self.db.execute_ddl(self._ALTER_ADD_CLOSE_REASON_DDL)
            # B3 修复：增量补齐 algo_id 列（PM 条件单返回 algoId 而非 orderId）
            await self.db.execute_ddl(self._ALTER_ADD_ALGO_ID_DDL)
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

        B3 修复：PM 账户条件单返回 algoId 而非 orderId，
        正确识别并写入 algo_id 字段，标记为 CONDITIONAL_ORDER 类型，
        不再写 order_id=NULL, price=0, qty=0 的脏数据。

        Args:
            order_result: 币安API返回的订单结果字典。
                          示例字段:
                            真实成交单: orderId, avgPrice, executedQty, commission, status
                            PM 条件单: algoId（无 orderId/executedQty/price）
            symbol: 交易对（如 "BTCUSDT"）
            side: 买卖方向（BUY/SELL）
            order_type: 订单类型（MARKET/LIMIT/STOP_MARKET 等）

        Returns:
            True 表示写入成功，False 表示未写入（跳过或失败）
        """
        try:
            status = order_result.get("status", "NEW")

            # B3 修复：区分条件单（有 algoId 无 orderId）和真实成交单
            algo_id_raw = order_result.get("algoId")
            order_id_raw = order_result.get("orderId")

            is_conditional = bool(algo_id_raw) and not bool(order_id_raw)

            if is_conditional:
                # 条件单创建：写入 algo_id，标记为 CONDITIONAL_ORDER，quantity/price 保持 0
                # 条件单还未成交，没有真实成交数量和价格，不应写假数据
                algo_id = str(algo_id_raw)
                executed_at = datetime.now(BEIJING_TZ).replace(tzinfo=None)
                await self.db.execute(
                    "INSERT INTO trading.trade_records "
                    "(strategy, symbol, order_id, algo_id, side, order_type, "
                    " quantity, price, commission, status, executed_at) "
                    "VALUES ($1, $2, NULL, $3, $4, $5, 0, 0, 0, $6, $7)",
                    self.strategy_name,
                    symbol,
                    algo_id,
                    side,
                    self.ORDER_TYPE_CONDITIONAL,
                    "CREATED",
                    executed_at,
                )
                logger.info(
                    "条件单创建记录已写入（B3 修复）",
                    strategy=self.strategy_name,
                    symbol=symbol,
                    algo_id=algo_id,
                    original_order_type=order_type,
                    side=side,
                )
                return True

            # 真实成交单：正常处理
            order_id = str(order_id_raw) if order_id_raw else None
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
        close_reason: Optional[str] = None,
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
            close_reason: 平仓原因标记（如 STOP_LOSS/TAKE_PROFIT），
                          看板据此统计止损/止盈次数；None 表示未知/手动。
        """
        try:
            strategy_name = strategy or self.strategy_name
            pnl_str = str(realized_pnl)
            exec_time = executed_at or datetime.now(BEIJING_TZ).replace(tzinfo=None)

            await self.db.execute(
                "INSERT INTO trading.trade_records "
                "(strategy, symbol, order_id, side, order_type, quantity, price, "
                " commission, status, executed_at, realized_pnl, close_reason) "
                "VALUES ($1, $2, '', $3, 'PNL_SUMMARY', 0, 0, 0, 'FILLED', $4, $5, $6)",
                strategy_name,
                symbol,
                side,
                exec_time,
                pnl_str,
                close_reason,
            )

            logger.info(
                "PnL汇总记录插入成功",
                strategy=strategy_name,
                symbol=symbol,
                side=side,
                close_reason=close_reason,
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

    async def log_stop_loss(
        self,
        symbol: str,
        side: str,
        realized_pnl: Optional[Decimal] = None,
        strategy: Optional[str] = None,
        executed_at: Optional[datetime] = None,
    ) -> bool:
        """
        记录一次止损触发（打标 close_reason='STOP_LOSS'）。

        跨策略止损平仓的统一点：策略识别到"止损触发并平仓"时调用，
        仅落一条带止损标记的 PnL 记录，供看板统计近N天止损次数。
        （止损次数统计与止盈分开，便于风控查看近期止损频率。）

        Args:
            symbol: 交易对（如 "BTCUSDT"）
            side: 平仓方向（BUY/SELL）
            realized_pnl: 可选，该笔止损平仓的已实现盈亏；未知时传 None
            strategy: 策略名称，默认使用初始化时设置的策略名称
            executed_at: 平仓成交时间，默认当前时间

        Returns:
            True 表示写入成功，False 表示写入失败
        """
        # 优先给最近一条真实平仓记录打标记，避免与真实平仓记录重复计数
        # （止损平仓通常已有一条由 BinanceClient 记录 + update_realized_pnl 回写的记录）
        strategy_name = strategy or self.strategy_name

        if await self._mark_existing_close_record(
            strategy_name=strategy_name,
            symbol=symbol,
            side=side,
            realized_pnl=realized_pnl,
            executed_at=executed_at,
        ):
            return True

        # 兜底：无真实平仓记录可标记时，插入一条带标记的 PnL 汇总记录
        return await self.insert_pnl_summary(
            realized_pnl=realized_pnl if realized_pnl is not None else Decimal("0"),
            symbol=symbol,
            side=side,
            strategy=strategy,
            executed_at=executed_at,
            close_reason=self.CLOSE_REASON_STOP_LOSS,
        )

    async def _mark_existing_close_record(
        self,
        strategy_name: str,
        symbol: str,
        side: str,
        realized_pnl: Optional[Decimal],
        executed_at: Optional[datetime],
    ) -> bool:
        """给最近一条真实平仓记录打上 STOP_LOSS 标记。

        匹配规则：同策略同 symbol 同侧、非 PNL_SUMMARY 汇总记录、±10 分钟窗口内、
        close_reason 尚未标记；执行失败或未命中返回 False，由调用方降级为插入汇总记录。

        Args:
            strategy_name: 策略名称
            symbol: 交易对
            side: 平仓方向（BUY/SELL）
            realized_pnl: 该笔止损平仓的已实现盈亏；None 表示不覆盖原有盈亏
            executed_at: 平仓成交时间，默认当前时间

        Returns:
            True 表示成功给真实平仓记录打标
        """
        exec_time = executed_at or datetime.now(BEIJING_TZ).replace(tzinfo=None)
        window = self.STOP_LOSS_MATCH_WINDOW_SECONDS
        try:
            result = await self.db.execute(
                "UPDATE trading.trade_records "
                "SET close_reason = $1"
                + (", realized_pnl = $2" if realized_pnl is not None else "")
                + " WHERE id = ("
                "    SELECT id FROM trading.trade_records"
                "    WHERE strategy = $3 AND symbol = $4 AND side = $5"
                "    AND order_type <> 'PNL_SUMMARY'"
                "    AND executed_at BETWEEN $6 AND $7"
                "    AND (close_reason IS NULL OR close_reason = '')"
                "    ORDER BY executed_at DESC"
                "    LIMIT 1"
                ")",
                self.CLOSE_REASON_STOP_LOSS,
                *( (str(realized_pnl),) if realized_pnl is not None else () ),
                strategy_name,
                symbol,
                side,
                exec_time - timedelta(seconds=window),
                exec_time + timedelta(seconds=window),
            )
            if self._parse_update_count(result) > 0:
                logger.info(
                    "止损打标成功（回写至真实平仓记录）",
                    strategy=strategy_name,
                    symbol=symbol,
                    side=side,
                )
                return True
        except Exception as e:
            logger.warning(
                "止损打标（UPDATE）执行异常，降级为插入标记记录",
                strategy=strategy_name,
                symbol=symbol,
                error=str(e)[:120],
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

    async def reconcile_commissions(
        self,
        binance_client,
        lookback_hours: int = 24
    ) -> Dict:
        """事后回填 trade_records 的真实手续费（commission）

        背景：币安合约/PM 账户下单返回结果不含 commission 字段（佣金只在
        userTrades 成交明细中返回），因此 trade_records.commission 落库恒为 0。
        本方法定时查询 userTrades，把真实佣金写回，供任何宿主复用。

        流程：
          1) 挑出最近 lookback_hours 内 commission=0 且 order_id 非空的记录，
             按 (symbol, order_id) 去重；
          2) 逐个 order 调 binance_client.get_user_trades 拉取该订单的全部分笔成交，
             累加真实佣金；
          3) 汇总 UPDATE 回填（仅更新仍为 0 的行，幂等）。
        任一步骤失败仅记日志，不抛出异常，保证不影响调度主流程。

        Args:
            binance_client: BinanceClient 实例（需具备 get_user_trades 方法）
            lookback_hours: 回填时间窗口（小时），仅处理最近 N 小时的待回填记录

        Returns:
            dict: {"queried_orders": n, "matched_orders": n, "total_commission": Decimal}
        """
        summary = {
            "queried_orders": 0,
            "matched_orders": 0,
            "total_commission": Decimal("0"),
        }
        try:
            pending = await self._fetch_pending_trade_keys(lookback_hours)
            summary["queried_orders"] = len(pending)
            for symbol, order_id in pending:
                if not order_id:
                    continue
                try:
                    matched, commission = await self._query_order_commission(
                        binance_client, symbol, order_id
                    )
                    if not matched:
                        continue  # 零佣金或未成交，不做无意义的 0 值覆盖
                    await self.db.execute(
                        self._UPDATE_COMMISSION_SQL, str(commission), symbol, order_id
                    )
                    summary["matched_orders"] += 1
                    summary["total_commission"] += commission
                except Exception as e:
                    logger.warning(
                        "佣金回填查询失败", symbol=symbol, order_id=order_id, error=str(e)
                    )
            logger.info(
                "佣金回填完成",
                queried_orders=summary["queried_orders"],
                matched_orders=summary["matched_orders"],
                total_commission=str(summary["total_commission"]),
            )
        except Exception as e:
            logger.error("佣金回填异常", error=str(e))
        return summary

    async def _fetch_pending_trade_keys(self, lookback_hours: int) -> List:
        """查询待回填的 (symbol, order_id) 去重组合

        Args:
            lookback_hours: 回看时间窗口（小时）

        Returns:
            [(symbol, order_id), ...]；无记录返回空列表
        """
        start_time = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(
            hours=lookback_hours
        )
        rows = await self.db.fetch_all(self._PENDING_RECORDS_SQL, start_time)
        return [(r["symbol"], r["order_id"]) for r in rows]

    async def _query_order_commission(self, binance_client, symbol: str, order_id: str):
        """查询单个订单在 userTrades 中的真实佣金并累加

        Args:
            binance_client: BinanceClient 实例
            symbol: 交易对
            order_id: 订单ID

        Returns:
            (matched, commission)：matched 表示佣金>0；commission 为累计 Decimal
        """
        trades = await binance_client.get_user_trades(symbol, order_id=order_id)
        total = Decimal("0")
        for trade in trades or []:
            raw = trade.get("commission")
            if raw is None or raw == "":
                continue
            total += Decimal(str(raw))
        return (total > 0), total