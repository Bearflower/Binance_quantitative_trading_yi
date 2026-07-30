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

# ============================================
# 根本性防冲突机制 ⭐⭐⭐
# ============================================

# 启动互斥锁 ID（使用 "KLINE" 的 32 位哈希值，确保唯一性）
_STARTUP_LOCK_ID = 0x4B4C494E


async def _acquire_startup_lock():
    """
    获取启动互斥锁（PostgreSQL advisory lock）
    
    使用 pg_try_advisory_lock 确保同一时刻只有一个 kline 服务实例在运行。
    如果锁已被其他实例持有，则启动失败，避免多个实例同时写入同一数据库。
    
    注意：session 级 advisory lock 在数据库连接断开时自动释放，
    因此 kline 服务崩溃或正常关闭后锁会自动释放，不会产生死锁。
    """
    async with db_manager.get_connection() as conn:
        acquired = await conn.fetch_val(
            "SELECT pg_try_advisory_lock(:lock_id)",
            {"lock_id": _STARTUP_LOCK_ID}
        )
        if not acquired:
            raise RuntimeError(
                "启动互斥锁获取失败：另一个 kline 服务实例已在运行中。\n"
                "可能原因：\n"
                "  1. 旧容器未被清理，仍在运行中\n"
                "  2. 多个 kline 服务实例连接到了同一数据库\n"
                "解决方案：\n"
                "  1. 检查并杀死旧容器：docker ps | grep kline\n"
                "  2. 确认数据库连接配置是否正确\n"
            )
        logger.info("✅ 启动互斥锁已获取")


async def _verify_database_identity():
    """
    验证数据库身份
    
    确认当前连接的数据库是预期中的 trading_system-postgres，
    而不是误连到了 common_service_postgres 或其他数据库。
    通过检查预期的基础表是否存在来验证。
    """
    async with db_manager.get_connection() as conn:
        # 1. 记录当前数据库名
        db_name = await conn.fetch_val("SELECT current_database()")
        logger.info(f"数据库身份验证：当前数据库 = {db_name}")

        # 2. 检查是否包含预期的核心表
        expected_tables = ["kline_btcusdt_1h", "kline_btcusdt_15m", "kline_ethusdt_1h"]
        found_tables = []
        missing_tables = []

        for table_name in expected_tables:
            exists = await conn.fetch_val("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = :table_name AND table_schema = 'public'
                )
            """, {"table_name": table_name})
            if exists:
                found_tables.append(table_name)
            else:
                missing_tables.append(table_name)

        if found_tables:
            logger.info(f"数据库身份验证通过：找到 {len(found_tables)} 个预期核心表")
        else:
            logger.warning(
                f"数据库身份验证：在 {db_name} 中未找到任何预期核心表 "
                f"({', '.join(expected_tables)})。\n"
                "如果这是首次部署或新数据库，可忽略此警告。\n"
                "否则，请确认 kline 服务连接的是正确的数据库（trading_system-postgres）"
            )


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

    # 获取启动互斥锁，防止同一数据库被多个 kline 实例写入 ⭐⭐⭐
    try:
        await _acquire_startup_lock()
        logger.info("启动互斥锁已获取，确认无其他 kline 实例在运行")
    except RuntimeError as e:
        logger.error(f"启动互斥锁获取失败: {e}")
        raise
    except Exception as e:
        logger.error(f"启动互斥锁异常: {e}", exc_info=True)
        raise

    # 验证数据库身份，确认连接的是预期的 trading_system-postgres ⭐⭐⭐
    try:
        await _verify_database_identity()
    except Exception as e:
        logger.warning(f"数据库身份验证异常（不阻塞启动）：{e}", exc_info=True)

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

    # 启动时清理无效注册标的
    try:
        cleaned = await collector.validate_registered_symbols()
        if cleaned > 0:
            logger.info(f"🧹 启动时清理了 {cleaned} 个无效的注册标的（已在币安下架）")
        else:
            logger.debug("启动时注册标验证通过，无需清理")
    except Exception as e:
        logger.warning(f"启动时注册标验证失败（不影响后续启动）：{e}", exc_info=True)

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
