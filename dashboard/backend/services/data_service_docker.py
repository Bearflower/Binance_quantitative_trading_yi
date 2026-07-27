#!/usr/bin/env python3
"""
Dashboard 数据服务（Docker容器版本）
使用数据库查询订单数，通过 Binance API 获取盈亏数据
"""
from datetime import datetime, timedelta, timezone
from typing import Dict
import asyncio
import os
import sys

import structlog

# 添加项目根目录到Python路径
sys.path.insert(0, os.getenv("APP_ROOT", "/app"))

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
        self._income_cache = {}
        self._income_cache_ttl = int(os.getenv("INCOME_CACHE_TTL", "30"))  # income 缓存秒数
        self._income_cache_max = int(os.getenv("INCOME_CACHE_MAX", "20"))  # 缓存条目上限
        self._api_concurrency = int(os.getenv("API_CONCURRENCY", "5"))  # API 并发限制
        self._income_lock = asyncio.Lock()  # 防止缓存惊群

    async def _ensure_initialized(self):
        if self._initialized:
            return

        self._db_manager = DatabaseManager(
            host=os.getenv("DATABASE_HOST", os.getenv("DB_HOST", "postgres")),
            port=int(os.getenv("DATABASE_PORT", os.getenv("DB_PORT", "5432"))),
            database=os.getenv("DATABASE_NAME", os.getenv("POSTGRES_DB", "trading_platform")),
            user=os.getenv("DATABASE_USER", os.getenv("POSTGRES_USER", "trading_user")),
            password=os.getenv("DATABASE_PASSWORD", os.getenv("DB_PASSWORD", "")),  # 通过 docker-compose 环境变量传入
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
        """计算实时时间范围：日=今天，周=本周，月=本月（实时数据）"""
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
            today = now.date()
            start = datetime.combine(today, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
        end = now.replace(tzinfo=None)
        return start, end

    async def _get_income_data(self, start_ms: int, end_ms: int, income_type: str = "REALIZED_PNL") -> list:
        """获取 income 数据（带服务级缓存，同一时间范围只调一次 Binance API）

        Args:
            start_ms: 起始时间戳（毫秒）
            end_ms: 结束时间戳（毫秒）
            income_type: 收入类型，默认 "REALIZED_PNL"，也支持 "COMMISSION"

        Returns:
            income 记录列表
        """
        cache_key = (start_ms, end_ms, income_type)
        # 快速路径：缓存命中直接返回
        if cache_key in self._income_cache:
            data, cached_at = self._income_cache[cache_key]
            if (datetime.now() - cached_at).total_seconds() < self._income_cache_ttl:
                return data

        # 加锁防止惊群（双重检查）
        async with self._income_lock:
            if cache_key in self._income_cache:
                data, cached_at = self._income_cache[cache_key]
                if (datetime.now() - cached_at).total_seconds() < self._income_cache_ttl:
                    return data
            try:
                data = await self._binance_client.get_income_history(
                    start_time=start_ms,
                    end_time=end_ms,
                    income_type=income_type,
                )
            except Exception as e:
                logger.warning("Binance income API 查询失败", income_type=income_type, error=str(e)[:80])
                data = []
            self._income_cache[cache_key] = (data, datetime.now())
            # 清理过期条目
            now = datetime.now()
            expired_keys = [k for k, (_, t) in self._income_cache.items()
                           if (now - t).total_seconds() >= self._income_cache_ttl]
            for k in expired_keys:
                del self._income_cache[k]
            # 条目上限保护
            if len(self._income_cache) > self._income_cache_max:
                oldest_key = min(self._income_cache, key=lambda k: self._income_cache[k][1])
                del self._income_cache[oldest_key]
        return data or []

    async def _get_commission_data(self, start_ms: int, end_ms: int) -> list:
        """获取佣金数据，按 symbol 聚合

        Binance PM 账户中，COMMISSION 类型记录交易手续费支出（负值）。
        用此数据计算各策略的佣金支出，并从毛利润中扣除得到净利润。

        Args:
            start_ms: 起始时间戳（毫秒）
            end_ms: 结束时间戳（毫秒）

        Returns:
            income 记录列表（COMMISSION 类型）
        """
        return await self._get_income_data(start_ms, end_ms, income_type="COMMISSION")

    async def _get_hrs_symbols(self) -> set:
        """从DB获取HRS策略交易过的币种（排除条件单）"""
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT DISTINCT symbol FROM trading.trade_records "
                "WHERE strategy = 'HRS策略' "
                "AND order_type NOT IN ('STOP', 'TAKE_PROFIT', 'STOP_MARKET', 'TAKE_PROFIT_MARKET')"
            )
            return {row["symbol"] for row in rows}
        except Exception as e:
            logger.warning("HRS策略币种查询失败", error=str(e)[:80])
            return set()

    async def _get_pnl_by_strategy(self, start_time, end_time):
        """通过 Binance API 获取各策略的净盈亏（已扣佣金，使用 income 缓存）

        REALIZED_PNL 是毛利润（不含佣金），COMMISSION 是交易手续费支出（负值）。
        净盈亏 = REALIZED_PNL + COMMISSION。

        Returns:
            dict: {strategy_key: {wins, losses, total_pnl, total_commission, net_pnl}}
        """
        start_bj = start_time.replace(tzinfo=BEIJING_TZ)
        end_bj = end_time.replace(tzinfo=BEIJING_TZ)
        start_ms = int(start_bj.timestamp() * 1000)
        end_ms = int(end_bj.timestamp() * 1000)

        result = {}
        strategy_keys = list(self._STRATEGY_KEY_MAP.values())
        for key in strategy_keys:
            result[key] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "total_commission": 0.0, "net_pnl": 0.0}

        # 并行获取毛利润和佣金数据
        income_list, commission_list = await asyncio.gather(
            self._get_income_data(start_ms, end_ms, income_type="REALIZED_PNL"),
            self._get_commission_data(start_ms, end_ms),
        )

        # 获取HRS策略币种列表，用于区分HRS和新币做空策略
        hrs_symbols = await self._get_hrs_symbols()

        def _map_strategy(symbol: str) -> str:
            """将 symbol 映射到策略 key"""
            for key, symbols in self._STRATEGY_SYMBOLS.items():
                if symbol.upper() in symbols:
                    return key
            if symbol in hrs_symbols:
                return "hrs"
            return "new_coin"

        # 处理毛利润（REALIZED_PNL）
        for entry in income_list:
            symbol = entry.get("symbol", "")
            income_str = entry.get("income", "0")
            try:
                income = float(income_str)
            except (ValueError, TypeError):
                continue
            strategy_key = _map_strategy(symbol)
            if income > 0:
                result[strategy_key]["wins"] += 1
            elif income < 0:
                result[strategy_key]["losses"] += 1
            result[strategy_key]["total_pnl"] += income

        # 处理佣金（COMMISSION）
        for entry in commission_list:
            symbol = entry.get("symbol", "")
            income_str = entry.get("income", "0")
            try:
                commission = float(income_str)
            except (ValueError, TypeError):
                continue
            strategy_key = _map_strategy(symbol)
            result[strategy_key]["total_commission"] += commission

        # 计算净盈亏 = 毛利润 + 佣金（佣金为负值，自动扣减）
        for key in strategy_keys:
            result[key]["net_pnl"] = result[key]["total_pnl"] + result[key]["total_commission"]

        return result

    async def _get_fills_concurrent(self, symbols_with_key: list, start_ms: int, end_ms: int) -> Dict[str, int]:
        """并发获取多个币种的成交数（asyncio.gather + Semaphore 限流）

        自动处理 Binance allOrders 7天时间窗口限制：
        当时间跨度超过 7 天时，拆分为多个 7 天窗口分别查询后合并结果。

        Args:
            symbols_with_key: [(strategy_key, symbol), ...] 查询列表
            start_ms: 起始时间戳（毫秒）
            end_ms: 结束时间戳（毫秒）

        Returns:
            Dict[str, int]: {symbol: fill_count} 按 symbol 返回成交数
        """
        sem = asyncio.Semaphore(self._api_concurrency)

        MAX_WINDOW_MS = 7 * 24 * 3600 * 1000  # 7天（毫秒）

        def _split_windows(s: int, e: int):
            """将 [s, e) 时间范围拆分为多个 7 天窗口"""
            windows = []
            cur = s
            while cur < e:
                window_end = min(cur + MAX_WINDOW_MS, e)
                windows.append((cur, window_end))
                cur = window_end
            return windows

        async def _query_one_symbol(symbol: str) -> int:
            """查询单个币种的全部成交数（自动拆分时间窗口）"""
            total = 0
            windows = _split_windows(start_ms, end_ms)
            for ws, we in windows:
                async with sem:
                    try:
                        orders = await self._binance_client.get_order_history(
                            symbol=symbol,
                            start_time=ws,
                            end_time=we,
                            limit=self._ORDER_HISTORY_LIMIT,
                        )
                        for o in orders:
                            update_time = o.get("updateTime", o.get("time", 0))
                            if o.get("status", "") == "FILLED" and ws <= update_time <= we:
                                total += 1
                    except Exception as e:
                        logger.warning("获取成交数失败", symbol=symbol, error=str(e)[:80])
            return total

        # 构建并发任务列表（按 symbol 去重，避免 ETHUSDT 等重复查询）
        seen_symbols = set()
        unique_tasks = []
        unique_symbols = []
        for key, symbol in symbols_with_key:
            if symbol not in seen_symbols:
                seen_symbols.add(symbol)
                unique_tasks.append(_query_one_symbol(symbol))
                unique_symbols.append(symbol)

        counts = await asyncio.gather(*unique_tasks)

        # 按 symbol 汇总（同一 symbol 可能属于不同策略）
        symbol_counts = dict(zip(unique_symbols, counts))
        result: Dict[str, int] = {}
        for key, symbol in symbols_with_key:
            result[symbol] = result.get(symbol, 0) + symbol_counts.get(symbol, 0)
        return result

    async def _get_fill_count_by_strategy(self, start_time, end_time):
        """并发获取各策略的成交笔数"""
        start_bj = start_time.replace(tzinfo=BEIJING_TZ)
        end_bj = end_time.replace(tzinfo=BEIJING_TZ)
        start_ms = int(start_bj.timestamp() * 1000)
        end_ms = int(end_bj.timestamp() * 1000)

        # 构建查询列表：(strategy_key, symbol)
        symbols_with_key = []
        for key, symbols in self._STRATEGY_SYMBOLS.items():
            for symbol in symbols:
                symbols_with_key.append((key, symbol))

        # 补齐 new_coin 动态币种（DB + income API 双数据源）
        try:
            new_coin_symbols = await self._db_manager.fetch_all(
                "SELECT DISTINCT symbol FROM trading.trade_records "
                "WHERE strategy = '新币做空策略' "
                "AND order_type NOT IN ('STOP', 'TAKE_PROFIT', 'STOP_MARKET', 'TAKE_PROFIT_MARKET') "
                "AND executed_at >= $1 AND executed_at <= $2",
                start_time, end_time
            )
            for row in new_coin_symbols:
                symbols_with_key.append(("new_coin", row["symbol"]))
        except Exception as e:
            logger.warning("new_coin 币种查询失败", error=str(e)[:80])

        # 补齐 HRS 动态币种（DB 双数据源）
        try:
            hrs_symbols = await self._db_manager.fetch_all(
                "SELECT DISTINCT symbol FROM trading.trade_records "
                "WHERE strategy = 'HRS策略' "
                "AND order_type NOT IN ('STOP', 'TAKE_PROFIT', 'STOP_MARKET', 'TAKE_PROFIT_MARKET') "
                "AND executed_at >= $1 AND executed_at <= $2",
                start_time, end_time
            )
            for row in hrs_symbols:
                symbols_with_key.append(("hrs", row["symbol"]))
        except Exception as e:
            logger.warning("HRS 币种查询失败", error=str(e)[:80])

        # 从 income API 补充动态币种（处理今天没下单但有平仓的情况）
        hrs_symbols_all = await self._get_hrs_symbols()
        income_list = await self._get_income_data(start_ms, end_ms)
        for entry in income_list:
            sym = entry.get("symbol", "")
            # 判断是否属于 btc_eth（固定币种列表）
            is_known = False
            for key, symbols in self._STRATEGY_SYMBOLS.items():
                if sym.upper() in symbols:
                    is_known = True
                    break
            if is_known:
                continue
            # 判断是否属于 HRS
            if sym in hrs_symbols_all:
                if ("hrs", sym) not in symbols_with_key:
                    symbols_with_key.append(("hrs", sym))
            else:
                # 默认归为 new_coin
                if ("new_coin", sym) not in symbols_with_key:
                    symbols_with_key.append(("new_coin", sym))

        # 并发查询，获取 symbol 级别结果
        symbol_fills = await self._get_fills_concurrent(symbols_with_key, start_ms, end_ms)

        # 按 strategy_key 汇总
        result: Dict[str, int] = {}
        for key, symbol in symbols_with_key:
            result[key] = result.get(key, 0) + symbol_fills.get(symbol, 0)

        # 确保所有策略都有条目
        for key in self._STRATEGY_KEY_MAP.values():
            if key not in result:
                result[key] = 0

        return result

    async def get_overview(self, report_type: str = "daily"):
        await self._ensure_initialized()
        start_time, end_time = self._get_date_range(report_type)

        # 1. 从数据库获取订单数（按策略聚合，查询完整时间范围，避免只查单天）
        # 包含所有订单类型（含条件单），与 closed_count（来自 Binance API income 数据）
        # 保持数据源口径一致
        order_rows = await self._db_manager.fetch_all(
            "SELECT strategy, COUNT(*) as trade_count "
            "FROM trading.trade_records "
            "WHERE executed_at >= $1 AND executed_at <= $2 "
            "GROUP BY strategy",
            start_time, end_time
        )
        # 转换为 { strategy_key: trade_count } 格式
        unified_stats = {}
        for row in order_rows:
            key = self._STRATEGY_KEY_MAP.get(row["strategy"], row["strategy"])
            unified_stats[key] = {"trade_count": row["trade_count"]}

        # 2. 从 Binance API 获取盈亏
        pnl_stats = await self._get_pnl_by_strategy(start_time, end_time)

        # 3. 从 Binance API 获取成交数
        fill_stats = await self._get_fill_count_by_strategy(start_time, end_time)

        strategies_data = []
        total_net_pnl = 0.0
        total_gross_pnl = 0.0
        total_commission = 0.0
        total_orders = 0
        total_closed = 0
        total_wins = 0

        strategy_keys = list(self._STRATEGY_KEY_MAP.values())
        for strategy_key in strategy_keys:
            strategy_name = self._STRATEGY_NAME_MAP.get(strategy_key, strategy_key)
            order_count = unified_stats.get(strategy_key, {}).get("trade_count", 0)

            pnl = pnl_stats.get(strategy_key, {"wins": 0, "losses": 0, "total_pnl": 0.0, "total_commission": 0.0, "net_pnl": 0.0})
            fill_count = fill_stats.get(strategy_key, 0)
            closed_count = pnl["wins"] + pnl["losses"]
            gross_pnl = pnl["total_pnl"]          # 毛利润（不含佣金）
            commission = pnl["total_commission"]   # 佣金（负值，即支出）
            net_pnl = pnl["net_pnl"]               # 净盈亏（已扣佣金）
            win_count = pnl["wins"]

            total_net_pnl += net_pnl
            total_gross_pnl += gross_pnl
            total_commission += commission
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
                "total_pnl": f"{net_pnl:.4f}",          # 前端显示净盈亏
                "gross_pnl": f"{gross_pnl:.4f}",         # 毛利润（参考）
                "commission": f"{commission:.4f}",        # 佣金支出（负值）
                "win_rate": round(win_count / closed_count * 100, 1) if closed_count > 0 else 0.0,
                "report_type": report_type,
                "updated_at": datetime.now(BEIJING_TZ).isoformat(),
            })

        win_rate = (total_wins / total_closed * 100) if total_closed > 0 else 0

        return {
            "total_pnl": f"{total_net_pnl:.4f}",          # 总净盈亏（已扣佣金）
            "total_gross_pnl": f"{total_gross_pnl:.4f}",   # 总毛利润
            "total_commission": f"{total_commission:.4f}",  # 总佣金支出（负值）
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
        """获取策略币种明细（支持 DB + income API 双数据源）"""
        await self._ensure_initialized()
        start_time, end_time = self._get_date_range(report_type)

        strategy_name = self._STRATEGY_NAME_MAP.get(strategy_id, "")
        if not strategy_name:
            return []

        start_bj = start_time.replace(tzinfo=BEIJING_TZ)
        end_bj = end_time.replace(tzinfo=BEIJING_TZ)
        start_ms = int(start_bj.timestamp() * 1000)
        end_ms = int(end_bj.timestamp() * 1000)

        # 一次性获取 PNL 和佣金数据，按 symbol 分组（复用 income 缓存）
        pnl_by_symbol: Dict[str, Dict] = {}
        income_list, commission_list = await asyncio.gather(
            self._get_income_data(start_ms, end_ms, income_type="REALIZED_PNL"),
            self._get_commission_data(start_ms, end_ms),
        )
        for entry in income_list:
            sym = entry.get("symbol", "")
            try:
                income = float(entry.get("income", 0))
            except (ValueError, TypeError):
                continue
            if sym not in pnl_by_symbol:
                pnl_by_symbol[sym] = {"pnl": 0.0, "commission": 0.0, "net_pnl": 0.0, "wins": 0, "losses": 0}
            pnl_by_symbol[sym]["pnl"] += income
            if income > 0:
                pnl_by_symbol[sym]["wins"] += 1
            elif income < 0:
                pnl_by_symbol[sym]["losses"] += 1

        # 处理佣金，按 symbol 聚合
        for entry in commission_list:
            sym = entry.get("symbol", "")
            try:
                comm = float(entry.get("income", 0))
            except (ValueError, TypeError):
                continue
            if sym not in pnl_by_symbol:
                pnl_by_symbol[sym] = {"pnl": 0.0, "commission": 0.0, "net_pnl": 0.0, "wins": 0, "losses": 0}
            pnl_by_symbol[sym]["commission"] += comm

        # 计算各币种净盈亏
        for sym_data in pnl_by_symbol.values():
            sym_data["net_pnl"] = sym_data["pnl"] + sym_data["commission"]

        # 从 DB 获取该策略的币种下单数
        rows = await self._db_manager.fetch_all(
            "SELECT symbol, COUNT(*) as order_count "
            "FROM trading.trade_records "
            "WHERE strategy = $1 AND executed_at >= $2 AND executed_at <= $3 "
            "GROUP BY symbol ORDER BY order_count DESC",
            strategy_name, start_time, end_time
        )
        db_symbols = {row["symbol"]: row["order_count"] for row in rows}

        # 确定该策略的币种列表：DB 记录 + income API 中属于该策略的币种
        # 策略归属逻辑与 _get_pnl_by_strategy 一致：btc_eth → hrs → new_coin
        hrs_symbols_all = await self._get_hrs_symbols() if strategy_id in ("hrs", "new_coin") else set()
        strategy_symbols = set(db_symbols.keys())
        for sym, pnl_data in pnl_by_symbol.items():
            # 判断该 symbol 是否属于当前策略
            sym_strategy = None
            for key, symbols in self._STRATEGY_SYMBOLS.items():
                if sym.upper() in symbols:
                    sym_strategy = key
                    break
            if sym_strategy is None:
                if sym in hrs_symbols_all:
                    sym_strategy = "hrs"
                else:
                    sym_strategy = "new_coin"
            if sym_strategy == strategy_id:
                strategy_symbols.add(sym)

        if not strategy_symbols:
            return []

        # 并发获取各币种成交数
        symbols_to_query = [(strategy_id, sym) for sym in strategy_symbols]
        fill_counts = await self._get_fills_concurrent(symbols_to_query, start_ms, end_ms)

        symbols = []
        for sym in strategy_symbols:
            order_count = db_symbols.get(sym, 0)
            pnl_data = pnl_by_symbol.get(sym, {"pnl": 0.0, "commission": 0.0, "net_pnl": 0.0, "wins": 0, "losses": 0})
            wins = pnl_data["wins"]
            losses = pnl_data["losses"]
            fill_count = fill_counts.get(sym, 0)
            closed_count = wins + losses
            win_rate = round(wins / closed_count * 100, 1) if closed_count > 0 else 0.0

            symbols.append({
                "symbol": sym,
                "order_count": order_count,
                "fill_count": fill_count,
                "wins": wins,
                "losses": losses,
                "closed_count": closed_count,
                "total_pnl": f"{pnl_data['net_pnl']:.4f}",  # 净盈亏（已扣佣金）
                "gross_pnl": f"{pnl_data['pnl']:.4f}",       # 毛利润
                "commission": f"{pnl_data['commission']:.4f}",  # 佣金支出
                "win_rate": win_rate,
                "data_quality": "ok",
                "quality_note": "",
            })

        # 按 order_count 降序排序
        symbols.sort(key=lambda x: x["order_count"], reverse=True)
        return symbols

    async def get_trend_data(self, report_type: str = "daily", days: int = 7):
        await self._ensure_initialized()
        now = datetime.now(BEIJING_TZ)

        # 计算整体时间范围（覆盖所有数据点）
        if report_type == "daily":
            first_day = now.date() - timedelta(days=days - 1)
            overall_start = datetime.combine(first_day, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
        elif report_type == "weekly":
            today = now.date()
            today_weekday = today.weekday()
            days_to_last_sunday = 0 if today_weekday == 6 else (today_weekday + 1)
            last_sunday = today - timedelta(days=days_to_last_sunday)
            first_week_monday = (last_sunday - timedelta(weeks=days - 2)) - timedelta(days=6) if days > 1 else (now.date() - timedelta(days=now.weekday()))
            overall_start = datetime.combine(first_week_monday, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
        else:
            first_month = now.replace(day=1)
            for _ in range(days - 1):
                prev_month_end = first_month - timedelta(days=1)
                first_month = prev_month_end.replace(day=1)
            overall_start = datetime.combine(first_month, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)

        overall_end = now.replace(tzinfo=None)

        # 1次 income API 获取全部数据（毛利润 + 佣金）
        overall_start_ms = int(overall_start.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        overall_end_ms = int(overall_end.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        income_list, commission_list = await asyncio.gather(
            self._get_income_data(overall_start_ms, overall_end_ms, income_type="REALIZED_PNL"),
            self._get_commission_data(overall_start_ms, overall_end_ms),
        )

        # 将佣金数据合并到 income 列表中，用于计算净盈亏
        # 策略：按时间将佣金归入对应时间段的 PnL
        income_list = list(income_list) + list(commission_list)

        # 1次 DB 查询按日期分组
        order_rows = await self._db_manager.fetch_all(
            "SELECT DATE(executed_at AT TIME ZONE 'Asia/Shanghai') as trade_date, COUNT(*) as order_count "
            "FROM trading.trade_records "
            "WHERE executed_at >= $1 AND executed_at <= $2 "
            "GROUP BY trade_date ORDER BY trade_date",
            overall_start, overall_end
        )
        order_by_date = {str(row["trade_date"]): row["order_count"] for row in order_rows}

        # 内存中按时间段切片
        trends = []
        for i in range(days):
            if report_type == "daily":
                day = now.date() - timedelta(days=days - 1 - i)
                start = datetime.combine(day, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                if i == days - 1:
                    end = now.replace(tzinfo=None)
                else:
                    end = datetime.combine(day, datetime.max.time().replace(microsecond=0), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                date_str = day.strftime("%m/%d")
                date_key = day.isoformat()
            elif report_type == "weekly":
                today = now.date()
                today_weekday = today.weekday()
                days_to_last_sunday = 0 if today_weekday == 6 else (today_weekday + 1)
                last_sunday = today - timedelta(days=days_to_last_sunday)
                if i < days - 1:
                    # 历史完整周
                    week_sunday = last_sunday - timedelta(weeks=days - 2 - i)
                    week_monday = week_sunday - timedelta(days=6)
                    start = datetime.combine(week_monday, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                    end = datetime.combine(week_sunday, datetime.max.time().replace(microsecond=0), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                else:
                    # 本周（实时）
                    week_monday = now.date() - timedelta(days=now.weekday())
                    start = datetime.combine(week_monday, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                    end = now.replace(tzinfo=None)
                    week_sunday = week_monday + timedelta(days=6)
                date_str = f"{start.strftime('%m/%d')}-{end.strftime('%m/%d')}"
                date_key = None  # 周视图不用 date_key 查 DB
            else:  # monthly
                if i < days - 1:
                    # 历史完整月
                    month_end = now.replace(day=1) - timedelta(days=1)
                    for _ in range(days - 2 - i):
                        month_end = (month_end.replace(day=1) - timedelta(days=1))
                    month_start = month_end.replace(day=1)
                    start = datetime.combine(month_start, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                    end = datetime.combine(month_end, datetime.max.time().replace(microsecond=0), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                else:
                    # 本月（实时）
                    start = datetime(now.year, now.month, 1, tzinfo=BEIJING_TZ).replace(tzinfo=None)
                    end = now.replace(tzinfo=None)
                    month_start = start
                date_str = start.strftime("%Y/%m")
                date_key = None

            # 从预获取的 income 数据中按时间切片
            start_ms = int(start.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
            end_ms = int(end.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
            total_pnl = 0.0
            wins = 0
            losses = 0
            for entry in income_list:
                entry_time = entry.get("time", 0)
                if start_ms <= entry_time <= end_ms:
                    try:
                        income = float(entry.get("income", 0))
                        total_pnl += income
                        if income > 0:
                            wins += 1
                        elif income < 0:
                            losses += 1
                    except (ValueError, TypeError):
                        pass

            # 从 DB 结果中获取订单数
            if report_type == "daily" and date_key:
                total_orders = order_by_date.get(date_key, 0)
            else:
                # 周/月视图需要聚合多天
                total_orders = 0
                for dk, cnt in order_by_date.items():
                    dk_date = datetime.strptime(dk, "%Y-%m-%d").date()
                    dk_start = datetime.combine(dk_date, datetime.min.time(), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                    if start <= dk_start <= end:
                        total_orders += cnt

            closed_count = wins + losses
            win_rate = round(wins / closed_count * 100, 1) if closed_count > 0 else 0.0

            trends.append({
                "date": date_str,
                "total_pnl": f"{total_pnl:.4f}",
                "order_count": total_orders,
                "win_rate": win_rate,
            })

        return trends