"""K 线数据服务 API 路由"""

import time
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from datetime import datetime

from shared.utils.logger import get_logger
from shared.core.database import Database
from shared.utils.table_exists import table_exists
from core.binance_client import BinanceClient
from core.collector import KlineCollector
from core.indicator import TechnicalIndicatorCalculator
from core.registry import registry
from core.table_name_guard import (
    SymbolNotCollectedError,
    TableNameFormatError,
    TableNameValidationError,
    TableUnavailableError,
    build_kline_table_name,
    build_readable_table_name,
    build_table_name_by_format,
    read_non_negative_int,
)
from models.kline import KlineData

logger = get_logger(__name__)
router = APIRouter()

# 全局对象（由 main.py 初始化）
db: Optional[Database] = None
binance_client: Optional[BinanceClient] = None
collector: Optional[KlineCollector] = None


def init_globals(
    database: Database, client: BinanceClient, coll: KlineCollector
):
    """初始化全局对象"""
    global db, binance_client, collector
    db = database
    binance_client = client
    collector = coll


@router.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "timestamp": datetime.now()}


async def _table_exists(conn, table_name: str) -> bool:
    """检查表是否存在：复用唯一助手 table_exists（尊重 search_path、参数化）。

    历史实现硬编码 table_schema='public'，而 kline 表实际落在 search_path
    命中的 schema（生产为 btc_eth），导致判断恒为 False、相关放行分支不可达。
    """
    return await table_exists(conn, table_name)


def _validated_table_name(symbol: str, interval: str) -> str:
    """校验并返回合法表名（R01 单词点）；非法输入抛 HTTPException(400)，不触达任何 SQL

    本函数是 K 线路由的唯一实现（routes / registry_routes 共用），统一带 error 明细的提示文案。
    """
    try:
        return build_kline_table_name(symbol, interval, registry=registry)
    except TableNameValidationError as e:
        logger.warning(f"K 线查询参数非法：symbol={symbol!r} interval={interval!r} - {e}")
        raise HTTPException(status_code=400, detail=f"参数非法：{e}") from e


# 表存在性检查结果缓存：{table_name: (exists: bool, ts: float)}（TTL=0 时不写入）
_table_exists_cache: dict = {}


def _read_table_exists_cache_ttl() -> int:
    """读取表存在性缓存 TTL（秒）；缺失/非法（含 MagicMock）一律回退 0（实时查询）"""
    from shared.core.config import settings as _settings

    return read_non_negative_int(
        getattr(_settings, "EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS", None), 0
    )


def _cache_lookup(table_name: str, ttl: int) -> Optional[bool]:
    """命中未过期的缓存则返回存在性，未命中/已过期返回 None"""
    if ttl <= 0:
        return None
    entry = _table_exists_cache.get(table_name)
    if entry is not None and (time.monotonic() - entry[1]) < ttl:
        return entry[0]
    return None


async def _resolve_table_exists(conn, table_name: str) -> bool:
    """带 TTL 缓存的表存在性查询（复用参数化查询；异常不写缓存，交由上层 fail-closed）"""
    ttl = _read_table_exists_cache_ttl()
    cached = _cache_lookup(table_name, ttl)
    if cached is not None:
        return cached
    exists = bool(await _table_exists(conn, table_name))
    if ttl > 0:
        _table_exists_cache[table_name] = (exists, time.monotonic())
    return exists


def _make_table_exists_checker(conn):
    """构造注入给读路径 guard 的存在性回调（表名以绑定参数传入，绝不拼接）"""

    async def _check(table_name: str) -> bool:
        return await _resolve_table_exists(conn, table_name)

    return _check


def _precheck_read_format(symbol: str, interval: str) -> None:
    """读路径前置格式校验（无 DB）：非法输入在触达 DB 之前即抛 HTTPException(400)"""
    try:
        build_table_name_by_format(symbol, interval)
    except TableNameValidationError as e:
        logger.warning(f"K 线查询参数非法：symbol={symbol!r} interval={interval!r} - {e}")
        raise HTTPException(status_code=400, detail=f"参数非法：{e}") from e


async def _validated_read_table_name(conn, symbol: str, interval: str) -> str:
    """读路径唯一入口（P0-2/P0-C）：白名单 OR「表已存在且格式合法」。

    - 格式非法（注入风险）→ HTTPException(400)；
    - SymbolNotCollectedError / TableUnavailableError **原样上抛**，由
      `_resolve_ready_table` 与端点按类型转换为 200 空 / 503（不再解析 detail 文案）。
    """
    try:
        return await build_readable_table_name(
            symbol,
            interval,
            table_exists=_make_table_exists_checker(conn),
            registry=registry,
        )
    except TableNameFormatError as e:
        logger.warning(f"K 线查询参数非法：symbol={symbol!r} interval={interval!r} - {e}")
        raise HTTPException(status_code=400, detail=f"参数非法：{e}") from e


async def _ensure_table_ready(conn, table_name: str, symbol: str, interval: str) -> None:
    """确保查询表可用；表不存在时尝试自动建表。

    不可用（表缺失且采集器不可用 / 建表失败）→ 抛 TableUnavailableError（503 + 告警），
    不再静默返回「无数据」——那是「应采集却不可用」被误解为「未采集」的旧缺陷。
    """
    if await _resolve_table_exists(conn, table_name):
        return
    if not collector:
        raise TableUnavailableError(f"表 {table_name} 不存在且采集器不可用")
    try:
        await collector.ensure_table(symbol, interval)
    except Exception as e:  # noqa: BLE001 - 建表失败即视为该表不可用
        raise TableUnavailableError(f"K 线表自动创建失败：{table_name} - {e}") from e
    logger.info(f"K 线表自动创建成功：{table_name}")


async def _resolve_ready_table(conn, symbol: str, interval: str) -> str:
    """读路径共用（/klines/latest 与 /indicators）：校验表名并确保表可用。

    契约（P0-C-AC1）：
      - 未采集（SymbolNotCollectedError）→ 返回空字符串，调用方 200 空数据（不告警）；
      - 应采集却不可用（TableUnavailableError）/ 格式非法（HTTPException 400）→ 上抛。

    Returns:
        str: 可用表名；未采集时返回空字符串
    """
    try:
        table_name = await _validated_read_table_name(conn, symbol, interval)
    except SymbolNotCollectedError:
        return ""
    await _ensure_table_ready(conn, table_name, symbol, interval)
    return table_name


@router.get("/klines/latest")
async def get_latest_klines(
    symbol: str = Query(..., description="交易对，如 BTCUSDT"),
    interval: str = Query(..., description="时间间隔，如 1h"),
    limit: int = Query(10, ge=1, le=100, description="获取数量"),
):
    """
    获取最新 K 线数据

    Args:
        symbol: 交易对
        interval: 时间间隔
        limit: 获取数量

    Returns:
        K 线数据列表
    """
    try:
        if not db:
            raise HTTPException(status_code=500, detail="数据库未初始化")

        # P0-2：读路径前置格式校验（不触达 DB）；非法输入在连库之前即 400
        _precheck_read_format(symbol, interval)
        table_name = ""

        async with db.get_connection() as conn:
            # P0-2：白名单 OR「表已存在且格式合法」+ 确保表可用；不可用即返回「无数据」
            table_name = await _resolve_ready_table(conn, symbol, interval)
            if not table_name:
                return {"code": 0, "message": "无数据", "data": []}

            query = f"""
                SELECT * FROM {table_name}
                ORDER BY open_time DESC
                LIMIT :limit
            """
            rows = await conn.fetch_all(query, {"limit": limit})

            if not rows:
                return {"code": 0, "message": "无数据", "data": []}

            klines = []
            for row in rows:
                open_price = float(row["open_price"])
                close_price = float(row["close_price"])
                
                # 计算涨跌幅（相对于开盘价）
                price_change = close_price - open_price
                price_change_percent = (price_change / open_price * 100) if open_price > 0 else 0.0
                
                kline = {
                    "symbol": symbol,
                    "interval": interval,
                    "open_time": int(row["open_time"].timestamp() * 1000),
                    "open_price": open_price,
                    "high_price": float(row["high_price"]),
                    "low_price": float(row["low_price"]),
                    "close_price": close_price,
                    "volume": float(row["volume"]),
                    "close_time": int(row["close_time"].timestamp() * 1000),
                    "quote_volume": float(row["quote_volume"]),
                    "trade_count": row["trade_count"],
                    "taker_buy_volume": float(row["taker_buy_volume"]),
                    "taker_buy_quote_volume": float(row["taker_buy_quote_volume"]),
                    # 新增涨跌幅字段
                    "price_change": round(price_change, 2),
                    "price_change_percent": round(price_change_percent, 2),
                }
                klines.append(kline)

            # 反转顺序，按时间正序返回
            klines.reverse()

            return {"code": 0, "message": "success", "data": klines}

    except HTTPException:
        # 格式非法（注入风险）→ 400，原样上抛（不再按 detail 文案区分）
        raise
    except TableUnavailableError as e:
        # 应采集却不可用（建表失败/存在性查询异常）→ 503 + 告警（不再静默 200 空）
        logger.error(f"K 线数据不可用（应采集却缺失）：symbol={symbol} interval={interval} - {e}")
        raise HTTPException(status_code=503, detail="K线数据暂不可用") from e
    except Exception as e:
        error_msg = str(e)
        # 防御性处理：表存在检查有竞态条件时兜底
        if "does not exist" in error_msg:
            logger.warning(f"K 线表 {table_name} 不存在（竞态），返回空数据")
            return {"code": 0, "message": "无数据", "data": []}
        logger.error(f"获取 K 线数据失败：{e}")
        raise HTTPException(status_code=500, detail=error_msg)


@router.get("/indicators")
async def get_indicators(
    symbol: str = Query(..., description="交易对"),
    interval: str = Query(..., description="时间间隔"),
    period: int = Query(100, ge=10, le=500, description="计算周期"),
):
    """
    获取技术指标

    Args:
        symbol: 交易对
        interval: 时间间隔
        period: 用于计算的 K 线数量

    Returns:
        技术指标数据
    """
    try:
        if not db:
            raise HTTPException(status_code=500, detail="数据库未初始化")

        # P0-2：读路径前置格式校验（不触达 DB）；非法输入在连库之前即 400
        _precheck_read_format(symbol, interval)
        table_name = ""

        async with db.get_connection() as conn:
            # P0-2：白名单 OR「表已存在且格式合法」+ 确保表可用；不可用即返回「无数据」
            table_name = await _resolve_ready_table(conn, symbol, interval)
            if not table_name:
                return {"code": 0, "message": "无数据", "data": None}

            query = f"""
                SELECT * FROM {table_name}
                ORDER BY open_time DESC
                LIMIT :limit
            """
            rows = await conn.fetch_all(query, {"limit": period})

            if not rows:
                return {"code": 0, "message": "无数据", "data": None}

            # 转换为 KlineData 对象
            klines = []
            for row in reversed(rows):  # 按时间正序
                kline = KlineData(
                    symbol=symbol,
                    interval=interval,
                    open_time=int(row["open_time"].timestamp() * 1000),
                    open_price=float(row["open_price"]),
                    high_price=float(row["high_price"]),
                    low_price=float(row["low_price"]),
                    close_price=float(row["close_price"]),
                    volume=float(row["volume"]),
                    close_time=int(row["close_time"].timestamp() * 1000),
                    quote_volume=float(row["quote_volume"]),
                    trade_count=row["trade_count"],
                    taker_buy_volume=float(row["taker_buy_volume"]),
                    taker_buy_quote_volume=float(row["taker_buy_quote_volume"]),
                )
                klines.append(kline)

            # 计算指标
            indicators = TechnicalIndicatorCalculator.calculate_all_indicators(
                klines
            )

            if not indicators:
                return {
                    "code": 0,
                    "message": "数据不足，无法计算指标",
                    "data": None,
                }

            return {"code": 0, "message": "success", "data": indicators}

    except HTTPException:
        # 格式非法（注入风险）→ 400，原样上抛（不再按 detail 文案区分）
        raise
    except TableUnavailableError as e:
        # 应采集却不可用（建表失败/存在性查询异常）→ 503 + 告警（不再静默 200 空）
        logger.error(f"K 线指标不可用（应采集却缺失）：symbol={symbol} interval={interval} - {e}")
        raise HTTPException(status_code=503, detail="K线数据暂不可用") from e
    except Exception as e:
        error_msg = str(e)
        # 防御性处理：表存在检查有竞态条件时兜底
        if "does not exist" in error_msg:
            logger.warning(f"K 线表不存在（竞态），返回空数据")
            return {"code": 0, "message": "无数据", "data": None}
        logger.error(f"计算技术指标失败：{e}")
        raise HTTPException(status_code=500, detail=error_msg)


@router.post("/collect/manual")
async def manual_collect(
    symbol: str = Query(..., description="交易对"),
    interval: str = Query(..., description="时间间隔"),
    minutes: int = Query(5, ge=1, le=1440, description="采集最近 N 分钟（最大 1440 分钟=24 小时）"),
):
    """
    手动触发 K 线采集

    Args:
        symbol: 交易对
        interval: 时间间隔
        minutes: 采集最近多少分钟

    Returns:
        采集结果
    """
    try:
        if not collector:
            raise HTTPException(status_code=500, detail="采集器未初始化")

        # R01：采集入口同样先走 guard，非法 symbol/interval 拒绝，避免落到建表/查询
        table_name = _validated_table_name(symbol, interval)
        logger.info(f"手动采集 K 线：{table_name} 最近 {minutes} 分钟")

        stored = await collector.collect_recent(symbol, interval, minutes)

        return {
            "code": 0,
            "message": "success",
            "data": {
                "symbol": symbol,
                "interval": interval,
                "stored_count": stored,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"手动采集失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/collector/stats")
async def get_collector_stats():
    """
    获取采集器统计信息

    Returns:
        统计信息
    """
    try:
        if not collector:
            raise HTTPException(status_code=500, detail="采集器未初始化")

        stats = collector.get_stats()

        return {"code": 0, "message": "success", "data": stats}

    except Exception as e:
        logger.error(f"获取统计信息失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/symbols")
async def get_symbols():
    """
    获取支持的币种列表

    Returns:
        币种列表
    """
    try:
        if not collector:
            raise HTTPException(status_code=500, detail="采集器未初始化")

        return {
            "code": 0,
            "message": "success",
            "data": {"symbols": collector.symbols, "intervals": collector.intervals},
        }

    except Exception as e:
        logger.error(f"获取币种列表失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))
