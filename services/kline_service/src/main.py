"""
K 线数据服务

提供币安 K 线数据采集、存储和查询服务
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import asyncio
from typing import Optional

from shared.core.database import db_manager
from shared.core.config import settings
from shared.utils.logger import get_logger

from core.binance_client import BinanceClient
from core.collector import KlineCollector
from core.scheduler import TaskScheduler
from core.registry import registry
from api import routes
from api import registry_routes

logger = get_logger("kline_service")

# 全局对象（使用 Optional 类型提示，初始化为 None）
binance_client: Optional[BinanceClient] = None
collector: Optional[KlineCollector] = None
scheduler: Optional[TaskScheduler] = None

# CORS 配置：根据环境区分允许的来源
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:8080",
    "http://localhost:8000",
    "http://43.156.242.184",
    "http://43.156.242.184:8000",
] if settings.DEBUG else [
    "https://your-production-domain.com",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global binance_client, collector, scheduler

    # 启动时
    logger.info("K 线数据服务启动中...")

    # 初始化数据库
    try:
        await db_manager.connect()
        logger.info("数据库连接成功")
    except Exception as e:
        logger.error(f"数据库连接失败: {e}", exc_info=True)
        raise

    # 初始化标的注册管理器
    try:
        await registry.initialize()
        logger.info("标的注册管理器已初始化")
    except Exception as e:
        logger.error(f"标的注册管理器初始化失败: {e}", exc_info=True)
        raise

    # 初始化币安客户端
    try:
        binance_client = BinanceClient()
        await binance_client.connect()
        logger.info("币安 API 客户端已连接")
    except Exception as e:
        logger.error(f"币安 API 客户端连接失败: {e}", exc_info=True)
        raise

    # 初始化采集器（固定标的）
    try:
        # 固定标的：包含 MTPCS 策略需要的所有币种
        FIXED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]
        collector = KlineCollector(
            binance_client=binance_client,
            db=db_manager,
            symbols=FIXED_SYMBOLS,
            intervals=["15m", "1h", "4h", "1d"],
        )
        logger.info("K 线采集器已初始化")
    except Exception as e:
        logger.error(f"K 线采集器初始化失败: {e}", exc_info=True)
        raise

    # 初始化调度器
    try:
        scheduler = TaskScheduler(collector)

        # 添加固定标的的定时任务
        for symbol in FIXED_SYMBOLS:
            for interval in ["15m", "1h", "4h", "1d"]:
                scheduler.add_job(symbol, interval)

        # 添加已注册标的的定时任务
        registered = registry.get_active_symbols()
        for config in registered:
            for interval in config.intervals:
                scheduler.add_job(config.symbol, interval)
            logger.info(f"已添加注册标的 {config.symbol} 的采集任务：{config.intervals}")

        # 启动调度器
        scheduler.start()
        logger.info(f"定时任务调度器已启动 ({len(scheduler.get_tasks())} 个任务)")
    except Exception as e:
        logger.error(f"调度器初始化失败: {e}", exc_info=True)
        raise

    # 初始化 API 路由
    try:
        routes.init_globals(db_manager, binance_client, collector)
        
        # 初始化注册管理 API 的调度器和采集器引用
        # 调度器：用于动态添加/移除采集任务
        # 采集器：用于注册后立即创建 K 线表
        registry_routes.init_scheduler(scheduler)
        registry_routes.init_collector(collector)

        logger.info("K 线数据服务启动完成")
    except Exception as e:
        logger.error(f"API 路由初始化失败: {e}", exc_info=True)
        raise

    yield

    # 关闭时
    logger.info("K 线数据服务关闭中...")

    # 停止调度器
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
            logger.info("定时任务调度器已停止")
        except Exception as e:
            logger.error(f"调度器关闭失败: {e}", exc_info=True)

    # 关闭币安客户端
    if binance_client:
        try:
            await binance_client.disconnect()
            logger.info("币安 API 客户端已关闭")
        except Exception as e:
            logger.error(f"币安客户端关闭失败: {e}", exc_info=True)

    # 关闭数据库
    try:
        await db_manager.disconnect()
        logger.info("数据库连接已关闭")
    except Exception as e:
        logger.error(f"数据库关闭失败: {e}", exc_info=True)

    logger.info("K 线数据服务已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="K 线数据服务",
    description="提供币安 K 线数据采集、存储和查询 API",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# 添加 CORS 中间件（使用环境配置的允许来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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
            "register": f"{settings.API_PREFIX}/register",
            "tasks": f"{settings.API_PREFIX}/register/tasks/status",
        },
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
    )
