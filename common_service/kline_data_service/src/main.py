"""
K 线数据服务

提供币安 K 线数据采集、存储和查询服务
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import asyncio

from shared.core.database import db_manager
from shared.core.config import settings
from shared.utils.logger import get_logger

from kline_data_service.core.binance_client import BinanceClient
from kline_data_service.core.collector import KlineCollector
from kline_data_service.core.scheduler import TaskScheduler
from kline_data_service.core.registry import registry
from kline_data_service.api import routes
from kline_data_service.api import registry_routes

logger = get_logger("kline_service")

# 全局对象
binance_client: BinanceClient
collector: KlineCollector
scheduler: TaskScheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global binance_client, collector, scheduler

    # 启动时
    logger.info("🚀 K 线数据服务启动中...")

    # 初始化数据库
    await db_manager.connect()
    logger.info("✅ 数据库连接成功")

    # 初始化标的注册管理器
    await registry.initialize()
    logger.info("✅ 标的注册管理器已初始化")

    # 初始化币安客户端
    binance_client = BinanceClient()
    await binance_client.connect()
    logger.info("✅ 币安 API 客户端已连接")

    # 初始化采集器（固定标的）
    collector = KlineCollector(
        binance_client=binance_client,
        db=db_manager,
        symbols=["BTCUSDT", "ETHUSDT", "BNBUSDT"],
        intervals=["15m", "1h", "4h", "1d"],
    )
    logger.info("✅ K 线采集器已初始化")

    # 初始化调度器
    scheduler = TaskScheduler(collector)

    # 添加固定标的的定时任务
    for symbol in ["BTCUSDT", "ETHUSDT", "BNBUSDT"]:
        for interval in ["15m", "1h", "4h", "1d"]:
            scheduler.add_job(symbol, interval)

    # 添加已注册标的的定时任务
    registered = registry.get_active_symbols()
    for config in registered:
        for interval in config.intervals:
            scheduler.add_job(config.symbol, interval)
        logger.info(f"📝 已添加注册标的 {config.symbol} 的采集任务：{config.intervals}")

    # 启动调度器
    scheduler.start()
    logger.info(f"✅ 定时任务调度器已启动 ({len(scheduler.get_tasks())} 个任务)")

    # 初始化 API 路由
    routes.init_globals(db_manager, binance_client, collector)

    logger.info("✅ K 线数据服务启动完成")

    yield

    # 关闭时
    logger.info("🛑 K 线数据服务关闭中...")

    # 停止调度器
    scheduler.shutdown(wait=False)
    logger.info("✅ 定时任务调度器已停止")

    # 关闭币安客户端
    await binance_client.disconnect()
    logger.info("✅ 币安 API 客户端已关闭")

    # 关闭数据库
    await db_manager.disconnect()
    logger.info("✅ 数据库连接已关闭")

    logger.info("✅ K 线数据服务已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="K 线数据服务",
    description="提供币安 K 线数据采集、存储和查询 API",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(routes.router, prefix=settings.API_PREFIX)
app.include_router(registry_routes.router, prefix=f"{settings.API_PREFIX}")


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "kline_data",
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": f"{settings.API_PREFIX}/health",
        "endpoints": {
            "klines": f"{settings.API_PREFIX}/klines/latest",
            "indicators": f"{settings.API_PREFIX}/indicators",
            "collect": f"{settings.API_PREFIX}/collect/manual",
            "stats": f"{settings.API_PREFIX}/collector/stats",
            "symbols": f"{settings.API_PREFIX}/symbols",
        },
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
    )
