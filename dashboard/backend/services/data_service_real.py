#!/usr/bin/env python3
"""
Dashboard 数据服务（Docker容器版本）
使用数据库查询订单数，通过 Binance API 获取盈亏数据
"""
from datetime import datetime, timedelta, timezone
import os
import sys

import structlog

# 添加项目根目录到Python路径
sys.path.insert(0, "/app")

from shared.database import DatabaseManager
from shared.binance_api import BinanceClient
from shared.trade_logger import TradeLogger

logger = structlog.get_logger()
BEIJING_TZ = timezone(timedelta(hours=8))


class DataService:
    """真实数据服务"""

    _STRATEGY_KEY_MAP = {
        "MTPCS策略": "btc_eth",
        "新币做空策略": "new_coin",
        "HRS策略": "hrs",
    }

    _STRATEGY_NAME_MAP = {v: k for k, v in _STRATEGY_KEY_MAP.items()}

    _STRATEGY_SYMBOLS = {
        "btc_eth": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "TRXUSDT"],
    }

    _STRATEGY_EMOJI_MAP = {
        "btc_eth": "📈",
        "new_coin": "📉",
        "hrs": "🔄",
    }

    _ORDER_HISTORY_LIMIT = int(os.getenv("ORDER_HISTORY_LIMIT", "1000"))

    def __init__(self):
        self._db_manager = None
        self._binance_client = None
        self._trade_logger = None
        self._initialized = False

    async def _ensure_initialized(self):
        if self._initialized:
            return

        self._db_manager = DatabaseManager(
            host=os.getenv("DATABASE_HOST", "postgres"),
            port=int(os.getenv("DATABASE_PORT", "5432")),
            database=os.getenv("DATABASE_NAME", "trading_platform"),
            user=os.getenv("DATABASE_USER", "trading_user"),
            password=os.getenv("DATABASE_PASSWORD", "trading_password_2024"),
            min_pool_size=int(os.getenv("DB_MIN_POOL_SIZE", "1")),
            max_pool_size=int(os.getenv("DB_MAX_POOL_SIZE", "5")),
        )
        await self._db_manager.connect()

        self._binance_client = BinanceClient(
            api_key=os.getenv("BINANCE_API_KEY", ""),
            api_secret=os.getenv("BINANCE_API_SECRET", ""),
            testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
            use_unified_account=os.getenv("USE_UNIFIED_ACCOUNT", "true").lower() == "true",
        )

        self._trade_logger = TradeLogger(self._db_manager, "Dashboard采集器")
        self._initialized = True
        logger.info("Dashboard数据服务初始化完成")

    def _get_date_range(self, report_type: str):
        """计算实时时间范围：日=今天00:00~现在，周=本周一00:00~现在，月=本月1日00:00~现在"""
        now = datetime.now(BEIJING_TZ)
        if report_type == "daily":
            today = now.date()
            start = datetime.combine(today, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
        elif report_type == "weekly":
            this_monday = now.date() - timedelta(days=now.weekday())
            start = datetime.combine(this_monday, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
        elif report_type == "monthly":
            start = datetime(now.year, now.month, 1, tzinfo=BEIJING_TZ).replace(tzinfo=None)
        else:
            # 未知类型默认日视图
            today = now.date()
            start = datetime.combine(today, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
        end = now.replace(tzinfo=None)
        return start, end

    async def _get_hrs_symbols(self) -> set:
        """从DB获取HRS策略交易过的币种"""
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT DISTINCT symbol FROM trading.trade_records WHERE strategy = 'HRS策略'"
            )
            return {row["symbol"] for row in rows}
        except Exception as e:
            logger.warning("HRS策略币种查询失败", error=str(e)[:80])
            return set()

    async def _get_pnl_by_strategy(self, start_time, end_time):
        """通过 Binance API 获取各策略的已实现盈亏"""
        start_bj = start_time.replace(tzinfo=BEIJING_TZ)
        end_bj = end_time.replace(tzinfo=BEIJING_TZ)
        start_ms = int(start_bj.timestamp() * 1000)
        end_ms = int(end_bj.timestamp() * 1000)

        result = {}
        strategy_keys = list(self._STRATEGY_KEY_MAP.values())
        for key in strategy_keys:
            result[key] = {"wins": 0, "losses": 0, "total_pnl": 0.0}

        try:
            income_list = await self._binance_client.get_income_history(
                start_time=start_ms,
                end_time=end_ms,
                income_type="REALIZED_PNL",
            )
        except Exception as e:
            logger.warning("Binance PNL查询失败", error=str(e))
            return result

        if not income_list:
            return result

        for entry in income_list:
            symbol = entry.get("symbol", "")
            income_str = entry.get("income", "0")
            try:
                income = float(income_str)
            except (ValueError, TypeError):
                continue

            strategy_key = None
            for key, symbols in self._STRATEGY_SYMBOLS.items():
                if symbol.upper() in symbols:
                    strategy_key = key
                    break
            if strategy_key is None:
                # 查询HRS币种归属
                hrs_symbols = await self._get_hrs_symbols()
                if symbol in hrs_symbols:
                    strategy_key = "hrs"
                else:
                    strategy_key = "new_coin"

            if income > 0:
                result[strategy_key]["wins"] += 1
            elif income < 0:
                result[strategy_key]["losses"] += 1
            result[strategy_key]["total_pnl"] += income

        return result

    async def _get_fill_count_by_strategy(self, start_time, end_time):
        """通过 Binance API 获取各策略的成交笔数"""
        start_bj = start_time.replace(tzinfo=BEIJING_TZ)
        end_bj = end_time.replace(tzinfo=BEIJING_TZ)
        start_ms = int(start_bj.timestamp() * 1000)
        end_ms = int(end_bj.timestamp() * 1000)

        result = {}
        # 查询固定策略币种
        for key, symbols in self._STRATEGY_SYMBOLS.items():
            total_fills = 0
            for symbol in symbols:
                try:
                    orders = await self._binance_client.get_order_history(
                        symbol=symbol,
                        start_time=start_ms,
                        end_time=end_ms,
                        limit=self._ORDER_HISTORY_LIMIT,
                    )
                    for o in orders:
                        update_time = o.get("updateTime", o.get("time", 0))
                        if o.get("status", "") == "FILLED" and start_ms <= update_time <= end_ms:
                            total_fills += 1
                except Exception as e:
                    logger.warning("获取成交数失败", symbol=symbol, error=str(e)[:80])
            result[key] = total_fills

        # 查询HRS动态币种成交数
        hrs_symbols = await self._get_hrs_symbols()
        hrs_total = 0
        for symbol in hrs_symbols:
            try:
                orders = await self._binance_client.get_order_history(
                    symbol=symbol,
                    start_time=start_ms,
                    end_time=end_ms,
                    limit=self._ORDER_HISTORY_LIMIT,
                )
                for o in orders:
                    update_time = o.get("updateTime", o.get("time", 0))
                    if o.get("status", "") == "FILLED" and start_ms <= update_time <= end_ms:
                        hrs_total += 1
            except Exception as e:
                logger.warning("获取HRS成交数失败", symbol=symbol, error=str(e)[:80])
        result["hrs"] = hrs_total

        return result

    async def get_overview(self, report_type: str = "daily"):
        await self._ensure_initialized()
        start_time, end_time = self._get_date_range(report_type)

        # 1. 从数据库获取订单数
        unified_stats = await self._trade_logger.get_daily_stats(start_time)

        # 2. 从 Binance API 获取盈亏
        pnl_stats = await self._get_pnl_by_strategy(start_time, end_time)

        # 3. 从 Binance API 获取成交数
        fill_stats = await self._get_fill_count_by_strategy(start_time, end_time)

        strategies_data = []
        total_pnl = 0.0
        total_orders = 0
        total_closed = 0
        total_wins = 0

        strategy_keys = list(self._STRATEGY_KEY_MAP.values())
        for strategy_key in strategy_keys:
            strategy_name = self._STRATEGY_NAME_MAP.get(strategy_key, strategy_key)
            order_count = 0

            for name, data in unified_stats.items():
                mapped_key = self._STRATEGY_KEY_MAP.get(name, "")
                if mapped_key == strategy_key:
                    order_count = data["trade_count"]
                    break

            pnl = pnl_stats.get(strategy_key, {"wins": 0, "losses": 0, "total_pnl": 0.0})
            fill_count = fill_stats.get(strategy_key, 0)
            closed_count = pnl["wins"] + pnl["losses"]
            strategy_pnl = pnl["total_pnl"]
            win_count = pnl["wins"]

            total_pnl += strategy_pnl
            total_orders += order_count
            total_closed += closed_count
            total_wins += win_count

            strategies_data.append({
                "emoji": self._STRATEGY_EMOJI_MAP.get(strategy_key, ""),
                "id": strategy_key,
                "name": strategy_name,
                "order_count": order_count,
                "fill_count": fill_count,
                "closed_count": closed_count,
                "win_count": win_count,
                "loss_count": pnl["losses"],
                "total_pnl": f"{strategy_pnl:.4f}",
                "win_rate": round(win_count / closed_count * 100, 2) if closed_count > 0 else 0.0,
                "report_type": report_type,
                "updated_at": datetime.now(BEIJING_TZ).isoformat(),
            })

        win_rate = (total_wins / total_closed * 100) if total_closed > 0 else 0

        return {
            "total_pnl": f"{total_pnl:.4f}",
            "total_orders": total_orders,
            "total_closed": total_closed,
            "total_wins": total_wins,
            "win_rate": round(win_rate, 2),
            "strategies": strategies_data,
            "report_type": report_type,
            "updated_at": datetime.now(BEIJING_TZ).isoformat(),
        }

    async def get_strategies(self, report_type: str = "daily"):
        overview = await self.get_overview(report_type)
        return overview["strategies"]

    async def get_strategy_detail(self, strategy_id: str, report_type: str = "daily"):
        strategies = await self.get_strategies(report_type)
        base = None
        for s in strategies:
            if s["id"] == strategy_id:
                base = s
                break
        if not base:
            return None

        await self._ensure_initialized()
        start_time, end_time = self._get_date_range(report_type)

        symbols = await self.get_strategy_symbols(strategy_id, report_type)
        # 将symbols列表转换为字典
        daily_counts = {}
        for sym in symbols:
            daily_counts[sym["symbol"]] = sym["order_count"]

        return {
            **base,
            "avg_daily_orders": 0.0,
            "symbols": symbols,
            "daily_counts": daily_counts,
            "data_source": "binance_api",
            "validation_warnings": [],
            "error": None,
        }

    async def get_strategy_symbols(self, strategy_id: str, report_type: str = "daily"):
        await self._ensure_initialized()
        start_time, end_time = self._get_date_range(report_type)

        name_mapping = {v: k for k, v in self._STRATEGY_KEY_MAP.items()}
        strategy_name = name_mapping.get(strategy_id, "")
        if not strategy_name:
            return []

        rows = await self._db_manager.fetch_all(
            "SELECT symbol, COUNT(*) as order_count, "
            "AVG(price)::numeric(18,4) as avg_price, "
            "SUM(quantity) as total_quantity "
            "FROM trading.trade_records "
            "WHERE strategy = $1 AND executed_at >= $2 AND executed_at <= $3 "
            "GROUP BY symbol ORDER BY order_count DESC",
            strategy_name, start_time, end_time
        )

        symbols = []
        for row in rows:
            symbol = row["symbol"]
            order_count = row["order_count"]

            # 通过 Binance API 获取该币种的盈亏
            symbol_pnl = 0.0
            start_bj = start_time.replace(tzinfo=BEIJING_TZ)
            end_bj = end_time.replace(tzinfo=BEIJING_TZ)
            start_ms = int(start_bj.timestamp() * 1000)
            end_ms = int(end_bj.timestamp() * 1000)

            try:
                income_list = await self._binance_client.get_income_history(
                    start_time=start_ms,
                    end_time=end_ms,
                    income_type="REALIZED_PNL",
                    symbol=symbol,
                )
                if income_list:
                    for entry in income_list:
                        try:
                            symbol_pnl += float(entry.get("income", 0))
                        except (ValueError, TypeError):
                            pass
            except Exception:
                pass

            symbols.append({
                "symbol": symbol,
                "order_count": order_count,
                "fill_count": order_count,
                "wins": 0,
                "losses": 0,
                "total_pnl": f"{symbol_pnl:.4f}",
                "win_rate": 0.0,
                "data_quality": "ok",
                "quality_note": "",
            })

        return symbols

    async def get_trend_data(self, report_type: str = "daily", days: int = 7):
        await self._ensure_initialized()
        trends = []

        now = datetime.now(BEIJING_TZ)
        
        for i in range(days):
            if report_type == "daily":
                day = now.date() - timedelta(days=i)
                start = datetime.combine(day, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                # 当天(i=0)用当前时刻，历史天用当天结束
                if i == 0:
                    end = now.replace(tzinfo=None)
                else:
                    end = datetime.combine(day, datetime.max.time().replace(microsecond=0), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                date_str = day.strftime("%m/%d")
            elif report_type == "weekly":
                week_start = now.date() - timedelta(weeks=i)
                week_start = week_start - timedelta(days=week_start.weekday())
                start = datetime.combine(week_start, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                if i == 0:
                    end = now.replace(tzinfo=None)
                else:
                    end = datetime.combine(week_start + timedelta(days=6), datetime.max.time().replace(microsecond=0), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                date_str = week_start.strftime("%m/%d")
            else:  # 月视图
                # 计算 i 个月前的月初
                month_date = now.replace(day=1)
                for _ in range(i):
                    month_date = (month_date - timedelta(days=1)).replace(day=1)
                start = datetime.combine(month_date, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                if i == 0:
                    end = now.replace(tzinfo=None)
                else:
                    # 计算该月最后一天
                    next_month = month_date.replace(day=28) + timedelta(days=4)
                    last_day = next_month - timedelta(days=next_month.day)
                    end = datetime.combine(last_day, datetime.max.time().replace(microsecond=0), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                date_str = month_date.strftime("%Y/%m")

            # 从数据库查询该时间段的订单数
            unified_stats = await self._trade_logger.get_daily_stats(start)
            total_orders = sum(data.get("trade_count", 0) for data in unified_stats.values())

            # 从 Binance API 获取该时间段的盈亏
            start_bj = start.replace(tzinfo=BEIJING_TZ)
            end_bj = end.replace(tzinfo=BEIJING_TZ)
            start_ms = int(start_bj.timestamp() * 1000)
            end_ms = int(end_bj.timestamp() * 1000)

            total_pnl = 0.0
            try:
                income_list = await self._binance_client.get_income_history(
                    start_time=start_ms,
                    end_time=end_ms,
                    income_type="REALIZED_PNL",
                )
                if income_list:
                    for entry in income_list:
                        try:
                            total_pnl += float(entry.get("income", 0))
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.warning("趋势数据PNL查询失败", date=date_str, error=str(e)[:80])

            trends.append({
                "date": date_str,
                "total_pnl": f"{total_pnl:.4f}",
                "order_count": total_orders,
                "win_rate": 0.0,
            })

        return list(reversed(trends))