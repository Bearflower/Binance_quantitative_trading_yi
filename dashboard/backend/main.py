"""
Dashboard API 主程序
FastAPI 应用入口
"""
from contextlib import asynccontextmanager
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
import structlog

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from dashboard.backend.api.routes import router as api_router
from dashboard.backend.core.config import settings, config
from dashboard.backend.core.cache import cache_service
from dashboard.backend.services.data_service import DataService
from dashboard.backend.collectors.collector import DailyReportCollector
from dashboard.backend.collectors.weekly_collector import WeeklyReportCollector
from shared.database import DatabaseManager


logger = structlog.get_logger()


# ========================================
# 应用生命周期管理
# ========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理

    在应用启动时初始化资源，在关闭时清理资源。
    """
    # 启动时初始化
    logger.info(
        "Dashboard API 启动中",
        version="1.0.0",
        environment="development" if settings.api_debug else "production"
    )

    # 初始化数据库连接
    db_manager = DatabaseManager(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_pool_size=settings.db_min_pool_size,
        max_pool_size=settings.db_max_pool_size
    )
    await db_manager.connect()
    logger.info("数据库连接已建立")

    # 初始化 Binance 客户端（可选）
    binance_client = None
    if settings.binance_api_key and settings.binance_api_secret:
        try:
            from shared.binance_api import BinanceClient
            binance_client = BinanceClient(
                api_key=settings.binance_api_key,
                api_secret=settings.binance_api_secret,
                testnet=settings.binance_testnet
            )
            logger.info("Binance 客户端已初始化")
        except Exception as e:
            logger.warning(
                "Binance 客户端初始化失败，将使用降级模式",
                error=str(e)
            )
    else:
        logger.info("未配置 Binance API，将使用降级模式")

    # 初始化采集器
    daily_collector = DailyReportCollector(db_manager, binance_client)
    weekly_collector = WeeklyReportCollector(db_manager, binance_client)

    # 初始化数据服务
    data_service = DataService(daily_collector, weekly_collector)

    # 将资源存储到应用状态
    app.state.db_manager = db_manager
    app.state.binance_client = binance_client
    app.state.data_service = data_service

    logger.info("Dashboard API 启动完成")

    yield  # 应用运行

    # 关闭时清理
    logger.info("Dashboard API 关闭中")

    # 关闭数据库连接
    await db_manager.disconnect()
    logger.info("数据库连接已关闭")

    # 清空缓存
    cache_service.clear()
    logger.info("缓存已清空")

    logger.info("Dashboard API 已关闭")


# ========================================
# 创建 FastAPI 应用
# ========================================

app = FastAPI(
    title="Dashboard API",
    description="交易数据可视化 Dashboard API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan
)


# ========================================
# 中间件配置
# ========================================

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应配置具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Gzip 压缩
app.add_middleware(GZipMiddleware, minimum_size=1000)


# 性能监控中间件
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """添加处理时间到响应头"""
    from time import time

    start_time = time()
    response = await call_next(request)
    process_time = time() - start_time

    response.headers["X-Process-Time"] = f"{process_time:.3f}"

    # 记录请求日志
    logger.info(
        "API 请求",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        process_time=f"{process_time:.3f}s"
    )

    return response


# ========================================
# 异常处理
# ========================================

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理"""
    logger.error(
        "未处理的异常",
        method=request.method,
        path=request.url.path,
        error=str(exc),
        exc_info=True
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "内部服务器错误",
                "details": {}
            }
        }
    )


# ========================================
# 注册路由
# ========================================

app.include_router(api_router, prefix="/api", tags=["dashboard"])


# ========================================
# 依赖注入覆盖
# ========================================

# 覆盖 routes.py 中的依赖注入
from dashboard.backend.api import routes

def get_data_service_override():
    """获取数据服务（从应用状态）"""
    def _get_data_service(request: Request):
        return request.app.state.data_service
    return _get_data_service

# 注意：这里需要在路由中正确设置依赖注入
# 由于 FastAPI 的依赖注入机制，这里采用应用状态的方式


# ========================================
# 根路径
# ========================================

@app.get("/", include_in_schema=False)
async def root():
    """根路径重定向到 API 文档"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/api/docs")


# ========================================
# 主函数
# ========================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_debug,
        log_level="info"
    )
