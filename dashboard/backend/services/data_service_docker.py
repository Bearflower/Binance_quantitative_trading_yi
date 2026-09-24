#!/usr/bin/env python3
"""
Dashboard 数据服务（Docker容器版本）
使用数据库查询订单数，通过 Binance API 获取盈亏数据
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
import asyncio
import json
import os
import sys

import structlog

# 添加项目根目录到Python路径
sys.path.insert(0, os.getenv("APP_ROOT", "/app"))

from shared.database import DatabaseManager
from shared.binance_api import BinanceClient
from shared.trade_logger import TradeLogger
from core.config import risk_settings

logger = structlog.get_logger()
BEIJING_TZ = timezone(timedelta(hours=8))


class DataService:
    """真实数据服务"""

    _STRATEGY_KEY_MAP = {
        "MTPCS策略": "btc_eth",
        "MTPCS激进策略": "btc_eth_aggressive",
        "MTPCS激进版": "btc_eth_aggressive",
        "新币做空策略": "new_coin",
        "HRS策略": "hrs",
        "网格策略": "grid",
    }

    _STRATEGY_NAME_MAP = {v: k for k, v in _STRATEGY_KEY_MAP.items()}

    # 共用同一分配额度的策略组（value 为资金归属的策略 id）：
    # 激进版(btc_eth_aggressive)已独立参与月度资金分配，不再与原版(btc_eth)合并占用比，
    # 因此该映射为空（各策略按自身 id 独立统计占用比）。
    _STRATEGY_ALLOCATION_GROUP = {}

    @classmethod
    def _allocation_group_ids(cls, strategy_id) -> list:
        """返回某策略及其共用同一分配额度的全部策略 id（含自身）

        用于月度资金分配占用比统计。当前 _STRATEGY_ALLOCATION_GROUP 为空，
        各策略独立参与分配，因此直接返回 [strategy_id]。
        """
        root = cls._STRATEGY_ALLOCATION_GROUP.get(strategy_id, strategy_id)
        return [root] + [k for k, v in cls._STRATEGY_ALLOCATION_GROUP.items() if v == root]

    @staticmethod
    def _normalize_strategy_id(raw) -> Optional[str]:
        """把各数据源的策略标识归一化为规范 id

        strategy_open_positions / trade_records 可能存中文名（如 HRS策略），
        而月度分配与概览使用规范 id（如 hrs）。统一经映射归并，避免同名持仓分裂。
        """
        if not raw:
            return None
        s = str(raw).strip()
        return DataService._STRATEGY_KEY_MAP.get(s, s)  # 未知标识原样保留，避免丢持仓

    @staticmethod
    def _db_strategy_names(strategy_id: str) -> list:
        """返回某策略 id 在 DB 中的全部落库中文名（去重保序）

        _STRATEGY_KEY_MAP 允许一个策略 id 对应多个中文名（如激进版在 DB 落库为
        "MTPCS激进策略"、但用户也叫"MTPCS激进版"），查询 trade_records 时必须按
        全部落库名匹配，避免遗漏或与展示名不一致。
        """
        names = [k for k, v in DataService._STRATEGY_KEY_MAP.items() if v == strategy_id]
        return list(dict.fromkeys(names))

    _STRATEGY_SYMBOLS = {
        "btc_eth": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "TRXUSDT"],
    }

    _STRATEGY_EMOJI_MAP = {
        "btc_eth": "📈",
        "btc_eth_aggressive": "⚡",
        "new_coin": "📉",
        "hrs": "🔄",
    }

    # trade_records 盈亏/委托/成交聚合共用列（策略级 _query_stats_rows 与币种级
    # get_strategy_symbols 同源引用，保证"概览 == 详情"口径单源，杜绝跨策略污染）
    _TRADE_RECORD_AGG_COLUMNS = """\
                   COUNT(*) AS order_count,
                   COUNT(realized_pnl) AS closed_count,
                   COUNT(*) FILTER (WHERE realized_pnl > 0) AS wins,
                   COUNT(*) FILTER (WHERE realized_pnl < 0) AS losses,
                   COALESCE(SUM(realized_pnl), 0) AS gross_pnl,
                   COALESCE(SUM(commission), 0) AS commission"""

    _ORDER_HISTORY_LIMIT = int(os.getenv("ORDER_HISTORY_LIMIT", "1000"))

    def __init__(self):
        self._db_manager = None
        self._binance_client = None
        self._trade_logger = None
        self._initialized = False
        # 初始化互斥锁：预热与多个定时任务可能并发触发 _ensure_initialized，
        # 无锁会导致重复创建 DatabaseManager/BinanceClient 与连接池泄漏。
        self._init_lock = asyncio.Lock()
        self._income_cache = {}
        self._income_cache_ttl = int(os.getenv("INCOME_CACHE_TTL", "30"))  # income 缓存秒数
        self._income_cache_max = int(os.getenv("INCOME_CACHE_MAX", "20"))  # 缓存条目上限
        self._api_concurrency = int(os.getenv("API_CONCURRENCY", "5"))  # API 并发限制
        self._income_lock = asyncio.Lock()  # 防止缓存惊群
        # 预计算落库配置（均走环境变量，禁止硬编码）
        self._precompute_retrospect_days = int(os.getenv("PRECOMPUTE_RETROSPECT_DAYS", "120"))  # 回溯天数
        self._precompute_income_slice_days = int(os.getenv("PRECOMPUTE_INCOME_SLICE_DAYS", "10"))  # income 拉取分片窗口天数
        self._freshness_timeout = int(os.getenv("PRECOMPUTE_FRESHNESS_TIMEOUT", "180"))  # 快照新鲜度超时(秒)
        # 持仓/占用对账快照配置（均走环境变量，禁止硬编码）
        self._position_freshness_timeout = int(os.getenv("POSITION_FRESHNESS_TIMEOUT", "600"))  # 持仓快照新鲜度超时(秒)，须大于对账间隔

    async def _ensure_initialized(self):
        if self._initialized:
            return
        # 串行化初始化：多个定时任务并发触发时只允许一个协程执行建连，
        # 其余协程在锁上等待后检查 _initialized 直接返回，避免重复建连/泄漏。
        async with self._init_lock:
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
        elif report_type == "yearly":
            start = datetime(now.year, 1, 1, tzinfo=BEIJING_TZ).replace(tzinfo=None)
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

    async def _get_income_data_paginated(self, start_ms: int, end_ms: int,
                                         income_type: str = "REALIZED_PNL",
                                         slice_days: Optional[int] = None) -> list:
        """分页拉取 income 数据，避免单次超 1000 条导致记录被截断

        背景：Binance /papi/v1/um/income 单次最多返回 1000 条，且不支持 fromId 分页，
        仅靠 startTime/endTime + limit 定位。当时间跨度较大（如回溯 120 天）时，
        收入+佣金记录数远超 1000 条，Binance 只会从 startTime 起返回最早的 1000 条，
        导致最近（9月）的 income 记录被截断缺失。此处按 slice_days 拆成多个小窗口
        分别拉取后合并，任何窗口内记录数均低于 1000 条，从而覆盖全量。

        Args:
            start_ms: 起始时间戳（毫秒）
            end_ms: 结束时间戳（毫秒）
            income_type: 收入类型，默认 "REALIZED_PNL"，也支持 "COMMISSION"
            slice_days: 分片窗口天数，默认取 self._precompute_income_slice_days

        Returns:
            全部窗口合并后的 income 记录列表（某窗口失败仅跳过，全部失败返回 []）
        """
        if slice_days is None:
            slice_days = self._precompute_income_slice_days
        if slice_days <= 0:
            slice_days = self._precompute_income_slice_days
        slice_ms = slice_days * 24 * 3600 * 1000
        windows = [(s, min(s + slice_ms, end_ms))
                   for s in range(start_ms, end_ms, slice_ms)]

        sem = asyncio.Semaphore(self._api_concurrency)

        async def _fetch_window(ws: int, we: int) -> list:
            """拉取单个窗口的 income 记录（复用 _get_income_data 缓存与锁）"""
            async with sem:
                try:
                    return await self._get_income_data(ws, we, income_type=income_type)
                except Exception as e:
                    logger.warning("分页拉取 income 窗口失败，跳过该窗口",
                                   income_type=income_type, window_ms=(ws, we), error=str(e)[:80])
                    return []

        results = await asyncio.gather(*(_fetch_window(ws, we) for ws, we in windows))
        merged: list = []
        for recs in results:
            merged.extend(recs)
        return merged

    async def get_account_equity(self) -> dict:
        """获取合约账户净资产（实时快照）

        使用 Binance PM 账户的 accountEquity（账户权益，含未实现盈亏），
        作为"合约账户净资产"展示在首页。该值为当前实时快照，不随日/周/月切换变化。
        同时获取可用余额与当前非零持仓数，用于净资产卡的补充信息展示。

        Returns:
            dict: {
                "total_equity": str,
                "available_balance": str,
                "open_positions": int,
                "updated_at": str
            }
        """
        await self._ensure_initialized()
        try:
            account_info = await self._binance_client.get_account_info()
            eq = float(account_info.get("totalMarginBalance", account_info.get("totalWalletBalance", 0)))
            avail = float(account_info.get("availableBalance", 0))
        except Exception as e:
            logger.warning("获取账户净资产失败", error=str(e)[:80])
            eq = 0.0
            avail = 0.0

        # 当前非零持仓数（通过持仓风险接口统计）
        open_positions = 0
        try:
            positions = await self._binance_client.get_position()
            open_positions = sum(
                1 for p in positions if abs(float(p.get("positionAmt", 0.0))) > 1e-8
            )
        except Exception as e:
            logger.warning("获取当前持仓数失败", error=str(e)[:80])

        return {
            "total_equity": f"{eq:.2f}",
            "available_balance": f"{avail:.2f}",
            "open_positions": open_positions,
            "updated_at": datetime.now(BEIJING_TZ).isoformat(),
        }

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

    async def _query_stats_rows(self, start_time, end_time) -> list:
        """查询 trade_records 按 (策略, 北京日) 与全量 北京日 两级聚合行（策略级 + total 级）

        SQL 与详情页 get_strategy_symbols 同源（realized_pnl/commission 字段），
        GROUPING SETS 同时产出策略级与 total 级（strategy 为 NULL 的行即 total）。
        失败返回空列表，由调用方兜底为全 0。

        Args:
            start_time, end_time: 北京时间 naive datetime

        Returns:
            list[dict]: strategy/td/order_count/closed_count/wins/losses/gross_pnl/commission
        """
        try:
            return await self._db_manager.fetch_all(
                f"""
                SELECT strategy,
                       DATE(executed_at) AS td,
                       {self._TRADE_RECORD_AGG_COLUMNS}
                FROM trading.trade_records
                WHERE executed_at >= $1 AND executed_at <= $2
                GROUP BY GROUPING SETS ((strategy, td), (td))
                """,
                start_time, end_time
            )
        except Exception as e:
            logger.warning("策略级落库聚合查询失败", error=str(e)[:80])
            return []

    @staticmethod
    def _new_stats_cell() -> dict:
        """新建空策略级聚合单元（与预计算聚合单元同构，fill 与委托同口径）"""
        return {"net_pnl": 0.0, "gross_pnl": 0.0, "commission": 0.0,
                "wins": 0, "losses": 0, "fill_count": 0, "order_count": 0}

    @staticmethod
    def _fold_stats_cell(cell, gross, comm, cnt, wins, losses) -> None:
        """把一行聚合值累加进目标 cell

        net = realized_pnl + commission（佣金负值自动扣减）；
        成交数 = 委托数（DB 落库即视为已成交，与详情页口径一致）。
        """
        cell["gross_pnl"] += gross
        cell["commission"] += comm
        cell["net_pnl"] += gross + comm
        cell["wins"] += wins
        cell["losses"] += losses
        cell["order_count"] += cnt
        cell["fill_count"] += cnt

    def _fold_stats_rows(self, rows) -> tuple:
        """把聚合行折叠为策略级与全量级的按日 cell（预计算 day 级用）

        Returns:
            (strategy_daily, total_daily)
            strategy_daily: {(strategy_key, 北京日): cell}（仅已映射落库名，未映射不计）
            total_daily: {北京日: cell}（trade_records 全量，含未映射策略）
        """
        strategy_daily = {}
        total_daily = {}
        for r in rows:
            d = r.get("td")
            if d is None:
                continue
            gross = float(r["gross_pnl"])
            comm = float(r["commission"])
            cnt = int(r["order_count"])
            wins = int(r["wins"])
            losses = int(r["losses"])
            if r.get("strategy") is None:
                self._fold_stats_cell(
                    total_daily.setdefault(d, self._new_stats_cell()),
                    gross, comm, cnt, wins, losses)
                continue
            key = self._STRATEGY_KEY_MAP.get(r["strategy"])
            if key:
                self._fold_stats_cell(
                    strategy_daily.setdefault((key, d), self._new_stats_cell()),
                    gross, comm, cnt, wins, losses)
        return strategy_daily, total_daily

    async def _get_strategy_summary(self, start_time, end_time) -> tuple:
        """策略级与全量汇总（trade_records 真实落库名，唯一权威口径，供实时兜底）

        Returns:
            (summary, total)
            summary: {strategy_key: cell}；total: cell（trade_records 全量）
        """
        rows = await self._query_stats_rows(start_time, end_time)
        strategy_daily, total_daily = self._fold_stats_rows(rows)
        summary = {}
        for (key, _d), cell in strategy_daily.items():
            self._fold_stats_cell(
                summary.setdefault(key, self._new_stats_cell()),
                cell["gross_pnl"], cell["commission"], cell["order_count"],
                cell["wins"], cell["losses"])
        total = self._new_stats_cell()
        for cell in total_daily.values():
            self._fold_stats_cell(
                total, cell["gross_pnl"], cell["commission"], cell["order_count"],
                cell["wins"], cell["losses"])
        return summary, total

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

    async def get_overview(self, report_type: str = "daily"):
        await self._ensure_initialized()
        # 读库快速路径（预计算落库，命中则直接返回；未命中/过期走实时兜底）
        metric = await self.get_overview_from_metric(report_type)
        if metric is not None:
            # 快照字段固定、不含浮动盈亏，此处补上实时未实现盈亏（实时查交易所持仓）
            metric["total_unrealized_pnl"] = f"{await self._query_unrealized_pnl():.4f}"
            return metric

        start_time, end_time = self._get_date_range(report_type)

        # 当前各策略持仓数与保证金（无上报数据时回 0 降级）
        open_positions_summary = await self.get_open_positions_summary()

        # 策略级盈亏/委托/成交统一按 trade_records 真实落库名聚合（唯一权威口径，
        # 与详情页 get_strategy_symbols、预计算 _fold_stats_rows 同源）。
        # summary: {strategy_key: cell}；total: 全量 cell（含未映射策略，保证一致性）
        summary, total = await self._get_strategy_summary(start_time, end_time)

        strategies_data = []
        strategy_keys = list(dict.fromkeys(self._STRATEGY_KEY_MAP.values()))  # 去重保序（多个中文名可映射同一策略 id）
        for strategy_key in strategy_keys:
            strategy_name = self._STRATEGY_NAME_MAP.get(strategy_key, strategy_key)
            cell = summary.get(strategy_key, self._new_stats_cell())

            order_count = cell["order_count"]
            fill_count = cell["fill_count"]
            closed_count = cell["wins"] + cell["losses"]
            gross_pnl = cell["gross_pnl"]           # 毛利润（不含佣金）
            commission = cell["commission"]         # 佣金（负值，即支出）
            net_pnl = cell["net_pnl"]               # 净盈亏（已扣佣金）
            win_count = cell["wins"]

            pos_info = open_positions_summary.get(strategy_key, {})

            strategies_data.append({
                "emoji": self._STRATEGY_EMOJI_MAP.get(strategy_key, ""),
                "id": strategy_key,
                "name": strategy_name,
                "open_position_count": pos_info.get("open_position_count", 0),
                "open_margin": f"{pos_info.get('open_margin', 0):.2f}",
                "order_count": order_count,
                "fill_count": fill_count,
                "closed_count": closed_count,
                "win_count": win_count,
                "loss_count": cell["losses"],
                "total_pnl": f"{net_pnl:.4f}",          # 前端显示净盈亏
                "gross_pnl": f"{gross_pnl:.4f}",         # 毛利润（参考）
                "commission": f"{commission:.4f}",        # 佣金支出（负值）
                "win_rate": round(win_count / closed_count * 100, 1) if closed_count > 0 else 0.0,
                "report_type": report_type,
                "updated_at": datetime.now(BEIJING_TZ).isoformat(),
            })

        # total 以 trade_records 全量聚合为准（比 Σ策略多出未映射策略，但字体口径一致），
        # 同时更新为"策略级自洽"口径：总量 = Σ策略汇总，保证 Σ策略 == 总量
        total_wins = total["wins"]
        total_closed = total["wins"] + total["losses"]
        win_rate = (total_wins / total_closed * 100) if total_closed > 0 else 0
        # 实时浮动盈亏（未实现盈亏）：总盈亏 = net_pnl(已实现) + unrealized_pnl(未实现)
        unrealized_pnl = await self._query_unrealized_pnl()

        return {
            "total_pnl": f"{total['net_pnl']:.4f}",          # 总净盈亏（已扣佣金，已实现）
            "total_gross_pnl": f"{total['gross_pnl']:.4f}",   # 总毛利润
            "total_commission": f"{total['commission']:.4f}",  # 总佣金支出（负值）
            "total_unrealized_pnl": f"{unrealized_pnl:.4f}",  # 浮动盈亏（未实现）
            "total_orders": total["order_count"],
            "total_closed": total_closed,
            "total_wins": total_wins,
            "win_rate": round(win_rate, 2),
            "strategies": strategies_data,
            "report_type": report_type,
            "updated_at": datetime.now(BEIJING_TZ).isoformat(),
        }

    async def _query_unrealized_pnl(self) -> float:
        """实时查询所有持仓的未实现盈亏总量（浮动盈亏）。

        币安 positionRisk 返回含 unrealizedProfit 字段（string），
        汇总所有非零持仓；失败时降级为 0（不影响主统计输出）。
        """
        try:
            positions = await self._binance_client.get_position()
            total_unrealized = 0.0
            for pos in positions:
                try:
                    # PM 账户 /papi/v1/um/positionRisk 返回 unRealizedProfit（大写 R），
                    # 常规合约 /fapi/v2/positionRisk 返回 unrealizedProfit（小写 r），兼容两者
                    raw = pos.get("unRealizedProfit", pos.get("unrealizedProfit", 0))
                    total_unrealized += float(raw or 0)
                except (TypeError, ValueError):
                    continue
            return round(total_unrealized, 4)
        except Exception as e:
            logger.warning("查询浮动盈亏失败，降级为 0", error=str(e)[:80])
            return 0.0

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

        # 统一口径（问题4）：详情页汇总指标必须与币种明细同源——
        # 汇总卡 order_count/closed_count/win_count/loss_count/pnl 由明细逐币种上卷求和，
        # 确保“汇总 == 明细求和”，避免概览聚合口径与明细归属不一致导致的矛盾。
        order_count = sum(s["order_count"] for s in symbols)
        closed_count = sum(s["closed_count"] for s in symbols)
        win_count = sum(s["wins"] for s in symbols)
        loss_count = sum(s["losses"] for s in symbols)
        net_pnl = sum(float(s["total_pnl"]) for s in symbols)
        gross_pnl_d = sum(float(s["gross_pnl"]) for s in symbols)
        commission_d = sum(float(s["commission"]) for s in symbols)

        base["order_count"] = order_count
        base["closed_count"] = closed_count
        base["win_count"] = win_count
        base["loss_count"] = loss_count
        base["total_pnl"] = f"{net_pnl:.4f}"
        base["gross_pnl"] = f"{gross_pnl_d:.4f}"
        base["commission"] = f"{commission_d:.4f}"
        base["win_rate"] = round(win_count / closed_count * 100, 1) if closed_count > 0 else 0.0

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
        """获取策略币种明细（统一以 DB trading.trade_records 为唯一口径）

        委托/成交/平仓/盈亏全部按真实落库策略名 + 币种聚合，杜绝此前"按币种全量归属
        income/成交"导致的跨策略污染——例如激进版(btc_eth_aggressive)与原始版共用币种，
        原版详情页却把激进版的 ETH/BNB 成交、盈亏误计入而产生"委托0 却有成交"的矛盾。

        口径约定：
          委托(order_count) = 该策略该币种全部落库下单次数
          成交(fill_count)  = 与委托一致（DB 落库即视为已成交）
          平仓(closed_count)= 已写入 realized_pnl 的平仓单数（未结算/持仓中不计入）
          盈亏(win/loss)    = 按平仓单 realized_pnl 正负各计一次
        """
        await self._ensure_initialized()
        start_time, end_time = self._get_date_range(report_type)

        strategy_names = self._db_strategy_names(strategy_id)
        if not strategy_names:
            return []

        rows = await self._db_manager.fetch_all(
            f"""
            SELECT symbol,
                   {self._TRADE_RECORD_AGG_COLUMNS}
            FROM trading.trade_records
            WHERE strategy = ANY($1::text[]) AND executed_at >= $2 AND executed_at <= $3
            GROUP BY symbol
            """,
            strategy_names, start_time, end_time
        )

        symbols = []
        for r in rows:
            gross = float(r["gross_pnl"])
            commission = float(r["commission"])
            closed = int(r["closed_count"])
            wins = int(r["wins"])
            losses = int(r["losses"])
            order_count = int(r["order_count"])
            symbols.append({
                "symbol": r["symbol"],
                "order_count": order_count,
                "fill_count": order_count,
                "wins": wins,
                "losses": losses,
                "closed_count": closed,
                "total_pnl": f"{gross + commission:.4f}",
                "gross_pnl": f"{gross:.4f}",
                "commission": f"{commission:.4f}",
                "win_rate": round(wins / closed * 100, 1) if closed > 0 else 0.0,
                "data_quality": "ok",
                "quality_note": "",
            })

        # 按 order_count 降序排序
        symbols.sort(key=lambda x: x["order_count"], reverse=True)
        return symbols

    async def get_trend_data(self, report_type: str = "daily", days: int = 7):
        # 读库快速路径（预计算落库，命中则直接返回；未命中/过期走实时兜底）
        metric = await self.get_trend_from_metric(report_type, days)
        if metric is not None:
            return metric

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
        elif report_type == "yearly":
            first_year = now.year - (days - 1)
            overall_start = datetime(first_year, 1, 1, tzinfo=BEIJING_TZ).replace(tzinfo=None)
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

        # 1次 DB 查询按日期分组（executed_at 落库即北京时间，直接取日期）
        order_rows = await self._db_manager.fetch_all(
            "SELECT DATE(executed_at) as trade_date, COUNT(*) as order_count "
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
            elif report_type == "yearly":
                year = now.year - (days - 1 - i)
                start = datetime(year, 1, 1, tzinfo=BEIJING_TZ).replace(tzinfo=None)
                if i == days - 1:
                    end = now.replace(tzinfo=None)
                else:
                    year_end = datetime(year + 1, 1, 1).date() - timedelta(days=1)
                    end = datetime.combine(year_end, datetime.max.time().replace(microsecond=0), tzinfo=BEIJING_TZ).replace(tzinfo=None)
                date_str = str(year)
                date_key = None  # 年视图按整年聚合，不用 date_key 查 DB
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

    # ========================================
    # 预计算落库（数据看板提速）
    #   后台定时将 Binance 实时聚合固化到 public.metric_snapshot，
    #   前端读库快速路径 <10ms，避免每请求实时拉 Binance（1-3s/接口）
    # ========================================

    _METRIC_UPSERT_SQL = """
    INSERT INTO public.metric_snapshot
        (granularity, scope, strategy_id, symbol, bucket_key, bucket_start, bucket_end, label,
         net_pnl, gross_pnl, commission, wins, losses, closed_count, fill_count, order_count, snapshot_at)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,CURRENT_TIMESTAMP)
    ON CONFLICT (granularity, scope, strategy_id, symbol, bucket_key) DO UPDATE SET
        bucket_end=EXCLUDED.bucket_end, net_pnl=EXCLUDED.net_pnl, gross_pnl=EXCLUDED.gross_pnl,
        commission=EXCLUDED.commission, wins=EXCLUDED.wins, losses=EXCLUDED.losses,
        closed_count=EXCLUDED.closed_count, fill_count=EXCLUDED.fill_count,
        order_count=EXCLUDED.order_count, snapshot_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
    """

    @staticmethod
    def _metric_granularity(report_type: str) -> str:
        """报告类型 → 预计算粒度（daily→day / weekly→week / monthly→month）"""
        return {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}.get(report_type, "day")

    @staticmethod
    def _naive(dt):
        """剥离时区得 naive datetime（与全文件 DB 存储风格一致）"""
        return dt.replace(tzinfo=None)

    def _resolve_strategy_key(self, symbol: str, hrs_symbols: set) -> str:
        """按现有归属逻辑判定 symbol 所属策略（btc_eth → hrs → new_coin）"""
        upper = symbol.upper()
        for key, symbols in self._STRATEGY_SYMBOLS.items():
            if upper in symbols:
                return key
        if symbol in hrs_symbols:
            return "hrs"
        return "new_coin"

    @staticmethod
    def _bucket_info(d) -> dict:
        """计算某日期在 day/week/month 三种粒度的 bucket_key 与 label"""
        wm = d - timedelta(days=d.weekday())
        ws = wm + timedelta(days=6)
        return {
            "day": {"key": d, "label": d.strftime("%m/%d")},
            "week": {"key": wm, "label": f"{wm.strftime('%m/%d')}-{ws.strftime('%m/%d')}"},
            "month": {"key": d.replace(day=1), "label": d.strftime("%Y/%m")},
            "year": {"key": d.replace(month=1, day=1), "label": d.strftime("%Y")},
        }

    @staticmethod
    def _bucket_end(granularity: str, bkey) -> datetime:
        """某粒度完整区间的结束时刻（进行中区间的结束实时取 now）"""
        day_end = datetime.max.time().replace(microsecond=0)
        if granularity == "day":
            return datetime.combine(bkey, day_end)
        if granularity == "week":
            return datetime.combine(bkey + timedelta(days=6), day_end)
        if granularity == "year":
            nxt = (bkey.replace(month=1, day=1) + timedelta(days=366)).replace(month=1, day=1)
            return datetime.combine(nxt - timedelta(days=1), day_end)
        nxt = (bkey.replace(day=28) + timedelta(days=4)).replace(day=1)
        return datetime.combine(nxt - timedelta(days=1), day_end)

    @staticmethod
    def _new_agg() -> dict:
        """新建空聚合单元（net/gross/commission/wins/losses/fill/order）"""
        return {
            "net_pnl": 0.0, "gross_pnl": 0.0, "commission": 0.0,
            "wins": 0, "losses": 0, "fill_count": 0, "order_count": 0,
        }

    @staticmethod
    def _merge(dst: dict, src: dict) -> dict:
        """原地累加 src 到 dst（用于 symbol→strategy→total 及 week/month 上卷）"""
        for k in ("net_pnl", "gross_pnl", "commission", "wins", "losses",
                  "fill_count", "order_count"):
            dst[k] += src[k]
        return dst

    def _extract_entry(self, entry: dict):
        """从收入记录解析 (symbol, 北京日期, income)，非法记录返回 (None,None,None)"""
        sym = entry.get("symbol", "")
        try:
            income = float(entry.get("income", 0))
        except (ValueError, TypeError):
            return None, None, None
        ts = entry.get("time", 0)
        try:
            day = datetime.fromtimestamp(ts / 1000, tz=BEIJING_TZ).date()
        except (ValueError, TypeError, OSError):
            return None, None, None
        return sym, day, income

    def _aggregate_day(self, income_list, commission_list, hrs_symbols) -> dict:
        """将 income/佣金归属到 symbol 级 day（供明细）；strategy/total 级随后由 _apply_orders_and_fills 以 trade_records 覆盖"""
        agg = {"symbol": {}, "strategy": {}, "total": {}}
        for entry in income_list:
            sym, d, income = self._extract_entry(entry)
            if sym is None:
                continue
            cell = agg["symbol"].setdefault(sym, {}).setdefault(d, self._new_agg())
            cell["gross_pnl"] += income
            cell["net_pnl"] += income
            if income > 0:
                cell["wins"] += 1
            elif income < 0:
                cell["losses"] += 1
        for entry in commission_list:
            sym, d, income = self._extract_entry(entry)
            if sym is None:
                continue
            cell = agg["symbol"].setdefault(sym, {}).setdefault(d, self._new_agg())
            cell["commission"] += income
            cell["net_pnl"] += income
        for sym, by_date in agg["symbol"].items():
            key = self._resolve_strategy_key(sym, hrs_symbols)
            for d, cell in by_date.items():
                self._merge(agg["strategy"].setdefault(key, {}).setdefault(d, self._new_agg()), cell)
                self._merge(agg["total"].setdefault(d, self._new_agg()), cell)
        return agg

    async def _compute_today_fills(self, agg, hrs_symbols, now) -> dict:
        """实时计算今天各 symbol 成交数（仅进行中区间，控制 Binance 调用成本）"""
        today = now.date()
        start = datetime.combine(today, datetime.min.time())
        start_ms = int(start.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        end_ms = int(now.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        symbols_with_key = []
        seen = set()
        for key, symbols in self._STRATEGY_SYMBOLS.items():
            for sym in symbols:
                if sym not in seen:
                    symbols_with_key.append((key, sym))
                    seen.add(sym)
        # 补充当天出现过的动态币种（income 归属）
        for sym in agg["symbol"]:
            if sym not in seen:
                symbols_with_key.append((self._resolve_strategy_key(sym, hrs_symbols), sym))
                seen.add(sym)
        # DB 当天交易补充（覆盖未平仓动态币种）
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT DISTINCT strategy, symbol FROM trading.trade_records "
                "WHERE executed_at >= $1 AND executed_at <= $2",
                start, now
            )
            for r in rows:
                key = self._STRATEGY_KEY_MAP.get(r["strategy"])
                if key and r["symbol"] not in seen:
                    symbols_with_key.append((key, r["symbol"]))
                    seen.add(r["symbol"])
        except Exception as e:
            logger.warning("预计算当天交易币种查询失败", error=str(e)[:80])
        return await self._get_fills_concurrent(symbols_with_key, start_ms, end_ms)

    async def _apply_orders_and_fills(self, agg, stats_daily, hrs_symbols, now):
        """以 trade_records 落库口经覆盖 strategy/total 级盈亏/委托/成交，实时补 today symbol 成交数

        strategy/total 级一律采用 trade_records 真实落库名聚合（_fold_stats_rows 结果），
        net/gross/commission/wins/losses/order/fill 全量覆盖，保证"Σ策略 == total"自洽，
        且与详情页 get_strategy_symbols 同源。symbol 级保留 income 归属（仅明细用）。
        """
        strategy_daily, total_daily = stats_daily
        # strategy_daily: {(strategy_key, 北京日): cell} → 转 {strategy_key: {北京日: cell}}
        agg["strategy"] = {}
        for (key, d), cell in strategy_daily.items():
            agg["strategy"].setdefault(key, {})[d] = dict(cell)
        agg["total"] = {d: dict(cell) for d, cell in total_daily.items()}
        # symbol 级 order/fill：DB 无 symbol 维日聚合，仅实时补 today 成交数
        today = now.date()
        fills = await self._compute_today_fills(agg, hrs_symbols, now)
        for sym, cnt in fills.items():
            agg["symbol"].setdefault(sym, {}).setdefault(today, self._new_agg())["fill_count"] = cnt

    @staticmethod
    def _iter_scope(agg, scope) -> dict:
        """返回某 scope 的聚合迭代容器（total 用空串占位 key）"""
        if scope == "total":
            return {"": agg["total"]}
        if scope == "strategy":
            return agg["strategy"]
        return agg["symbol"]

    def _make_record(self, granularity, scope, key, base_date, bucket, cell,
                     is_current, now) -> dict:
        """把聚合单元转成一条落库记录（衍生 closed_count，当前区间 end 实时取 now）"""
        bucket_start = datetime.combine(bucket["key"], datetime.min.time())
        bucket_end = now if is_current else self._bucket_end(granularity, base_date)
        return {
            "granularity": granularity,
            "scope": scope,
            "strategy_id": key if scope == "strategy" else "",
            "symbol": key if scope == "symbol" else "",
            "bucket_key": bucket["key"],
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "label": bucket["label"],
            "net_pnl": cell["net_pnl"],
            "gross_pnl": cell["gross_pnl"],
            "commission": cell["commission"],
            "wins": cell["wins"],
            "losses": cell["losses"],
            "closed_count": cell["wins"] + cell["losses"],
            "fill_count": cell["fill_count"],
            "order_count": cell["order_count"],
        }

    def _rollup(self, agg, granularity, now) -> list:
        """从 day 聚合按周/月 bucket_key 归一上卷，产出对应粒度记录"""
        today = now.date()
        records = []
        cur_bkey = self._bucket_info(today)[granularity]["key"]
        for scope in ("total", "strategy", "symbol"):
            for key, by_date in self._iter_scope(agg, scope).items():
                grouped = {}
                for d, cell in by_date.items():
                    bkey = self._bucket_info(d)[granularity]["key"]
                    grouped.setdefault(bkey, self._new_agg())
                    self._merge(grouped[bkey], cell)
                for bkey, cell in grouped.items():
                    bucket = self._bucket_info(bkey)[granularity]
                    records.append(self._make_record(
                        granularity, scope, key, bkey, bucket, cell,
                        bkey == cur_bkey, now))
        return records

    def _emit_all_records(self, agg, now) -> list:
        """把聚合容器展开为 day × 三作用域记录，并追加 week/month 上卷记录"""
        today = now.date()
        records = []
        for scope in ("total", "strategy", "symbol"):
            for key, by_date in self._iter_scope(agg, scope).items():
                for d, cell in by_date.items():
                    bucket = self._bucket_info(d)
                    records.append(self._make_record(
                        "day", scope, key, d, bucket["day"], cell, d == today, now))
        records += self._rollup(agg, "week", now)
        records += self._rollup(agg, "month", now)
        records += self._rollup(agg, "year", now)
        return records

    async def _store_snapshot(self, records) -> int:
        """批量 UPSERT 落库（单事务逐条），返回落库行数"""
        if not records:
            return 0
        params = []
        for r in records:
            params.append((
                r["granularity"], r["scope"], r["strategy_id"], r["symbol"],
                r["bucket_key"], r["bucket_start"], r["bucket_end"], r["label"],
                r["net_pnl"], r["gross_pnl"], r["commission"], r["wins"],
                r["losses"], r["closed_count"], r["fill_count"], r["order_count"],
            ))
        queries = [(self._METRIC_UPSERT_SQL, p) for p in params]
        await self._db_manager.execute_transaction(queries)
        return len(records)

    async def compute_and_store_metrics(self) -> dict:
        """全量指标预计算：聚合 + UPSERT 落库（供定时任务调用）

        并行一次拉 income/佣金 + 一次 DB 每日订单 + HRS 币种，内存切片得到
        day/week/month × total/strategy/symbol 三粒度三作用域记录并落库。

        Returns:
            dict: {granularities, rows} 成功；任一步骤异常向上抛出由任务层捕获。
        """
        await self._ensure_initialized()
        now = self._naive(datetime.now(BEIJING_TZ))
        overall_start = now - timedelta(days=self._precompute_retrospect_days)
        start_ms = int(overall_start.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        end_ms = int(now.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)

        income_list, commission_list, hrs_symbols, stats_rows = await asyncio.gather(
            self._get_income_data_paginated(start_ms, end_ms, income_type="REALIZED_PNL"),
            self._get_income_data_paginated(start_ms, end_ms, income_type="COMMISSION"),
            self._get_hrs_symbols(),
            self._query_stats_rows(overall_start, now),
        )

        agg = self._aggregate_day(income_list, commission_list, hrs_symbols)
        stats_daily = self._fold_stats_rows(stats_rows)
        await self._apply_orders_and_fills(agg, stats_daily, hrs_symbols, now)
        records = self._emit_all_records(agg, now)
        rows = await self._store_snapshot(records)
        return {"granularities": 3, "rows": rows}

    async def get_overview_from_metric(self, report_type: str = "daily"):
        """读预计算快速路径：当前区间（scope=total/strategy）返回与 get_overview 同结构 dict

        无数据或快照过期（超过新鲜度超时）返回 None，由调用方走实时兜底。
        """
        await self._ensure_initialized()
        granularity = self._metric_granularity(report_type)
        bucket_key = self._bucket_info(datetime.now(BEIJING_TZ).date())[granularity]["key"]
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT scope, strategy_id, symbol, net_pnl, gross_pnl, commission, "
                "wins, losses, closed_count, fill_count, order_count, snapshot_at "
                "FROM public.metric_snapshot "
                "WHERE granularity=$1 AND bucket_key=$2 AND scope IN ('total','strategy')",
                granularity, bucket_key
            )
        except Exception as e:
            logger.warning("总览读预计算失败，走实时兜底", error=str(e)[:80])
            return None
        if not rows:
            return None
        if not self._snapshot_fresh(rows):
            return None
        open_summary = await self.get_open_positions_summary()
        return self._build_overview_dict(rows, open_summary, report_type)

    @staticmethod
    def _snapshot_age_seconds(snapshot_at) -> float:
        """以快照表实际存储的参考帧计算年龄（秒）。

        snapshot_at/updated_at 由 DB 的 CURRENT_TIMESTAMP 写入，postgres 时区为 UTC，
        落库均为 UTC naive 时间。故必须用 datetime.utcnow() 相减；若误用北京 now，
        会产生固定 8 小时偏差、恒判快照过期，导致每次请求都回退实时聚合（卡顿）。
        """
        if snapshot_at is None:
            return float("inf")
        return (datetime.utcnow() - snapshot_at).total_seconds()

    def _snapshot_fresh(self, rows) -> bool:
        """快照新鲜度校验：最新 snapshot_at 距今未超新鲜度超时时视为有效"""
        latest = max((r.get("snapshot_at") for r in rows), default=None)
        if latest is None:
            return False
        return self._snapshot_age_seconds(latest) <= self._freshness_timeout

    async def get_trend_from_metric(self, report_type: str, days: int):
        """读预计算快速路径：scope=total 同粒度倒序 LIMIT days 再反转为升序

        返回与 get_trend_data 同结构的列表；无数据/过期返回 None 走实时兜底。
        """
        await self._ensure_initialized()
        granularity = self._metric_granularity(report_type)
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT label, net_pnl, order_count, wins, losses, snapshot_at "
                "FROM public.metric_snapshot "
                "WHERE granularity=$1 AND scope='total' AND strategy_id='' AND symbol='' "
                "ORDER BY bucket_key DESC LIMIT $2",
                granularity, days
            )
        except Exception as e:
            logger.warning("趋势读预计算失败，走实时兜底", error=str(e)[:80])
            return None
        if not rows:
            return None
        if not self._snapshot_fresh(rows):
            return None
        trends = []
        for r in reversed(rows):
            wins = r["wins"]
            losses = r["losses"]
            closed = wins + losses
            win_rate = round(wins / closed * 100, 1) if closed > 0 else 0.0
            trends.append({
                "date": r["label"],
                "total_pnl": f"{float(r['net_pnl']):.4f}",
                "order_count": r["order_count"],
                "win_rate": win_rate,
            })
        return trends

    def _build_overview_dict(self, rows, open_summary, report_type) -> dict:
        """把预计算行组装为 get_overview 完全同结构的响应 dict"""
        total = next((r for r in rows if r["scope"] == "total"), {})
        strategies = []
        # 覆盖策略全集：预计算有 metric 行 → 用其盈亏；否则全 0 兜底（仅体现持仓，
        # 覆盖激进版等"有持仓但近期无独立成交、metric 行缺失"的策略，避免策略被跳过）
        strategy_keys = list(dict.fromkeys(self._STRATEGY_KEY_MAP.values()))  # 去重保序（多个中文名可映射同一策略 id）
        for extra_key in open_summary.keys():
            if extra_key not in strategy_keys:
                strategy_keys.append(extra_key)
        for key in strategy_keys:
            srow = next((r for r in rows if r["scope"] == "strategy"
                         and r["strategy_id"] == key), None)
            wins = srow["wins"] if srow else 0
            losses = srow["losses"] if srow else 0
            closed = srow["closed_count"] if srow else 0
            gross = float(srow["gross_pnl"]) if srow else 0.0
            comm = float(srow["commission"]) if srow else 0.0
            net = float(srow["net_pnl"]) if srow else 0.0
            order_count = srow["order_count"] if srow else 0
            fill_count = srow["fill_count"] if srow else 0
            win_rate = round(wins / closed * 100, 1) if closed > 0 else 0.0
            pos = open_summary.get(key, {})
            strategies.append({
                "emoji": self._STRATEGY_EMOJI_MAP.get(key, ""),
                "id": key,
                "name": self._STRATEGY_NAME_MAP.get(key, key),
                "open_position_count": pos.get("open_position_count", 0),
                "open_margin": f"{pos.get('open_margin', 0):.2f}",
                "order_count": int(order_count),
                "fill_count": int(fill_count),
                "closed_count": closed,
                "win_count": wins,
                "loss_count": losses,
                "total_pnl": f"{net:.4f}",
                "gross_pnl": f"{gross:.4f}",
                "commission": f"{comm:.4f}",
                "win_rate": win_rate,
                "report_type": report_type,
                "updated_at": datetime.now(BEIJING_TZ).isoformat(),
            })
        total_wins = total.get("wins", 0)
        total_closed = total.get("closed_count", 0)
        win_rate = (total_wins / total_closed * 100) if total_closed > 0 else 0
        return {
            "total_pnl": f"{float(total.get('net_pnl', 0)):.4f}",
            "total_gross_pnl": f"{float(total.get('gross_pnl', 0)):.4f}",
            "total_commission": f"{float(total.get('commission', 0)):.4f}",
            "total_orders": total.get("order_count", 0),
            "total_closed": total_closed,
            "total_wins": total_wins,
            "win_rate": round(win_rate, 2),
            "strategies": strategies,
            "report_type": report_type,
            "updated_at": datetime.now(BEIJING_TZ).isoformat(),
        }

    # ========================================
    # 收益模块：净资产快照 + 收益率
    # ========================================

    async def get_account_returns(self, period: str = "daily") -> dict:
        """获取账户收益率（基于净资产快照增量，非 income API）

        逻辑：
          1. 取今日净资产（get_account_equity 的 total_equity）。
          2. 根据 period 查 public.equity_snapshot 对应期初快照：
             - daily: 昨日 snapshot_date
             - weekly: 本周一 snapshot_date
             - monthly: 本月1号 snapshot_date
          3. 任一期初缺失或为0 → yield_unavailable=true，仍返回当前净值。
          4. 否则 yield=(equity - period_start)/period_start*100（保留1位小数）。

        Args:
            period: 周期，daily | weekly | monthly

        Returns:
            dict: {period, equity, yield, yield_text, yield_unavailable,
                   period_start_equity, period_pnl, snapshot_date}
        """
        await self._ensure_initialized()

        # 1. 今日净资产
        equity_float = 0.0
        try:
            equity_str = (await self.get_account_equity()).get("total_equity", "0")
            equity_float = float(equity_str)
        except (TypeError, ValueError):
            logger.warning("净资产解析失败，收益率标记为不可用")

        # 2. 期初快照（严格取周期起点日对应净资产快照）
        # 严格匹配：日=昨日，周=本周一，月=本月1号，年=本年1/1。周期起点无快照
        # （如历史快照积累前）一律标记不可用，避免错误地把"最早一条"当期初而算错净盈亏。
        period_start_equity = None
        start_date = self._get_period_start_date(period)
        try:
            row = await self._db_manager.fetch_one(
                "SELECT total_equity, snapshot_date FROM public.equity_snapshot "
                "WHERE snapshot_date = $1",
                start_date,
            )
            if row and row.get("total_equity") is not None:
                period_start_equity = float(row["total_equity"])
        except Exception as e:
            logger.warning("查询期初快照失败（快照表可能未初始化）", error=str(e)[:80])

        # 3. 期初缺失/为0 → 收益率不可用（但业务上期初为0不可除）
        period_start_ok = (
            period_start_equity is not None and period_start_equity > 0
        )
        if not period_start_ok:
            return {
                "period": period,
                "equity": equity_str,
                "yield": None,
                "yield_text": "--",
                "yield_unavailable": True,
                "period_start_equity": (
                    f"{period_start_equity:.2f}" if period_start_equity is not None else None
                ),
                "period_pnl": None,
                "snapshot_date": datetime.now(BEIJING_TZ).date().isoformat(),
            }

        # 今日净值若实时接口失败返回0，也应标记为不可用
        if equity_float <= 0:
            return self._build_returns_unavailable(period, equity_str, "实时净值不可用")

        # 4. 收益率计算
        payout = (equity_float - period_start_equity) / period_start_equity * 100
        payout_round = round(payout, 1)
        text = f"+{payout_round:.1f}%" if payout_round > 0 else f"{payout_round:.1f}%"

        return {
            "period": period,
            "equity": equity_str,
            "yield": payout_round,
            "yield_text": text,
            "yield_unavailable": False,
            "period_start_equity": f"{period_start_equity:.2f}",
            "period_pnl": f"{equity_float - period_start_equity:.2f}",
            "snapshot_date": datetime.now(BEIJING_TZ).date().isoformat(),
        }

    def _get_period_start_date(self, period: str):
        """计算期初快照日期（北京时间）

        周期性取值：
          - daily:  昨日
          - weekly: 本周一
          - monthly: 本月1号
        其他值回退为昨日。

        Args:
            period: 周期

        Returns:
            datetime.date: 期初快照日期
        """
        today = datetime.now(BEIJING_TZ).date()
        if period == "weekly":
            return today - timedelta(days=today.weekday())
        elif period == "monthly":
            return today.replace(day=1)
        elif period == "yearly":
            return today.replace(month=1, day=1)
        return today - timedelta(days=1)

    def _build_returns_unavailable(self, period: str, equity: str, reason: str) -> dict:
        """构造收益率不可用的返回（复用，避免重复代码）"""
        logger.warning("收益率标记为不可用", period=period, reason=reason)
        return {
            "period": period,
            "equity": equity,
            "yield": None,
            "yield_text": "--",
            "yield_unavailable": True,
            "period_start_equity": None,
            "period_pnl": None,
            "snapshot_date": datetime.now(BEIJING_TZ).date().isoformat(),
        }

    # ========================================
    # 策略持仓概览（策略卡扩展字段）
    # ========================================

    async def get_open_positions_summary(self) -> dict:
        """按策略聚合当前持仓数与持仓保证金

        优先读持仓对账快照表（strategy_position_snapshot，后台定时对账落库），
        快照缺失/过期时回退实时查询 trading.strategy_open_positions（margin > 0）。

        Returns:
            dict: {strategy_id: {"open_position_count": int, "open_margin": float}}
                  查询失败返回 {}
        """
        await self._ensure_initialized()
        # 快速路径：读持仓对账快照（fresh 且非空时返回）
        snapshot = await self._read_position_snapshot()
        if snapshot:
            return {
                sid: {
                    "open_position_count": int(info["open_position_count"]),
                    "open_margin": round(info["open_margin"], 2),
                }
                for sid, info in snapshot.items()
                if int(info.get("open_position_count", 0)) > 0
            }
        # 兜底：实时聚合 strategy_open_positions
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT strategy_id, COUNT(*) AS cnt, COALESCE(SUM(margin),0) AS margin "
                "FROM trading.strategy_open_positions "
                "WHERE margin > 0 "
                "GROUP BY strategy_id"
            )
        except Exception as e:
            logger.warning("查询持仓汇总失败（持仓表可能未初始化）", error=str(e)[:80])
            return {}

        result = {}
        for row in rows:
            result[row["strategy_id"]] = {
                "open_position_count": int(row["cnt"]),
                "open_margin": round(float(row["margin"]), 2),
            }
        return result

    async def get_position_utilization_trend(self, days: int = 30) -> dict:
        """返回 各策略占用比 的历史趋势（按天聚合，取每天最后一个小时的占用比）

        Args:
            days: 回溯天数（天数由配置/环境变量注入，禁止硬编码场景由调用方传入）

        Returns:
            dict: {dates: [str], strategies: {strategy_id: {name, series: [float]}}}
        """
        await self._ensure_initialized()
        cutoff = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(days=max(days, 1))
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT strategy_id, occupied_ratio, snapshot_hour "
                "FROM public.strategy_position_snapshot_history "
                "WHERE snapshot_hour >= $1 "
                "ORDER BY snapshot_hour",
                cutoff,
            )
        except Exception as e:
            logger.warning("查询占用比历史趋势失败", error=str(e)[:80])
            return {"dates": [], "strategies": {}}

        # 按天聚合：rows 按 snapshot_hour 升序，后值覆盖前值 → 每天保留最后一个小时的占用比
        day_series: Dict[str, Dict[str, float]] = {}
        for r in rows:
            day = str(r["snapshot_hour"].date())
            day_series.setdefault(day, {})[str(r["strategy_id"])] = float(r["occupied_ratio"] or 0)

        if not day_series:
            return {"dates": [], "strategies": {}}

        dates = sorted(day_series.keys())
        strategy_ids = set()
        for by_day in day_series.values():
            strategy_ids.update(by_day.keys())

        name_map = dict(self._STRATEGY_NAME_MAP)
        name_map.setdefault("btc_eth", "MTPCS策略")
        name_map.setdefault("btc_eth_aggressive", "MTPCS激进版")
        name_map.setdefault("hrs", "HRS策略")
        name_map.setdefault("new_coin", "新币做空策略")
        name_map.setdefault("grid", "网格策略")

        strategies = {}
        for sid in strategy_ids:
            strategies[sid] = {
                "name": name_map.get(sid, sid),
                "series": [day_series[d].get(sid, 0.0) for d in dates],
            }
        return {"dates": dates, "strategies": strategies}

    async def _read_position_snapshot(self) -> dict:
        """读持仓对账快照表（新鲜且非空时返回）

        Returns:
            dict: {strategy_id: {"strategy_name", "open_position_count", "open_margin",
                                 "allocated_amount", "occupied_ratio", "snapshot_at"}}
                  表不存在/无数据/过期 → {}
        """
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT strategy_id, strategy_name, open_position_count, open_margin, "
                "allocated_amount, occupied_ratio, snapshot_at "
                "FROM public.strategy_position_snapshot"
            )
        except Exception as e:
            logger.warning("读持仓快照失败，走实时兜底", error=str(e)[:80])
            return {}
        if not rows:
            return {}
        latest = max((r.get("snapshot_at") for r in rows), default=None)
        if latest is None:
            return {}
        if self._snapshot_age_seconds(latest) > self._position_freshness_timeout:
            return {}
        return {
            str(r["strategy_id"]): {
                "strategy_name": str(r.get("strategy_name") or ""),
                "open_position_count": int(r.get("open_position_count") or 0),
                "open_margin": float(r.get("open_margin") or 0),
                "allocated_amount": float(r.get("allocated_amount") or 0),
                "occupied_ratio": float(r.get("occupied_ratio") or 0),
                "snapshot_at": r.get("snapshot_at"),
            }
            for r in rows
        }

    async def compute_and_store_positions(self) -> dict:
        """持仓/占用对账：从币安持仓 + 策略上报表推导各策略当前持仓与占用比并落库

        权威口径：
          - 币安实时持仓（get_position）为主判断"当前有哪些未平仓位"
          - 归属策略：strategy_open_positions 已有归属优先（如激进版 BNB），
            否则按 trade_records 该币种最近一次下单标注的策略归属（解决 HRS 等未上报场景）
          - 各策略持仓保证金：strategy_open_positions 已有 margin 复用；
            缺失时用币安仓位 initialMargin（无则按 名义价值/杠杆）估算
          - 占用比 = 该策略持仓保证金 / 该策略分配金额（来自最新 active 月度分配）

        Returns:
            dict: {rows} 落库行数；失败向上抛出由任务层捕获。
        """
        await self._ensure_initialized()
        pos_list = await self._binance_client.get_position()

        # 1) 策略上报表：symbol -> (strategy_id, margin)；同 symbol 多策略上报记录为归属歧义
        reported = {}
        reported_conflict = set()
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT strategy_id, symbol, margin FROM trading.strategy_open_positions WHERE margin > 0"
            )
            for r in rows:
                sym = str(r["symbol"])
                sid = self._normalize_strategy_id(r["strategy_id"])
                if sym in reported and reported[sym][0] != sid:
                    reported_conflict.add(sym)
                elif sym not in reported:
                    reported[sym] = (sid, float(r["margin"] or 0))
        except Exception as e:
            logger.warning("读 strategy_open_positions 失败，跳过上报归属", error=str(e)[:80])

        # 2) 每个币种最近一次"开仓方向"订单标注的策略（归属兜底）
        # 仅统计 LIMIT/MARKET 开仓单，剔除 STOP/TAKE_PROFIT/STOP_MARKET/TAKE_PROFIT_MARKET/
        # CONDITIONAL_ORDER/PNL_SUMMARY 等平仓/条件/总结记录；避免交易所触发的平仓条件单
        # (比开仓时间更晚) 把该币种真实敞口抢占到错误策略名下，导致持仓归属颠倒。
        # 例：激进版 08:10 SELL LIMIT 开 SOL 空仓后被归为'最近成交'，原版 09:01 的 BUY
        # 条件单却成了'该币种最近一条记录'，从而误夺归属。
        last_trade_strategy = {}
        try:
            rows = await self._db_manager.fetch_all(
                "SELECT DISTINCT ON (symbol) symbol, strategy "
                "FROM trading.trade_records "
                "WHERE order_type IN ('LIMIT', 'MARKET') "
                "ORDER BY symbol, executed_at DESC"
            )
            for r in rows:
                last_trade_strategy[str(r["symbol"])] = self._normalize_strategy_id(r["strategy"])
        except Exception as e:
            logger.warning("读 trade_records 下端归属失败", error=str(e)[:80])

        # 3) 汇总各策略持仓数 / 保证金
        margin_by_strategy: Dict[str, float] = {}
        count_by_strategy: Dict[str, int] = {}
        for pos in pos_list:
            sym = str(pos.get("symbol") or "")
            amt = float(pos.get("positionAmt") or 0)
            if not sym or abs(amt) <= 1e-8:
                continue
            strategy_id = None
            reported_margin = 0.0
            if sym in reported_conflict:
                # 同 symbol 多策略上报冲突：以 trade_records 最近成交策略为准
                strategy_id = last_trade_strategy.get(sym)
                reported_margin = reported[sym][1]
                if not strategy_id:
                    # 无成交记录可鉴权时，回退采用首条上报策略
                    strategy_id = reported[sym][0]
            elif sym in reported:
                strategy_id, reported_margin = reported[sym]
            else:
                strategy_id = last_trade_strategy.get(sym)
            if not strategy_id:
                continue
            # 保证金统一采用币安实时持仓口径（与币安App"持仓保证金"一致）。
            # 组合保证金(PM)账户 positionRisk 不返回 initialMargin，
            # 故依优先级：initialMargin > 名义价值/杠杆估算 > 策略上报陈旧值兜底。
            im = float(pos.get("initialMargin") or 0)
            notional = abs(float(pos.get("notional") or 0))
            leverage = float(pos.get("leverage") or 1)
            if im > 0:
                margin = im
            elif notional > 0 and leverage > 0:
                margin = notional / leverage
            else:
                margin = reported_margin  # 仅当币安无任何数值时才用策略上报表值兜底
            margin_by_strategy[strategy_id] = margin_by_strategy.get(strategy_id, 0.0) + margin
            count_by_strategy[strategy_id] = count_by_strategy.get(strategy_id, 0) + 1

        # 4) 该策略分配金额（最新 active 月度分配），用于占用比
        allocated_by_strategy = {}
        try:
            alloc = await self._get_active_capital_allocation_raw()
            for e in alloc:
                sid = e.get("strategy_id")
                amount = float(e.get("allocated_amount") or 0)
                if sid:
                    allocated_by_strategy[sid] = amount
        except Exception as exc:
            logger.warning("读月度分配失败，占用比按0", error=str(exc)[:80])

        # 5) 汇总所有已知策略名称，落库（含无持仓策略，便于占用比/名称为0兜底）
        all_ids = set(count_by_strategy) | set(allocated_by_strategy)
        name_map = {**self._STRATEGY_NAME_MAP}
        name_map["btc_eth"] = name_map.get("btc_eth", "MTPCS策略")
        name_map["btc_eth_aggressive"] = name_map.get("btc_eth_aggressive", "MTPCS激进版")
        name_map["hrs"] = name_map.get("hrs", "HRS策略")
        name_map["new_coin"] = name_map.get("new_coin", "新币做空策略")
        name_map["grid"] = name_map.get("grid", "网格策略")
        now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
        snapshot_hour = now.replace(minute=0, second=0, microsecond=0)
        rows_written = 0
        for sid in all_ids:
            margin = margin_by_strategy.get(sid, 0.0)
            alloc = allocated_by_strategy.get(sid, 0.0)
            ratio = round(margin / alloc, 6) if alloc > 0 else 0.0
            await self._db_manager.execute(
                "INSERT INTO public.strategy_position_snapshot "
                "(strategy_id, strategy_name, open_position_count, open_margin, "
                " allocated_amount, occupied_ratio, snapshot_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (strategy_id) DO UPDATE SET "
                "  strategy_name=EXCLUDED.strategy_name, "
                "  open_position_count=EXCLUDED.open_position_count, "
                "  open_margin=EXCLUDED.open_margin, "
                "  allocated_amount=EXCLUDED.allocated_amount, "
                "  occupied_ratio=EXCLUDED.occupied_ratio, "
                "  snapshot_at=EXCLUDED.snapshot_at, updated_at=CURRENT_TIMESTAMP",
                sid, name_map.get(sid, sid), int(count_by_strategy.get(sid, 0)),
                margin, alloc, ratio, now,
            )
            # 同步追加/覆盖历史明细（同一小时重复对账仅更新，不产生重复行）
            try:
                await self._db_manager.execute(
                    "INSERT INTO public.strategy_position_snapshot_history "
                    "(strategy_id, strategy_name, open_position_count, open_margin, "
                    " allocated_amount, occupied_ratio, snapshot_hour, updated_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (strategy_id, snapshot_hour) DO UPDATE SET "
                    "  strategy_name=EXCLUDED.strategy_name, "
                    "  open_position_count=EXCLUDED.open_position_count, "
                    "  open_margin=EXCLUDED.open_margin, "
                    "  allocated_amount=EXCLUDED.allocated_amount, "
                    "  occupied_ratio=EXCLUDED.occupied_ratio, "
                    "  updated_at=CURRENT_TIMESTAMP",
                    sid, name_map.get(sid, sid), int(count_by_strategy.get(sid, 0)),
                    margin, alloc, ratio, snapshot_hour,
                )
            except Exception as hist_exc:
                # 历史明细写入失败不影响当前快照与整轮对账（仅记录日志）
                logger.warning("写占用比历史明细失败，跳过", strategy_id=sid, error=str(hist_exc)[:80])
            rows_written += 1

        # 清理本周期未出现的陈旧策略行（如历史遗留的中文名策略 ID），避免残留脏数据
        canonical_ids = list(all_ids)
        if canonical_ids:
            await self._db_manager.execute(
                "DELETE FROM public.strategy_position_snapshot "
                "WHERE NOT (strategy_id = ANY($1::text[]))",
                canonical_ids,
            )
        else:
            await self._db_manager.execute("DELETE FROM public.strategy_position_snapshot")
        return {"rows": rows_written, "strategies": len(all_ids)}

    # ========================================
    # AI 监控模块（调优执行记录 + 月度分配 + 最近建议）
    # ========================================

    async def get_ai_monitor(self, weeks: int = 8, limit: int = 10) -> dict:
        """聚合 AI 监控数据：调优执行记录 / 月度资金分配 / 最近优化建议

        Args:
            weeks: 调优记录回溯周数
            limit: 返回条数上限

        Returns:
            dict: {tuning_runs, capital_allocation, recent_suggestions}
        """
        await self._ensure_initialized()
        tuning_runs = await self._get_ai_tuning_runs(weeks=weeks, limit=limit)
        capital_allocation = await self._get_active_capital_allocation()
        recent_suggestions = await self._get_recent_suggestions(limit=limit)
        return {
            "tuning_runs": tuning_runs,
            "capital_allocation": capital_allocation,
            "recent_suggestions": recent_suggestions,
            "refresh_info": await self._get_position_refresh_info(),
        }

    async def _get_ai_tuning_runs(self, weeks: int, limit: int) -> list:
        """查询最近的 AI 调优执行记录（public.ai_tuner_runs）

        按 executed_at 倒序取最近 limit 条；缺失表/异常返回 []。

        Args:
            weeks: 回溯周数（仅用于过滤最近 N 周，防止历史批次混入）
            limit: 返回条数上限

        Returns:
            list: [{strategy_id, strategy_name, status, run_key, executed_at}]
        """
        try:
            since = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(weeks=weeks)
            rows = await self._db_manager.fetch_all(
                "SELECT strategy_id, strategy_name, status, run_key, executed_at "
                "FROM public.ai_tuner_runs "
                "WHERE executed_at >= $1 "
                "ORDER BY executed_at DESC "
                "LIMIT $2",
                since, limit,
            )
        except Exception as e:
            logger.warning("查询调优执行记录失败（表可能未初始化）", error=str(e)[:80])
            return []

        if not rows:
            return []

        # 同一 run_key 视为一批（一次调优对所有策略的集中执行），
        # 只需映射最新一批的结果，避免历史批次混入展示
        grouped: Dict[str, list] = {}
        for r in rows:
            key = r.get("run_key")
            if not key:
                continue
            grouped.setdefault(key, []).append(r)

        if not grouped:
            return []

        latest_key = max(grouped.keys())
        return [
            {
                "strategy_id": r.get("strategy_id"),
                "strategy_name": r.get("strategy_name"),
                "status": r.get("status"),
                "run_key": r.get("run_key"),
                "executed_at": r.get("executed_at").isoformat() if r.get("executed_at") else None,
            }
            for r in grouped[latest_key]
        ]

    async def _get_active_capital_allocation(self) -> Optional[dict]:
        """查询最新 status='active' 的月度资金分配记录

        表结构（由 config_updater.py 写入确认）：
          month / total_capital / strategy_count / is_first_month /
          entries(JSONB 数组) / status / created_at

        Returns:
            dict: {month, total_capital, strategy_count, entries, status}
                  表不存在/无 active 记录 → None
        """
        try:
            row = await self._db_manager.fetch_one(
                "SELECT month, total_capital, strategy_count, entries, status "
                "FROM public.capital_allocation "
                "WHERE status = 'active' "
                "ORDER BY month DESC "
                "LIMIT 1"
            )
        except Exception as e:
            logger.warning("查询月度资金分配失败（表可能未初始化）", error=str(e)[:80])
            return None

        if not row:
            return None

        entries = row.get("entries") or []
        # asyncpg 返回的 JSONB 可能为 JSON 字符串，先转换为 list（解析失败回退空列表）
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except (ValueError, TypeError):
                logger.warning("月度资金分配 entries 解析失败，按空处理")
                entries = []
        # 占用比：从持仓对账快照表读取（家庭级持仓保证金之和 / 该策略分配金额，
        # 由后台定时任务落库 margin、allocated_amount）
        snapshot = await self._read_position_snapshot()
        parsed_entries = [
            {
                "strategy_id": e.get("strategy_id"),
                "strategy_name": e.get("strategy_name"),
                "allocated_amount": e.get("allocated_amount"),
                "allocated_ratio": e.get("allocated_ratio"),
                "occupied_amount": self._entry_family_margin(e, snapshot),
                "occupied_ratio": self._entry_occupied_ratio(e, snapshot),
                "rank": e.get("rank"),
            }
            for e in entries
            if isinstance(e, dict)
        ]

        return {
            "month": row.get("month"),
            "total_capital": str(row.get("total_capital")),
            "strategy_count": int(row.get("strategy_count") or 0),
            "entries": parsed_entries,
            "status": row.get("status"),
        }

    def _entry_occupied_ratio(self, entry: dict, snapshot: dict) -> float:
        """计算月度资金分配条目的占用比（家庭级）

        占用比 = 该策略及其共用同一分配额度的所有策略持仓保证金之和 / 该策略分配金额。
        例：MTPCS(btc_eth) 与激进版(btc_eth_aggressive) 共享 btc_eth 分配金额，
        占用比 = (btc_eth.margin + btc_eth_aggressive.margin) / btc_eth.allocated_amount。

        Args:
            entry: 分配条目（{strategy_id, allocated_amount, ...}）
            snapshot: 持仓对账快照 {strategy_id: {"open_margin", ...}}

        Returns:
            float: 占用比（0~1+），分配金额为 0 或无策略 id 时返回 0.0
        """
        sid = entry.get("strategy_id")
        if not sid:
            return 0.0
        alloc_amount = float(entry.get("allocated_amount") or 0)
        if alloc_amount <= 0:
            return 0.0
        family_margin = self._entry_family_margin(entry, snapshot)
        return round(family_margin / alloc_amount, 6)

    def _entry_family_margin(self, entry: dict, snapshot: dict) -> float:
        """计算某分配条目的家庭级占用保证金之和（USDT）

        该策略及其共用同一分配额度的所有策略持仓保证金之和。
        例：MTPCS(btc_eth) 与激进版(btc_eth_aggressive) 共享 btc_eth 分配金额，
        占用金额 = btc_eth.margin + btc_eth_aggressive.margin。

        Args:
            entry: 分配条目（{strategy_id, ...}）
            snapshot: 持仓对账快照 {strategy_id: {"open_margin", ...}}

        Returns:
            float: 家庭级占用保证金（USDT），无策略 id 时返回 0.0
        """
        sid = entry.get("strategy_id")
        if not sid:
            return 0.0
        return round(
            sum(
                snapshot.get(gid, {}).get("open_margin", 0.0)
                for gid in self._allocation_group_ids(str(sid))
            ),
            6,
        )

    async def _get_active_capital_allocation_raw(self) -> list:
        """读取最新 active 月度资金分配的原始 entries 列表（供持仓对账任务使用）

        Returns:
            list: [{strategy_id, allocated_amount, ...}]；无记录/异常返回 []
        """
        try:
            row = await self._db_manager.fetch_one(
                "SELECT entries FROM public.capital_allocation "
                "WHERE status = 'active' "
                "ORDER BY month DESC "
                "LIMIT 1"
            )
        except Exception as e:
            logger.warning("读取月度资金分配原始条目失败", error=str(e)[:80])
            return []
        if not row:
            return []
        entries = row.get("entries") or []
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except (ValueError, TypeError):
                logger.warning("月度分配原始 entries 解析失败，按空处理")
                return []
        return [e for e in entries if isinstance(e, dict)]

    async def _get_recent_suggestions(self, limit: int) -> list:
        """查询最近一批 AI 优化建议（状态与 ai_tuner 周度调优结果保持一致）

        状态来源：public.ai_tuner_runs 最新 run_key 批次各策略的 status
          （success=已调整 / skip=无需调整 / error=异常），与「AI 调优执行」周报统计一致。
        内容来源：trading.strategy_memory 同日期的 ai_suggestions（建议摘要/调整项）。

        Returns:
            list: [{strategy_id, strategy_name, status, created_at, adjustments, is_applied, is_rejected}]
        """
        try:
            latest = await self._db_manager.fetch_one(
                "SELECT run_key FROM public.ai_tuner_runs "
                "ORDER BY executed_at DESC LIMIT 1"
            )
            if not latest or not latest.get("run_key"):
                return []
            run_key = latest["run_key"]

            runs = await self._db_manager.fetch_all(
                "SELECT strategy_id, strategy_name, status, executed_at "
                "FROM public.ai_tuner_runs WHERE run_key = $1 "
                "ORDER BY executed_at",
                run_key,
            )
            if not runs:
                return []

            run_day = datetime.fromisoformat(str(run_key)).date()
            mem_rows = await self._db_manager.fetch_all(
                "SELECT strategy_id, ai_suggestions "
                "FROM trading.strategy_memory "
                "WHERE created_at::date = $1",
                run_day,
            )
            mem_by_sid: Dict[str, list] = {}
            for row in mem_rows:
                mem_by_sid.setdefault(row.get("strategy_id"), row)

            result = []
            for r in runs:
                status = (r.get("status") or "error") if runs else "error"
                mem = mem_by_sid.get(r.get("strategy_id"))
                ai_suggestions = (mem or {}).get("ai_suggestions")
                result.append({
                    "strategy_id": r.get("strategy_id"),
                    "strategy_name": r.get("strategy_name"),
                    "status": status,
                    "created_at": (
                        r.get("executed_at").isoformat()
                        if r.get("executed_at") else None
                    ),
                    "adjustments": self._extract_suggestions(ai_suggestions) if ai_suggestions else [],
                    "is_applied": bool((mem or {}).get("is_applied")),
                    "is_rejected": bool((mem or {}).get("is_rejected")),
                })
            return result
        except Exception as e:
            logger.warning("查询最近建议失败（表可能未初始化）", error=str(e)[:80])
            return []

    @staticmethod
    def _extract_suggestions(suggestions) -> list:
        """从 ai_suggestions JSON 中提取调整建议文案

        ai_suggestions 可能为 dict（如 {"parameters": [...]}）或 list，
        统一展开为字符串列表，供前端展示。

        Args:
            suggestions: ai_suggestions 原始值（JSONB 已解析为 dict/list）

        Returns:
            list: 调整建议文案列表
        """
        if isinstance(suggestions, str):
            try:
                suggestions = json.loads(suggestions)
            except (ValueError, TypeError):
                return [suggestions]
        if isinstance(suggestions, list):
            return [s for s in suggestions if isinstance(s, str)]
        if isinstance(suggestions, dict):
            # 常见结构：{"parameters": [...]} 或含 description/summary
            out = []
            for key in ("parameters", "adjustments", "changes"):
                val = suggestions.get(key)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str):
                            out.append(item)
                        elif isinstance(item, dict):
                            out.append(item.get("description") or item.get("name") or str(item))
            for key in ("description", "summary", "recommendation", "reason"):
                if suggestions.get(key):
                    out.append(str(suggestions[key]))
            return out
        return []

    # ========================================
    # 风控模块
    # ========================================

    async def get_risk(self, days: int = 7) -> dict:
        """获取风控指标（阈值全部来自 risk_settings 配置中心）

        Args:
            days: 回撤/亏损统计天数

        Returns:
            dict: 完整风控指标
        """
        await self._ensure_initialized()
        risk_cfg = risk_settings.get("risk", {})

        # 1. 总持仓保证金
        total_position_margin = await self._get_total_position_margin()
        # 2. 持仓上限 = 月度分配总额（entries allocated_amount 求和）
        margin_limit = await self._get_margin_limit_from_allocation()

        # 3. 占用率
        limit_occupancy = (
            round(total_position_margin / margin_limit * 100, 2)
            if margin_limit and margin_limit > 0
            else None
        )

        # 4. 净资产占用率
        account_equity = 0.0
        try:
            account_equity = float(
                (await self.get_account_equity()).get("total_equity", "0")
            )
        except (TypeError, ValueError):
            account_equity = 0.0
        equity_ratio_occupancy = (
            round(total_position_margin / account_equity * 100, 2)
            if account_equity > 0
            else None
        )

        # 5. 可用保证金
        available_margin = (
            round(max(margin_limit - total_position_margin, 0), 2)
            if margin_limit is not None
            else None
        )

        # 6. 逼近/超限判定（逐策略判定，消除绝对额 cap 造成的全局误报）
        caps = risk_cfg.get("account_ratio_caps", {})
        snapshot = await self._read_position_snapshot()
        approaching, exceeded = self._judge_threshold(
            caps, snapshot, account_equity,
            risk_cfg.get("occupancy_warning_ratio", 0.8),
        )

        # 7. 止损统计
        stop_window = int(risk_cfg.get("stop_loss_days_window", 7))
        recent_stop_count, recent_stop_trend = await self._get_stop_stats(stop_window)

        # 8. 连续亏损天数 + 最大回撤
        loss_days, max_drawdown = await self._get_equity_loss_stats(days)

        return {
            "total_position_margin": f"{total_position_margin:.2f}",
            "margin_limit": f"{margin_limit:.2f}" if margin_limit is not None else None,
            "limit_occupancy": limit_occupancy,
            "account_ratio_caps": caps or self._flat_caps(),
            "equity_ratio_occupancy": equity_ratio_occupancy,
            "available_margin": f"{available_margin:.2f}" if available_margin is not None else None,
            "approaching_threshold": approaching,
            "threshold_exceeded": exceeded,
            "recent_stop_count": recent_stop_count,
            "recent_stop_trend": recent_stop_trend,
            "consecutive_loss_days": loss_days,
            "max_drawdown_period": (
                max_drawdown[0].isoformat() if max_drawdown and max_drawdown[0] else None
            ),
            "drawdown_pct": max_drawdown[1] if max_drawdown else None,
            "daily_drawdown_pct": risk_cfg.get("daily_drawdown_pct"),
            "updated_at": datetime.now(BEIJING_TZ).isoformat(),
            "refresh_info": await self._get_position_refresh_info(),
        }

    async def _get_position_refresh_info(self) -> dict:
        """持仓对账刷新信息：更新间隔 + 距下次更新的剩余秒数（供前端倒计时标注）

        以最新一次持仓快照落库时间推算：remaining = interval - (now - snapshot_time) % interval。
        快照表读取失败/无记录时按整周期返回。
        """
        interval = int(os.getenv("POSITION_RECONCILE_INTERVAL_SECONDS", "300"))
        try:
            row = await self._db_manager.fetch_one(
                "SELECT MAX(updated_at) AS u FROM public.strategy_position_snapshot"
            )
        except Exception:
            row = None
        remaining = interval
        if row and row.get("u"):
            # postgres updated_at 为 UTC naive（CURRENT_TIMESTAMP），与 datetime.utcnow() 对齐
            age = (datetime.utcnow() - row["u"]).total_seconds()
            if age >= 0:
                remaining = max(0, int(interval - (age % interval)))
            if remaining == 0:
                remaining = interval
        return {"refresh_interval": interval, "refresh_in": remaining}

    async def _get_total_position_margin(self) -> float:
        """获取当前总持仓保证金（各策略对账快照 open_margin 之和）

        优先读持仓对账快照（strategy_position_snapshot，按币安实时持仓过滤落库，
        不含已平/停滞的旧仓位）；快照为空或读取失败时回退 strategy_open_positions SUM。
        """
        try:
            row = await self._db_manager.fetch_one(
                "SELECT COALESCE(SUM(open_margin),0) AS total "
                "FROM public.strategy_position_snapshot"
            )
            total = float(row.get("total") or 0.0)
            if total > 0:
                return round(total, 2)
        except Exception as e:
            logger.warning("查询对账快照持仓保证金失败，回退策略表", error=str(e)[:80])
        try:
            row = await self._db_manager.fetch_one(
                "SELECT COALESCE(SUM(margin),0) AS total "
                "FROM trading.strategy_open_positions"
            )
            return float(row.get("total") or 0.0)
        except Exception as e:
            logger.warning("查询策略表持仓保证金失败（表可能未初始化）", error=str(e)[:80])
            return 0.0

    async def _get_margin_limit_from_allocation(self) -> Optional[float]:
        """从月度资金分配 entries 求和得出持仓上限

        取最新 active 分配的 entries.allocated_amount 之和；查失败/无记录返回 None。

        Returns:
            float: 持仓上限（USDT）；不可得返回 None
        """
        try:
            row = await self._db_manager.fetch_one(
                "SELECT entries FROM public.capital_allocation "
                "WHERE status = 'active' "
                "ORDER BY month DESC "
                "LIMIT 1"
            )
        except Exception as e:
            logger.warning("查询持仓上限失败（分配表可能未初始化）", error=str(e)[:80])
            return None

        if not row:
            return None
        entries = row.get("entries") or []
        # asyncpg 返回的 JSONB 可能为 JSON 字符串，先转换为 list（解析失败回退空列表）
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except (ValueError, TypeError):
                logger.warning("持仓上限 entries 解析失败，按空处理")
                entries = []
        total = 0.0
        for e in entries:
            if isinstance(e, dict) and e.get("allocated_amount") is not None:
                try:
                    total += float(e["allocated_amount"])
                except (TypeError, ValueError):
                    continue
        return total if total > 0 else None

    def _flat_caps(self) -> dict:
        """返回默认（扁平）风控阈值对账（配置缺失时使用，避免前端空）"""
        return {}

    def _judge_threshold(
        self,
        caps: dict,
        snapshot: dict,
        account_equity: float,
        warning_ratio: float,
    ) -> tuple:
        """判断各策略占用是否逼近/超过各自阈值（逐策略判定）

        对每个策略 cap 单独判定，任一策略超限 → threshold_exceeded=true；
        任一策略逼近（未超但占用 >= 上限*warning_ratio）→ approaching_threshold=true。

        - 绝对额型（cap>=1，如 new_coin=150，来源为其 config.yaml 的
          position_sizing.total.total_position_margin_limit）：该策略家庭级占用保证金
          直接与 cap 数值比较（margin vs 150）。
        - 比例型（cap<1，如 0.3）：该策略家庭级占用比 = family_margin / account_equity，
          与 cap 比较。

        Args:
            caps: account_ratio_caps 配置（策略 id -> cap）
            snapshot: 持仓对账快照 {strategy_id: {"open_margin", ...}}
            account_equity: 账户净资产（比例型 cap 判定用，需 > 0）
            warning_ratio: 占用率预警系数

        Returns:
            (approaching_threshold, threshold_exceeded): (bool, bool)
        """
        approaching_threshold = False
        threshold_exceeded = False
        for sid, cap in caps.items():
            try:
                cap_f = float(cap)
            except (TypeError, ValueError):
                continue
            if cap_f <= 0:
                continue
            # 各策略家庭级占用保证金（与月度分配占用口径一致）
            family_margin = self._entry_family_margin({"strategy_id": sid}, snapshot)
            if cap_f >= 1:
                # 绝对额型：占用保证金与上限直接比较
                exceed = family_margin >= cap_f
                approach = (not exceed) and (family_margin >= cap_f * warning_ratio)
            else:
                # 比例型：占用比 = 占用保证金 / 净资产，与上限比较
                if account_equity <= 0:
                    continue
                ratio = family_margin / account_equity
                exceed = ratio >= cap_f
                approach = (not exceed) and (ratio >= cap_f * warning_ratio)
            if exceed:
                threshold_exceeded = True
            if approach:
                approaching_threshold = True
        return approaching_threshold, threshold_exceeded

    async def _get_stop_stats(self, window_days: int) -> tuple:
        """统计近 N 天已触发的止损次数与每日分布

        止损判定：trade_records.close_reason='STOP_LOSS'。
        该标记由各策略在"止损触发并平仓"时通过 TradeLogger.log_stop_loss() 写入
        （见 shared/trade_logger.py），与止盈/手动平仓明确区分。

        Args:
            window_days: 统计窗口（天）

        Returns:
            (count, trend): 总笔数与 [{date: MM-DD, count}]
        """
        try:
            since = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(days=window_days)
            rows = await self._db_manager.fetch_all(
                "SELECT executed_at FROM trading.trade_records "
                "WHERE close_reason = $1 "
                "AND executed_at >= $2",
                TradeLogger.CLOSE_REASON_STOP_LOSS,
                since,
            )
        except Exception as e:
            logger.warning("查询止损记录失败", error=str(e)[:80])
            return 0, []

        # 按日统计（executed_at 已是北京时间无时区）
        daily = {}
        for r in rows:
            exec_time = r.get("executed_at")
            if not exec_time:
                continue
            day_str = exec_time.strftime("%m-%d")
            daily[day_str] = daily.get(day_str, 0) + 1

        trend = [{"date": d, "count": c} for d, c in sorted(daily.items())]
        return len(rows), trend

    async def _get_equity_loss_stats(self, days: int) -> tuple:
        """基于净资产快照计算连续亏损天数与最大单日回撤

        - 连续亏损天数：等比/等值判定「equity 较前日下降」的连续天数（从最近向前累计）。
        - 最大单日回撤：近 N 天快照中单日回撤率最大值；无快照返回 None。

        Args:
            days: 回看天数

        Returns:
            (consecutive_loss_days, (max_drawdown_period, drawdown_pct) | None)
        """
        try:
            since = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(days=days)
            rows = await self._db_manager.fetch_all(
                "SELECT snapshot_date, total_equity "
                "FROM public.equity_snapshot "
                "WHERE snapshot_date >= $1 "
                "ORDER BY snapshot_date ASC",
                since,
            )
        except Exception as e:
            logger.warning("查询净资产快照失败（表可能未初始化）", error=str(e)[:80])
            return 0, None

        if not rows:
            return 0, None

        # 连续亏损天数：从最近一天向前累计 equity 下降的连续天数
        equities = [(r["snapshot_date"], float(r["total_equity"])) for r in rows]
        consecutive = 0
        for i in range(len(equities) - 1, 0, -1):
            if equities[i][1] < equities[i - 1][1]:
                consecutive += 1
            else:
                break

        # 最大单日回撤：近窗口内单日 equity 相对前日的跌幅
        max_drop = None
        for i in range(1, len(equities)):
            prev, cur = equities[i - 1][1], equities[i][1]
            if prev <= 0:
                continue
            drop_pct = (prev - cur) / prev * 100
            if drop_pct < 0:
                continue
            if max_drop is None or drop_pct > max_drop[1]:
                max_drop = (equities[i][0], round(drop_pct, 2))

        return consecutive, max_drop