"""
Dashboard API 主程序（Docker容器版本）
FastAPI 应用入口
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import asyncio
import os
import random

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
import structlog

from api.routes_docker import router as api_router
from core.config import settings
from core.cache import cache_service
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from services.data_service_docker import DataService
from services.equity_snapshot_job import run_snapshot
from services.metric_precompute_job import run_precompute
from services.commission_reconcile_job import run_reconcile


logger = structlog.get_logger()


# ========================================
# 模拟数据服务（用于演示）
# ========================================

class MockDataService:
    """模拟数据服务，返回演示数据"""
    
    def __init__(self):
        self.strategies = ["btc_eth", "new_coin", "hrs"]
        self.strategy_names = {
            "btc_eth": "BTC/ETH MTPCS策略",
            "new_coin": "新币做空策略",
            "hrs": "HRS策略"
        }
    
    async def get_overview(self, report_type: str = "daily"):
        """获取总览数据"""
        strategies_data = []
        total_pnl = 0
        total_orders = 0
        total_closed = 0
        total_wins = 0
        
        for strategy_id in self.strategies:
            data = await self.get_strategy_detail(strategy_id, report_type)
            strategies_data.append(data)
            total_pnl += data["total_pnl"]
            total_orders += data["order_count"]
            total_closed += data["closed_count"]
            total_wins += data["win_count"]
        
        win_rate = (total_wins / total_closed * 100) if total_closed > 0 else 0
        
        return {
            "total_pnl": round(total_pnl, 2),
            "total_orders": total_orders,
            "total_closed": total_closed,
            "total_wins": total_wins,
            "win_rate": round(win_rate, 2),
            "strategies": strategies_data,
            "report_type": report_type,
            "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat()
        }
    
    async def get_strategies(self, report_type: str = "daily"):
        """获取策略列表"""
        strategies = []
        for strategy_id in self.strategies:
            data = await self.get_strategy_detail(strategy_id, report_type)
            strategies.append(data)
        return strategies
    
    async def get_strategy_detail(self, strategy_id: str, report_type: str = "daily"):
        """获取单个策略详情"""
        if strategy_id not in self.strategies:
            return None
        
        # 生成模拟数据
        order_count = random.randint(10, 50)
        fill_count = int(order_count * random.uniform(0.8, 0.95))
        closed_count = int(fill_count * random.uniform(0.6, 0.8))
        win_count = int(closed_count * random.uniform(0.4, 0.7))
        loss_count = closed_count - win_count
        total_pnl = random.uniform(-500, 1500)
        win_rate = (win_count / closed_count * 100) if closed_count > 0 else 0
        
        return {
            "strategy_id": strategy_id,
            "strategy_name": self.strategy_names.get(strategy_id, strategy_id),
            "order_count": order_count,
            "fill_count": fill_count,
            "closed_count": closed_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 2),
            "report_type": report_type,
            "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat()
        }
    
    async def get_strategy_symbols(self, strategy_id: str, report_type: str = "daily"):
        """获取策略币种明细"""
        if strategy_id not in self.strategies:
            return []
        
        # 生成模拟币种数据
        symbols = []
        if strategy_id == "btc_eth":
            symbol_list = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
        elif strategy_id == "new_coin":
            symbol_list = ["NEWUSDT", "COINUSDT", "TOKENUSDT", "TESTUSDT"]
        else:
            symbol_list = ["ETHUSDT", "BTCUSDT", "MATICUSDT"]
        
        for symbol in symbol_list:
            order_count = random.randint(5, 20)
            fill_count = int(order_count * random.uniform(0.8, 0.95))
            closed_count = int(fill_count * random.uniform(0.6, 0.8))
            total_pnl = random.uniform(-100, 300)
            win_rate = random.uniform(40, 70)
            
            symbols.append({
                "symbol": symbol,
                "order_count": order_count,
                "fill_count": fill_count,
                "closed_count": closed_count,
                "total_pnl": round(total_pnl, 2),
                "win_rate": round(win_rate, 2)
            })
        
        return symbols
    
    async def get_trend_data(self, report_type: str = "daily", days: int = 7):
        """获取趋势数据"""
        trends = []
        base_date = datetime.now(timezone(timedelta(hours=8)))
        
        for i in range(days):
            if report_type == "daily":
                date = base_date - timedelta(days=i)
                date_str = date.strftime("%Y-%m-%d")
            else:
                date = base_date - timedelta(weeks=i)
                date_str = date.strftime("%Y-W%W")
            
            trend = {
                "date": date_str,
                "total_pnl": round(random.uniform(-200, 500), 2),
                "order_count": random.randint(20, 60),
                "win_rate": round(random.uniform(40, 70), 2)
            }
            trends.append(trend)
        
        return list(reversed(trends))


# 创建模拟数据服务实例
data_service = MockDataService()


# ========================================
# 应用生命周期管理
# ========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理

    启动时：
      - 创建 APScheduler AsyncIOScheduler（北京时间）
      - 注册每日 23:30 的净资产快照任务
    关闭时：
      - 关闭调度器
    说明：生产实际数据走 routes_docker 的 DataService；此处仅注入快照调度，
    MockDataService 仅本机演示，不改其 data_service 逻辑。
    """
    logger.info(
        "Dashboard API 启动中",
        version="1.0.0",
        environment="production"
    )

    # 每日净资产快照调度器（北京时间 23:30）
    snapshot_scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    snapshot_scheduler.add_job(
        run_snapshot,
        CronTrigger(hour=23, minute=30, timezone="Asia/Shanghai"),
        args=[DataService()],
        id="equity_daily_snapshot",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    snapshot_scheduler.start()
    logger.info("净资产快照调度器已启动", schedule="每天 23:30 北京时间")

    # 指标预计算调度器（默认每 60 秒，走环境变量注入，禁止硬编码）
    precompute_service = DataService()
    precompute_interval = int(os.getenv("PRECOMPUTE_INTERVAL_SECONDS", "60"))
    # 启动期等待依赖（DB/网络）就绪的延迟：避免启动瞬间对未就绪依赖产生 DNS 等噪音告警
    startup_ready_delay = timedelta(seconds=int(os.getenv("STARTUP_READY_DELAY_SECONDS", "30")))
    precompute_scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    precompute_scheduler.add_job(
        run_precompute,
        IntervalTrigger(
            seconds=precompute_interval,
            start_date=(
                datetime.now() + startup_ready_delay
                if startup_ready_delay.total_seconds() > 0
                else None
            ),
        ),
        args=[precompute_service],
        id="metric_precompute",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    precompute_scheduler.start()
    logger.info("指标预计算调度器已启动", schedule=f"每 {precompute_interval} 秒")

    # 启动时预热一次（延迟至依赖就绪，既避免重启后首屏读库无数据，也不产生启动噪音）
    async def _run_after_ready(coro):
        if startup_ready_delay.total_seconds() > 0:
            await asyncio.sleep(startup_ready_delay.total_seconds())
        await coro

    asyncio.create_task(_run_after_ready(run_precompute(precompute_service)))

    # 持仓对账调度器（默认每 5 分钟，走环境变量注入，禁止硬编码），
    # 推导各策略持仓/保证金/占用比并落库 strategy_position_snapshot。
    # 注意：间隔必须小于持仓快照新鲜度超时(POSITION_FRESHNESS_TIMEOUT)，否则快照会中途判失效回退读残留表。
    position_service = DataService()
    position_interval = int(os.getenv("POSITION_RECONCILE_INTERVAL_SECONDS", "300"))
    position_scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    position_scheduler.add_job(
        position_service.compute_and_store_positions,
        IntervalTrigger(
            seconds=position_interval,
            start_date=(
                datetime.now() + startup_ready_delay
                if startup_ready_delay.total_seconds() > 0
                else None
            ),
        ),
        id="position_reconcile",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    position_scheduler.start()
    logger.info("持仓对账调度器已启动", schedule=f"每 {position_interval} 秒")

    # 启动时执行一次（延迟至依赖就绪，避免重启后持仓快照为空且无启动噪音）
    asyncio.create_task(_run_after_ready(position_service.compute_and_store_positions()))

    # 佣金回填调度器（默认每 1 小时，走环境变量注入，禁止硬编码），
    # 事后把 Binance userTrades 真实佣金回填 trade_records.commission
    commission_service = DataService()
    commission_interval = int(os.getenv("COMMISSION_RECONCILE_INTERVAL_SECONDS", "3600"))
    commission_scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    commission_scheduler.add_job(
        run_reconcile,
        IntervalTrigger(
            seconds=commission_interval,
            start_date=(
                datetime.now() + startup_ready_delay
                if startup_ready_delay.total_seconds() > 0
                else None
            ),
        ),
        args=[commission_service],
        id="commission_reconcile",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    commission_scheduler.start()
    logger.info("佣金回填调度器已启动", schedule=f"每 {commission_interval} 秒")

    yield

    if snapshot_scheduler.running:
        snapshot_scheduler.shutdown(wait=False)
    if precompute_scheduler.running:
        precompute_scheduler.shutdown(wait=False)
    if position_scheduler.running:
        position_scheduler.shutdown(wait=False)
    if commission_scheduler.running:
        commission_scheduler.shutdown(wait=False)
    logger.info("Dashboard API 关闭中")


# ========================================
# 创建 FastAPI 应用
# ========================================

app = FastAPI(
    title="Dashboard API",
    description="交易数据可视化看板 API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan
)

# 添加 Gzip 压缩中间件
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 禁止浏览器缓存 API 响应（确保切换日/周/月时数据实时更新）
@app.middleware("http")
async def add_no_cache_header(request: Request, call_next):
    """为 API 响应添加 Cache-Control: no-cache 头"""
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ========================================
# 注册路由
# ========================================

app.include_router(api_router, prefix="/api")


# ========================================
# 全局异常处理
# ========================================

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器"""
    logger.error(
        "未处理的异常",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        exc_info=True
    )
    
    return JSONResponse(
        status_code=500,
        content={
            "code": -1,
            "message": "服务器内部错误",
            "data": {}
        }
    )


# ========================================
# 根路径
# ========================================

@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "Dashboard API",
        "version": "1.0.0",
        "docs": "/api/docs"
    }
