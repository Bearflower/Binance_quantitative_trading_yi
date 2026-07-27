"""
日报数据采集器
从统一交易记录表 trading.trade_records 采集各策略的昨日交易统计数据，
并通过 Binance income API 获取已实现盈亏计算胜率
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import structlog

from shared.database import DatabaseManager
from shared.trade_logger import TradeLogger

if TYPE_CHECKING:
    from shared.binance_api import BinanceClient

BEIJING_TZ = timezone(timedelta(hours=8))

logger = structlog.get_logger()


@dataclass
class StrategyStats:
    """
    单个策略的日统计数据

    Attributes:
        name: 策略名称
        detection_count: 检测次数（暂不可用，需策略层自行上报）
        valid_signals: 有效信号数（暂不可用，需策略层自行上报）
        order_count: 当日下单笔数（来自 trade_records）
        fill_count: 当日成交笔数（来自 Binance allOrders API）
        closed_count: 当日平仓笔数（wins + losses，来自 Binance P&L）
        win_count: 盈利笔数（来自 Binance REALIZED_PNL）
        loss_count: 亏损笔数（来自 Binance REALIZED_PNL）
        total_pnl: 当日已实现盈亏总额（USDT，来自 Binance REALIZED_PNL）
        error: 采集失败时的错误信息
    """
    name: str
    detection_count: int = 0
    valid_signals: int = 0
    order_count: int = 0
    fill_count: int = 0
    closed_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    total_pnl: Decimal = Decimal("0")
    error: Optional[str] = None

    @property
    def win_rate(self) -> float:
        closed = self.closed_count
        if closed == 0:
            return 0.0
        return round(self.win_count / closed * 100, 1)


class DailyReportCollector:
    """
    日报数据采集器
    通过 TradeLogger 从统一交易记录表 trading.trade_records 采集各策略的昨日交易统计，
    并通过 Binance income API 获取已实现盈亏流水计算真实胜率。

    新策略只需通过 BinanceClient 下单即可自动纳入日报覆盖范围，
    无需修改本采集器代码。
    """

    # 策略名 → 日报 key 映射
    _STRATEGY_KEY_MAP: Dict[str, str] = {
        "MTPCS策略": "btc_eth",
        "新币做空策略": "new_coin",
        "HRS策略": "hrs",
    }

    # 各策略的已知交易对（用于 P&L 按策略归属）
    _STRATEGY_SYMBOLS: Dict[str, List[str]] = {
        "btc_eth": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "TRXUSDT"],
    }

    def __init__(self, db_manager: DatabaseManager, binance_client: "Optional[BinanceClient]" = None):
        self.db = db_manager
        self.trade_logger = TradeLogger(db_manager, "日报采集器")
        self.binance_client = binance_client
        logger.info("日报数据采集器初始化完成（统一记录表模式）")

    @staticmethod
    def get_previous_day_range() -> Tuple[datetime, datetime]:
        """
        获取上一日（UTC+8 时区）的起止时间

        Returns:
            (start_time, end_time) 元组，北京时间无时区
            示例：当前 2026-05-12 15:30 → (2026-05-11 00:00:00, 2026-05-11 23:59:59)
        """
        now = datetime.now(BEIJING_TZ)
        yesterday = now.date() - timedelta(days=1)

        start_time = datetime.combine(
            yesterday,
            datetime.min.time(),
            tzinfo=BEIJING_TZ
        ).replace(tzinfo=None)
        end_time = datetime.combine(
            yesterday,
            datetime.max.time().replace(microsecond=0),
            tzinfo=BEIJING_TZ
        ).replace(tzinfo=None)

        logger.debug(
            "计算上一日时间范围",
            start=start_time.isoformat(),
            end=end_time.isoformat()
        )
        return start_time, end_time

    def _map_symbol_to_strategy_key(self, symbol: str) -> Optional[str]:
        """
        将交易对映射到策略 key

        根据各策略的已知交易对列表进行归属判断。
        新币做空策略兜底：不在 MTPCS 列表中的都归为新币。

        Args:
            symbol: 交易对（如 "BTCUSDT"）

        Returns:
            策略 key（btc_eth / new_coin / hrs），无法归属返回 None
        """
        for key, symbols in self._STRATEGY_SYMBOLS.items():
            if symbol.upper() in symbols:
                return key
        # 兜底归为新币做空策略
        return "new_coin"

    def _map_symbol_to_strategy_name(self, symbol: str) -> str:
        """将交易对映射到策略名称（用于日志）"""
        key = self._map_symbol_to_strategy_key(symbol)
        if key == "btc_eth":
            return "MTPCS策略"
        elif key == "hrs":
            return "HRS策略"
        elif key == "new_coin":
            return "新币做空策略"
        return f"未知策略({symbol})"

    async def _fetch_daily_pnl(
        self,
        start_time: datetime,
        end_time: datetime
    ) -> Dict[str, Dict[str, int]]:
        """
        从 Binance income API 获取指定日期的已实现盈亏，按策略分组统计胜负

        Args:
            start_time: 查询起始时间（北京时间，无时区）
            end_time: 查询结束时间（北京时间，无时区）

        Returns:
            各策略的胜负统计：{ strategy_key: {"wins": N, "losses": N, "total_pnl": Decimal} }
        """
        if self.binance_client is None:
            logger.info("未配置 Binance 客户端，跳过 P&L 查询")
            return {}

        # 转为 UTC 时间戳（毫秒）
        start_bj = start_time.replace(tzinfo=BEIJING_TZ)
        end_bj = end_time.replace(tzinfo=BEIJING_TZ)
        start_ms = int(start_bj.timestamp() * 1000)
        end_ms = int(end_bj.timestamp() * 1000)

        logger.info(
            "查询 Binance 已实现盈亏流水",
            start=start_bj.isoformat(),
            end=end_bj.isoformat()
        )

        try:
            income_list = await self.binance_client.get_income_history(
                start_time=start_ms,
                end_time=end_ms,
                income_type="REALIZED_PNL"
            )
        except Exception as e:
            logger.error("Binance income API 查询失败", error=str(e), exc_info=True)
            return {}

        if not income_list:
            logger.info("查询时间段内无已实现盈亏记录")
            return {}

        # 按策略分组统计
        pnl_by_strategy: Dict[str, Dict[str, int]] = {}

        for entry in income_list:
            symbol = entry.get("symbol", "")
            income_str = entry.get("income", "0")

            try:
                income = Decimal(str(income_str))
            except Exception:
                continue

            strategy_key = self._map_symbol_to_strategy_key(symbol)
            if strategy_key is None:
                continue

            if strategy_key not in pnl_by_strategy:
                pnl_by_strategy[strategy_key] = {"wins": 0, "losses": 0, "total_pnl": Decimal("0")}

            if income > 0:
                pnl_by_strategy[strategy_key]["wins"] += 1
            elif income < 0:
                pnl_by_strategy[strategy_key]["losses"] += 1

            pnl_by_strategy[strategy_key]["total_pnl"] += income

        logger.info(
            "P&L 按策略分组完成",
            result={
                k: f"wins={v['wins']}, losses={v['losses']}, pnl={v['total_pnl']}"
                for k, v in pnl_by_strategy.items()
            }
        )

        return pnl_by_strategy

    async def _fetch_daily_fills(
        self,
        start_time: datetime,
        end_time: datetime,
        known_symbols: Dict[str, List[str]]
    ) -> Dict[str, int]:
        """
        从 Binance allOrders API 获取指定日期的成交笔数（status=FILLED），按策略分组

        Args:
            start_time: 查询起始时间（北京时间，无时区）
            end_time: 查询结束时间（北京时间，无时区）
            known_symbols: 各策略已知的交易对列表 { strategy_key: [symbols] }

        Returns:
            各策略的成交笔数：{ strategy_key: fill_count }
        """
        if self.binance_client is None:
            logger.info("未配置 Binance 客户端，跳过成交数采集")
            return {}

        start_bj = start_time.replace(tzinfo=BEIJING_TZ)
        end_bj = end_time.replace(tzinfo=BEIJING_TZ)
        start_ms = int(start_bj.timestamp() * 1000)
        end_ms = int(end_bj.timestamp() * 1000)

        result: Dict[str, int] = {}

        for strategy_key, symbols in known_symbols.items():
            total_fills = 0
            for symbol in symbols:
                try:
                    orders = await self.binance_client.get_order_history(
                        symbol=symbol,
                        start_time=start_ms,
                        end_time=end_ms,
                        limit=1000
                    )
                    for o in orders:
                        update_time = o.get("updateTime", o.get("time", 0))
                        if (o.get("status", "") == "FILLED"
                                and start_ms <= update_time <= end_ms):
                            total_fills += 1
                except Exception as e:
                    logger.warning(
                        "获取成交数失败，跳过该币种",
                        symbol=symbol,
                        strategy=strategy_key,
                        error=str(e)[:80]
                    )
                    continue
            result[strategy_key] = total_fills
            logger.info(
                "成交数采集完成",
                strategy=strategy_key,
                fill_count=total_fills,
                symbol_count=len(symbols)
            )

        return result

    async def collect_all(self) -> Dict[str, StrategyStats]:
        """
        采集所有策略的昨日统计数据

        从统一交易记录表 trading.trade_records 一次性查询所有策略的交易数据，
        不再逐 schema 查询各策略私有表。

        Returns:
            key 为策略标识（btc_eth / new_coin / hrs），
            value 为对应的 StrategyStats 实例
        """
        start_time, end_time = self.get_previous_day_range()
        report_date = start_time.strftime("%Y-%m-%d")

        logger.info(
            "开始采集日报数据（统一记录表模式）",
            report_date=report_date,
            start=start_time.isoformat(),
            end=end_time.isoformat()
        )

        result: Dict[str, StrategyStats] = {}

        # 1. 查询订单数量（trade_records 表）
        try:
            unified_stats = await self.trade_logger.get_daily_stats(start_time)
            logger.info(
                "统一记录表查询完成",
                report_date=report_date,
                strategy_count=len(unified_stats)
            )

            # 转换为 StrategyStats 并映射 key（先填充订单数）
            for strategy_name, data in unified_stats.items():
                key = self._STRATEGY_KEY_MAP.get(strategy_name)
                if key is None:
                    key = strategy_name.lower().replace("策略", "")
                    logger.warning(
                        "未知策略，使用自动生成 key",
                        strategy_name=strategy_name,
                        auto_key=key
                    )

                stats = StrategyStats(
                    name=strategy_name,
                    detection_count=0,
                    valid_signals=0,
                    order_count=data["trade_count"],
                    fill_count=0,
                    win_count=0,
                    loss_count=0,
                    closed_count=0
                )
                result[key] = stats
                logger.info(
                    f"{strategy_name}数据采集完成",
                    key=key,
                    order_count=stats.order_count
                )

        except Exception as e:
            logger.error(
                "统一记录表查询失败",
                error=str(e),
                exc_info=True
            )
            for strategy_name, key in self._STRATEGY_KEY_MAP.items():
                result[key] = StrategyStats(
                    name=strategy_name,
                    error=f"数据采集失败: {str(e)}"
                )
            return result

        # 2. 查询 P&L 流水并计算真实胜率
        try:
            pnl_stats = await self._fetch_daily_pnl(start_time, end_time)

            for key in list(result.keys()):
                if key in pnl_stats and not result[key].error:
                    pnl = pnl_stats[key]
                    result[key].win_count = pnl["wins"]
                    result[key].loss_count = pnl["losses"]
                    result[key].closed_count = pnl["wins"] + pnl["losses"]
                    result[key].total_pnl = pnl["total_pnl"]
                    logger.info(
                        f"{result[key].name} 盈亏数据已更新",
                        key=key,
                        wins=pnl["wins"],
                        losses=pnl["losses"],
                        total_pnl=str(pnl["total_pnl"])
                    )
        except Exception as e:
            logger.error(
                "P&L 数据查询失败，胜率将显示为0",
                error=str(e),
                exc_info=True
            )

        # 3. 查询成交数（fill_count，来自 allOrders API）
        try:
            fill_stats = await self._fetch_daily_fills(
                start_time, end_time, self._STRATEGY_SYMBOLS
            )
            for key in list(result.keys()):
                if key in fill_stats and not result[key].error:
                    result[key].fill_count = fill_stats[key]
                    logger.info(
                        f"{result[key].name} 成交数据已更新",
                        key=key,
                        fill_count=fill_stats[key]
                    )
        except Exception as e:
            logger.error(
                "成交数采集失败，fill_count 将显示为0",
                error=str(e),
                exc_info=True
            )

        logger.info(
            "日报数据采集全部完成",
            strategy_count=len(result),
            report_date=report_date
        )

        return result