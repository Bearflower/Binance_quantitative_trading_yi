"""K 线数据服务 API 路由"""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
from datetime import datetime

from shared.utils.logger import get_logger
from shared.core.database import Database
from core.binance_client import BinanceClient
from core.collector import KlineCollector
from core.indicator import TechnicalIndicatorCalculator
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
    """检查表是否存在（避免查询不存在的表触发 PostgreSQL 错误日志）"""
    query = """
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = :table_name AND table_schema = 'public'
        )
    """
    return await conn.fetch_val(query, {"table_name": table_name})


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

        table_name = f"kline_{symbol.lower()}_{interval}"

        async with db.get_connection() as conn:
            # 先检查表是否存在，避免触发 PostgreSQL relation does not exist 错误日志
            if not await _table_exists(conn, table_name):
                logger.info(f"K 线表 {table_name} 不存在，尝试自动创建")
                # 自动创建 K 线表（兜底机制），确保后续查询可用
                if collector:
                    try:
                        await collector.ensure_table(symbol, interval)
                        logger.info(f"K 线表自动创建成功: {table_name}")
                    except Exception as e:
                        logger.warning(f"K 线表自动创建失败: {table_name} - {e}")
                else:
                    logger.debug(f"K 线表 {table_name} 不存在且采集器未初始化，返回空数据")
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

        # 获取历史 K 线
        table_name = f"kline_{symbol.lower()}_{interval}"

        async with db.get_connection() as conn:
            # 先检查表是否存在，避免触发 PostgreSQL relation does not exist 错误日志
            if not await _table_exists(conn, table_name):
                logger.info(f"K 线表 {table_name} 不存在，尝试自动创建")
                # 自动创建 K 线表（兜底机制），确保后续查询可用
                if collector:
                    try:
                        await collector.ensure_table(symbol, interval)
                        logger.info(f"K 线表自动创建成功: {table_name}")
                    except Exception as e:
                        logger.warning(f"K 线表自动创建失败: {table_name} - {e}")
                else:
                    logger.debug(f"K 线表 {table_name} 不存在且采集器未初始化，返回空数据")
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
