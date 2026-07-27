"""
周报数据采集器
从统一交易记录表 trading.trade_records 和 Binance income API 采集过去一周数据，
按策略和币种双维度聚合，计算胜率、收益率等指标。
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import structlog

from shared.database import DatabaseManager

if TYPE_CHECKING:
    from shared.binance_api import BinanceClient

BEIJING_TZ = timezone(timedelta(hours=8))

logger = structlog.get_logger()


@dataclass
class SymbolStats:
    symbol: str
    order_count: int = 0
    fill_count: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: Decimal = Decimal("0")
    cross_week_fills: int = 0
    data_quality: str = "ok"
    quality_note: str = ""

    @property
    def closed_count(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        c = self.closed_count
        return round(self.wins / c * 100, 1) if c > 0 else 0.0


@dataclass
class WeeklyStrategyStats:
    name: str
    emoji: str = ""
    order_count: int = 0
    fill_count: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: Decimal = Decimal("0")
    symbols: Dict[str, SymbolStats] = field(default_factory=dict)
    daily_counts: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None
    data_source: str = "binance_api"
    validation_warnings: List[str] = field(default_factory=list)

    @property
    def closed_count(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        c = self.closed_count
        return round(self.wins / c * 100, 1) if c > 0 else 0.0

    @property
    def avg_daily_orders(self) -> float:
        if self.order_count == 0:
            return 0.0
        trading_days = max(len(self.daily_counts), 1)
        return round(self.order_count / trading_days, 1)


@dataclass
class ValidationResult:
    strategy_key: str
    passed: bool
    hard_violations: List[str] = field(default_factory=list)
    soft_warnings: List[str] = field(default_factory=list)
    cross_week_detected: bool = False
    data_source: str = "binance_api"


class WeeklyReportCollector:
    _STRATEGY_KEY_MAP: Dict[str, str] = {
        "MTPCS策略": "btc_eth",
        "新币做空策略": "new_coin",
        "HRS策略": "hrs",
    }

    _REVERSE_KEY_MAP: Dict[str, str] = {
        "btc_eth": "MTPCS策略",
        "new_coin": "新币做空策略",
        "hrs": "HRS策略",
    }

    _STRATEGY_EMOJI: Dict[str, str] = {
        "btc_eth": "📈",
        "new_coin": "📉",
        "hrs": "🔄",
    }

    _STRATEGY_SYMBOLS: Dict[str, List[str]] = {
        "btc_eth": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "TRXUSDT"],
    }

    def __init__(self, db_manager: DatabaseManager, binance_client: "Optional[BinanceClient]" = None):
        self.db = db_manager
        self.binance_client = binance_client
        logger.info("周报数据采集器初始化完成")

    @staticmethod
    def get_previous_week_range() -> Tuple[datetime, datetime, str, str]:
        """
        获取最近一个完整周（周一00:00 到 周日23:59:59）的起止时间

        适配周日定时推送：周日运行时统计本周 Mon-Sun 的数据。
        通用策略：找到最近已过去的周日（含今天）为 end，往前推 6 天为 start。
        无论哪天运行，都统计最近一个完整周（Mon-Sun）。

        Returns:
            (start_time, end_time, start_label, end_label)
        """
        now = datetime.now(BEIJING_TZ)
        today = now.date()
        today_weekday = today.weekday()  # 0=Mon ... 6=Sun

        # 距离上个周日的天数：如果今天是周日(6)，则0天前（今天）；否则 today_weekday + 1 天前
        days_to_last_sunday = 0 if today_weekday == 6 else (today_weekday + 1)
        last_sunday = today - timedelta(days=days_to_last_sunday)
        last_monday = last_sunday - timedelta(days=6)

        start_time = datetime.combine(last_monday, datetime.min.time()).replace(tzinfo=None)
        end_time = datetime.combine(
            last_sunday, datetime.max.time().replace(microsecond=0)
        ).replace(tzinfo=None)

        start_label = last_monday.strftime("%m/%d")
        end_label = last_sunday.strftime("%m/%d")

        logger.info("计算上一周时间范围", start=start_label, end=end_label)
        return start_time, end_time, start_label, end_label

    def _map_symbol_to_strategy_key(self, symbol: str) -> Optional[str]:
        for key, symbols in self._STRATEGY_SYMBOLS.items():
            if symbol.upper() in symbols:
                return key
        return "new_coin"

    async def _fetch_unified_orders(
        self, start_time: datetime, end_time: datetime
    ) -> Dict[str, WeeklyStrategyStats]:
        if self.binance_client is None:
            logger.warning("Binance 客户端未初始化，跳过统一订单采集")
            return {}
        three_weeks = timedelta(weeks=3)
        lookback_start = start_time - three_weeks

        start_ms = int(start_time.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        end_ms = int(end_time.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        lookback_start_ms = int(lookback_start.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)

        result: Dict[str, WeeklyStrategyStats] = {}
        api_failures = 0
        total_attempts = 0

        for strategy_key, symbols in self._STRATEGY_SYMBOLS.items():
            stats = WeeklyStrategyStats(
                name=self._REVERSE_KEY_MAP.get(strategy_key, strategy_key),
                emoji=self._STRATEGY_EMOJI.get(strategy_key, "📊")
            )

            for symbol in symbols:
                sym_stats = SymbolStats(symbol=symbol)
                total_attempts += 1

                all_orders = []
                batch_start = lookback_start_ms
                seven_days_ms = 7 * 24 * 3600 * 1000
                try:
                    while batch_start < end_ms:
                        batch_end = min(batch_start + seven_days_ms, end_ms)
                        batch_orders = await self.binance_client.get_order_history(
                            symbol=symbol,
                            start_time=batch_start,
                            end_time=batch_end
                        )
                        all_orders.extend(batch_orders)
                        batch_start = batch_end
                except Exception as e:
                    api_failures += 1
                    sym_stats.data_quality = "degraded"
                    sym_stats.quality_note = f"API查询失败: {str(e)[:80]}"
                    logger.warning("统一订单采集失败，跳过", symbol=symbol, error=str(e))
                    stats.symbols[symbol] = sym_stats
                    continue

                for o in all_orders:
                    order_time = o.get("time", 0)
                    update_time = o.get("updateTime", order_time)
                    status = o.get("status", "")

                    if start_ms <= order_time <= end_ms:
                        sym_stats.order_count += 1
                        order_date = datetime.fromtimestamp(
                            order_time / 1000, tz=BEIJING_TZ
                        ).strftime("%Y-%m-%d")
                        stats.daily_counts[order_date] = stats.daily_counts.get(order_date, 0) + 1

                    if status == "FILLED" and start_ms <= update_time <= end_ms:
                        sym_stats.fill_count += 1
                        if order_time < start_ms:
                            sym_stats.cross_week_fills += 1

                stats.symbols[symbol] = sym_stats
                stats.order_count += sym_stats.order_count
                stats.fill_count += sym_stats.fill_count

            result[strategy_key] = stats

        if api_failures == total_attempts:
            for s in result.values():
                s.data_source = "trade_records_fallback"
                s.validation_warnings.append("所有币种API查询失败，数据可能不完整")
        elif api_failures > 0:
            for s in result.values():
                s.data_source = "mixed"

        total_orders = sum(s.order_count for s in result.values())
        logger.info(
            "统一订单采集完成",
            total_orders=total_orders,
            total_fills=sum(s.fill_count for s in result.values()),
            api_failures=api_failures,
            total_attempts=total_attempts
        )
        return result

    async def _fetch_order_data(
        self, start_time: datetime, end_time: datetime
    ) -> Dict[str, WeeklyStrategyStats]:
        """查询 trade_records 获取各策略的周下单数和逐日分布"""
        result: Dict[str, WeeklyStrategyStats] = {}

        # 按策略+币种聚合
        rows = await self.db.fetch_all(
            "SELECT strategy, symbol, DATE(executed_at) as trade_date, COUNT(*) as cnt "
            "FROM trading.trade_records "
            "WHERE executed_at BETWEEN $1 AND $2 "
            "GROUP BY strategy, symbol, DATE(executed_at) "
            "ORDER BY strategy, symbol, trade_date",
            start_time, end_time
        )

        for row in rows:
            strategy_name = row["strategy"]
            symbol = row["symbol"]
            trade_date = str(row["trade_date"])
            cnt = row["cnt"]

            key = self._STRATEGY_KEY_MAP.get(strategy_name, strategy_name)
            if key not in result:
                result[key] = WeeklyStrategyStats(
                    name=strategy_name,
                    emoji=self._STRATEGY_EMOJI.get(key, "📊")
                )

            stats = result[key]
            stats.order_count += cnt
            stats.daily_counts[trade_date] = stats.daily_counts.get(trade_date, 0) + cnt

            if symbol not in stats.symbols:
                stats.symbols[symbol] = SymbolStats(symbol=symbol)
            stats.symbols[symbol].order_count += cnt

        # 确保三个主要策略有条目
        for strategy_name, key in self._STRATEGY_KEY_MAP.items():
            if key not in result:
                result[key] = WeeklyStrategyStats(
                    name=strategy_name,
                    emoji=self._STRATEGY_EMOJI.get(key, "📊")
                )
            else:
                result[key].name = strategy_name

        total_orders = sum(s.order_count for s in result.values())
        logger.info(
            "周订单数据采集完成",
            total_orders=total_orders,
            strategy_count=len(result)
        )
        return result

    async def _fetch_weekly_pnl(
        self, start_time: datetime, end_time: datetime
    ) -> Dict[str, Dict[str, List[Tuple[Decimal, str]]]]:
        """
        从 Binance income API 获取一周的已实现盈亏，按策略+币种分组

        Returns:
            { strategy_key: { symbol: [(income, entry_time), ...] } }
        """
        if self.binance_client is None:
            return {}

        start_ms = int(start_time.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        end_ms = int(end_time.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)

        try:
            income_list = await self.binance_client.get_income_history(
                start_time=start_ms,
                end_time=end_ms,
                income_type="REALIZED_PNL"
            )
        except Exception as e:
            logger.error("Binance income API 查询失败", error=str(e))
            return {}

        if not income_list:
            logger.info("周查询时间段内无已实现盈亏记录")
            return {}

        result: Dict[str, Dict[str, List[Tuple[Decimal, str]]]] = {}

        for entry in income_list:
            symbol = entry.get("symbol", "")
            income_str = entry.get("income", "0")
            income_time = entry.get("time", "")

            try:
                income = Decimal(str(income_str))
            except Exception:
                continue

            strategy_key = self._map_symbol_to_strategy_key(symbol)
            if strategy_key is None:
                continue

            if strategy_key not in result:
                result[strategy_key] = {}

            if symbol not in result[strategy_key]:
                result[strategy_key][symbol] = []

            result[strategy_key][symbol].append((income, income_time))

        total_entries = sum(
            len(entries) for symbols in result.values() for entries in symbols.values()
        )
        logger.info("周 P&L 数据采集完成", total_entries=total_entries)
        return result

    async def _merge_pnl_into_stats(
        self,
        stats: Dict[str, WeeklyStrategyStats],
        pnl_data: Dict[str, Dict[str, List[Tuple[Decimal, str]]]],
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """将 P&L 数据合并到策略统计中。

        P&L 数据来自 Binance income API，按策略 key 分组。
        如果某个策略 key 在 stats 中不存在（如 new_coin 动态币种），
        会自动创建 stats 条目，确保 P&L 数据不丢失。

        对于通过 income_api_only 自动创建的条目（如 new_coin），
        会额外从 Binance allOrders API 补充本周的委托/成交数据。
        """
        for key, symbols in pnl_data.items():
            if key not in stats:
                # 动态策略（如 new_coin）：income API 有 P&L 但 trade_records 无记录时，
                # 自动创建条目，至少能展示平仓数和盈亏
                stats[key] = WeeklyStrategyStats(
                    name=self._REVERSE_KEY_MAP.get(key, key),
                    emoji=self._STRATEGY_EMOJI.get(key, "📊"),
                    data_source="income_api_only"
                )
                logger.info(
                    "P&L 数据触发自动创建策略条目",
                    strategy_key=key,
                    strategy_name=stats[key].name,
                    symbol_count=len(symbols)
                )

            strategy_stats = stats[key]

            for symbol, pnl_entries in symbols.items():
                if symbol not in strategy_stats.symbols:
                    strategy_stats.symbols[symbol] = SymbolStats(symbol=symbol)

                sym = strategy_stats.symbols[symbol]

                for income, _time in pnl_entries:
                    sym.total_pnl += income
                    strategy_stats.total_pnl += income
                    if income > 0:
                        sym.wins += 1
                        strategy_stats.wins += 1
                    elif income < 0:
                        sym.losses += 1
                        strategy_stats.losses += 1

        logger.info(
            "P&L 数据合并完成",
            strategies=list(stats.keys())
        )

        # -- 补充 new_coin 等 income_api_only 策略的委托/成交数据 --
        # 这些策略的 P&L 来自 income API，但 order_count/fill_count 仍为 0，
        # 需要根据 P&L 涉及的 symbol 去 Binance allOrders API 查询本周订单
        if self.binance_client is not None:
            start_ms = int(start_time.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
            end_ms = int(end_time.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
            seven_days_ms = 7 * 24 * 3600 * 1000

            for key, strategy_stats in stats.items():
                if strategy_stats.data_source != "income_api_only":
                    continue

                symbols_to_query = list(strategy_stats.symbols.keys())
                if not symbols_to_query:
                    continue

                logger.info(
                    "补充 income_api_only 策略的委托/成交数据",
                    strategy=key,
                    symbols=symbols_to_query,
                )

                for symbol in symbols_to_query:
                    sym_stats = strategy_stats.symbols[symbol]
                    all_orders: list = []
                    batch_start = start_ms

                    try:
                        while batch_start < end_ms:
                            batch_end = min(batch_start + seven_days_ms, end_ms)
                            batch_orders = await self.binance_client.get_order_history(
                                symbol=symbol,
                                start_time=batch_start,
                                end_time=batch_end,
                            )
                            if batch_orders:
                                all_orders.extend(batch_orders)
                            batch_start = batch_end
                    except Exception as e:
                        logger.warning(
                            "income_api_only 策略订单查询失败",
                            strategy=key,
                            symbol=symbol,
                            error=str(e),
                        )
                        continue

                    for o in all_orders:
                        order_time = o.get("time", 0)
                        update_time = o.get("updateTime", order_time)
                        status = o.get("status", "")

                        # 委托时间在本周范围内
                        if start_ms <= order_time <= end_ms:
                            sym_stats.order_count += 1
                            strategy_stats.order_count += 1
                            order_date = datetime.fromtimestamp(
                                order_time / 1000, tz=BEIJING_TZ
                            ).strftime("%Y-%m-%d")
                            strategy_stats.daily_counts[order_date] = (
                                strategy_stats.daily_counts.get(order_date, 0) + 1
                            )

                        # 成交状态且 updateTime 在本周范围内
                        if status == "FILLED" and start_ms <= update_time <= end_ms:
                            sym_stats.fill_count += 1
                            strategy_stats.fill_count += 1

                logger.info(
                    "income_api_only 策略委托/成交数据补充完成",
                    strategy=key,
                    order_count=strategy_stats.order_count,
                    fill_count=strategy_stats.fill_count,
                )

    def _validate_consistency(self, stats: WeeklyStrategyStats) -> ValidationResult:
        strategy_key = self._STRATEGY_KEY_MAP.get(stats.name, stats.name)
        result = ValidationResult(strategy_key=strategy_key, passed=True)

        skip_fill_check = stats.data_source != "binance_api"

        if not skip_fill_check:
            if stats.fill_count < stats.closed_count:
                result.hard_violations.append(
                    f"成交({stats.fill_count}) < 平仓({stats.closed_count})，数据异常"
                )
                result.passed = False

        if stats.order_count == 0 and stats.closed_count > 0:
            result.cross_week_detected = True
            result.soft_warnings.append("本周委托=0但存在平仓，可能为前周条件单触发")

        for sym in stats.symbols.values():
            if not skip_fill_check and sym.fill_count < sym.closed_count:
                result.hard_violations.append(
                    f"{sym.symbol}: 成交({sym.fill_count}) < 平仓({sym.closed_count})"
                )
                result.passed = False
                sym.data_quality = "anomaly"
                sym.quality_note = "成交<平仓，数据不一致"

        stats.validation_warnings = result.soft_warnings
        return result

    async def collect_all(self) -> Dict[str, WeeklyStrategyStats]:
        start_time, end_time, start_label, end_label = self.get_previous_week_range()

        logger.info(
            "开始采集周报数据",
            start=start_label,
            end=end_label
        )

        # 1. 统一订单采集（allOrders → order_count + fill_count + cross_week_fills）
        try:
            stats = await self._fetch_unified_orders(start_time, end_time)
        except Exception as e:
            logger.error("统一订单采集全部失败，降级到 trade_records", error=str(e), exc_info=True)
            try:
                stats = await self._fetch_order_data(start_time, end_time)
                for s in stats.values():
                    s.data_source = "trade_records_fallback"
            except Exception as fallback_e:
                logger.error("降级采集也失败", error=str(fallback_e), exc_info=True)
                stats = {}
                for strategy_name, key in self._STRATEGY_KEY_MAP.items():
                    stats[key] = WeeklyStrategyStats(
                        name=strategy_name,
                        emoji=self._STRATEGY_EMOJI.get(key, "📊"),
                        error=f"数据采集失败: {str(e)}",
                        data_source="failed"
                    )
                return stats

        # 1.1 补充 new_coin 策略的委托/成交数据（该策略交易动态币种，无法通过 _STRATEGY_SYMBOLS 预定义）
        for key in list(self._STRATEGY_KEY_MAP.values()):
            if key not in self._STRATEGY_SYMBOLS and key not in stats:
                # 动态币种策略：使用 trade_records 补充 order_count 和 daily_counts
                strategy_name = self._REVERSE_KEY_MAP.get(key, key)
                rows = await self.db.fetch_all(
                    "SELECT symbol, DATE(executed_at) as trade_date, COUNT(*) as cnt "
                    "FROM trading.trade_records "
                    "WHERE strategy = $1 AND executed_at BETWEEN $2 AND $3 "
                    "GROUP BY symbol, DATE(executed_at) "
                    "ORDER BY symbol, trade_date",
                    strategy_name, start_time, end_time
                )

                if not rows:
                    # trade_records 中可能没有该策略名，尝试通过排除已知币种来查找动态币种订单
                    all_known_symbols = list(set().union(*[set(s.upper() for s in syms) for syms in self._STRATEGY_SYMBOLS.values()]))
                    placeholders = ','.join(f"${i+3}" for i in range(len(all_known_symbols)))
                    rows = await self.db.fetch_all(
                        f"SELECT symbol, DATE(executed_at) as trade_date, COUNT(*) as cnt "
                        f"FROM trading.trade_records "
                        f"WHERE executed_at BETWEEN $1 AND $2 "
                        f"AND UPPER(symbol) NOT IN ({placeholders}) "
                        f"GROUP BY symbol, DATE(executed_at) "
                        f"ORDER BY symbol, trade_date",
                        start_time, end_time, *all_known_symbols
                    )
                    if rows:
                        logger.info(
                            "通过符号排除匹配到动态币种订单",
                            strategy_key=key,
                            row_count=len(rows)
                        )

                if rows:
                    stats[key] = WeeklyStrategyStats(
                        name=strategy_name,
                        emoji=self._STRATEGY_EMOJI.get(key, "📊"),
                        data_source="trade_records"
                    )
                    strategy_stats = stats[key]
                    for row in rows:
                        symbol = row["symbol"]
                        trade_date = str(row["trade_date"])
                        cnt = row["cnt"]
                        strategy_stats.order_count += cnt
                        strategy_stats.daily_counts[trade_date] = (
                            strategy_stats.daily_counts.get(trade_date, 0) + cnt
                        )
                        if symbol not in strategy_stats.symbols:
                            strategy_stats.symbols[symbol] = SymbolStats(symbol=symbol)
                        strategy_stats.symbols[symbol].order_count += cnt
                    logger.info(
                        "已通过 trade_records 补充动态币种策略数据",
                        strategy=key,
                        strategy_name=strategy_name,
                        order_count=strategy_stats.order_count,
                        symbol_count=len(strategy_stats.symbols)
                    )

        # 2. P&L 数据（income API → closed_count/wins/losses/total_pnl）
        try:
            pnl_data = await self._fetch_weekly_pnl(start_time, end_time)
            await self._merge_pnl_into_stats(stats, pnl_data, start_time, end_time)
        except Exception as e:
            logger.error("P&L 合并失败", error=str(e), exc_info=True)

        # 3. 勾稽校验
        for key, strategy_stats in stats.items():
            validation = self._validate_consistency(strategy_stats)
            if not validation.passed:
                logger.warning("勾稽校验未通过", strategy=key, violations=validation.hard_violations)

        logger.info(
            "周报数据采集全部完成",
            strategy_count=len(stats),
            start=start_label,
            end=end_label
        )

        return stats