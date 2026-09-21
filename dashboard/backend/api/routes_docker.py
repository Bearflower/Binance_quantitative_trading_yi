"""
Dashboard API 路由（Docker容器版本）
定义所有 API 接口
"""
from typing import Literal

from fastapi import APIRouter, Query, HTTPException, Depends
from starlette.requests import Request
from starlette.responses import Response
from datetime import datetime, timedelta, timezone

import structlog

from models.schemas import (
    HealthResponse,
    MetadataResponse,
    OverviewResponse,
    StrategiesResponse,
    StrategyDetail,
    SymbolsResponse,
    TrendResponse,
    EquityResponse,
    EquityData,
    ErrorResponse,
    ReturnsResponse,
    ReturnsData,
    AiMonitorResponse,
    AiMonitorData,
    RiskResponse,
    RiskData,
)
from services.data_service_docker import DataService
from core.cache import CacheService
from core.config import strategy_config, settings


logger = structlog.get_logger()

# 创建路由器
router = APIRouter()

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=settings.timezone_offset))

# ========================================
# 数据服务依赖注入
# ========================================

def get_data_service() -> DataService:
    """获取数据服务实例"""
    return DataService()

def get_cache_service() -> CacheService:
    """获取缓存服务实例"""
    return cache_service

# 全局缓存服务实例
cache_service = CacheService()


# ========================================
# 健康检查接口
# ========================================

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="健康检查",
    description="检查 API 服务是否正常运行"
)
async def health_check():
    """
    健康检查接口
    
    返回服务状态和版本信息
    """
    return HealthResponse(
        code=0,
        message="服务正常",
        data={
            "status": "healthy",
            "version": settings.app_version,
            "timestamp": datetime.now(BEIJING_TZ).isoformat()
        }
    )


# ========================================
# 元数据接口
# ========================================

@router.get(
    "/metadata",
    response_model=MetadataResponse,
    summary="获取元数据",
    description="获取策略配置和系统元数据"
)
async def get_metadata(
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取元数据
    
    返回所有策略的配置信息
    """
    cache_key = "metadata"
    
    cached_data = cache.get(cache_key)
    if cached_data:
        return MetadataResponse(
            code=0,
            message="获取元数据成功（缓存）",
            data=cached_data
        )
    
    metadata = {
        "strategies": [
            {
                "id": strategy_id,
                "name": config["name"],
                "description": config["description"]
            }
            for strategy_id, config in strategy_config.items()
        ],
        "version": settings.app_version,
        "updated_at": datetime.now(BEIJING_TZ).isoformat()
    }
    
    cache.set(cache_key, metadata, ttl_seconds=settings.cache_ttl_metadata)
    
    return MetadataResponse(
        code=0,
        message="获取元数据成功",
        data=metadata
    )


# ========================================
# 总览数据接口
# ========================================

@router.get(
    "/overview",
    response_model=OverviewResponse,
    summary="获取总览数据",
    description="获取所有策略的总览统计数据"
)
async def get_overview(
    type: Literal["daily", "weekly", "monthly", "yearly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取总览数据
    
    返回所有策略的汇总统计数据
    """
    cache_key = f"overview:{type}"
    
    cached_data = cache.get(cache_key)
    if cached_data:
        return OverviewResponse(
            code=0,
            message="获取总览数据成功（缓存）",
            data=cached_data
        )
    
    overview_data = await data_service.get_overview(report_type=type)
    
    response_data = {
        "total_pnl": overview_data.get("total_pnl", 0),
        "total_gross_pnl": overview_data.get("total_gross_pnl", 0),
        "total_commission": overview_data.get("total_commission", 0),
        "total_orders": overview_data.get("total_orders", 0),
        "total_closed": overview_data.get("total_closed", 0),
        "total_wins": overview_data.get("total_wins", 0),
        "win_rate": overview_data.get("win_rate", 0),
        "strategies": overview_data.get("strategies", []),
        "report_type": type,
        "updated_at": datetime.now(BEIJING_TZ).isoformat()
    }
    
    _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
    ttl = _ttl_map.get(type, settings.cache_ttl_daily)
    cache.set(cache_key, response_data, ttl_seconds=ttl)
    
    return OverviewResponse(
        code=0,
        message="获取总览数据成功",
        data=response_data
    )


# ========================================
# 策略列表接口
# ========================================

@router.get(
    "/strategies",
    response_model=StrategiesResponse,
    summary="获取策略列表",
    description="获取所有策略的统计数据"
)
async def get_strategies(
    type: Literal["daily", "weekly", "monthly", "yearly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取策略列表
    
    返回所有策略的统计数据
    """
    cache_key = f"strategies:{type}"
    
    cached_data = cache.get(cache_key)
    if cached_data:
        return StrategiesResponse(
            code=0,
            message="获取策略列表成功（缓存）",
            data=cached_data
        )
    
    strategies_data = await data_service.get_strategies(report_type=type)
    
    response_data = {
        "strategies": strategies_data,
        "report_type": type,
        "updated_at": datetime.now(BEIJING_TZ).isoformat()
    }
    
    _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
    ttl = _ttl_map.get(type, settings.cache_ttl_daily)
    cache.set(cache_key, response_data, ttl_seconds=ttl)
    
    return StrategiesResponse(
        code=0,
        message="获取策略列表成功",
        data=response_data
    )


# ========================================
# 策略详情接口
# ========================================

@router.get(
    "/strategies/{strategy_id}",
    response_model=StrategyDetail,
    summary="获取策略详情",
    description="获取单个策略的详细统计数据"
)
async def get_strategy_detail(
    strategy_id: str,
    type: Literal["daily", "weekly", "monthly", "yearly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取策略详情
    
    返回单个策略的详细统计数据
    """
    cache_key = f"strategy:{strategy_id}:{type}"
    
    cached_data = cache.get(cache_key)
    if cached_data:
        return StrategyDetail(
            code=0,
            message="获取策略详情成功（缓存）",
            data=cached_data
        )
    
    strategy_data = await data_service.get_strategy_detail(
        strategy_id=strategy_id,
        report_type=type
    )
    
    if not strategy_data:
        raise HTTPException(
            status_code=404,
            detail=f"策略不存在：{strategy_id}"
        )
    
    response_data = {
        **strategy_data,
        "updated_at": datetime.now(BEIJING_TZ).isoformat()
    }
    
    _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
    ttl = _ttl_map.get(type, settings.cache_ttl_daily)
    cache.set(cache_key, response_data, ttl_seconds=ttl)
    
    return StrategyDetail(
        code=0,
        message="获取策略详情成功",
        data=response_data
    )


# ========================================
# 币种明细接口
# ========================================

@router.get(
    "/strategies/{strategy_id}/symbols",
    response_model=SymbolsResponse,
    summary="获取币种明细",
    description="获取策略下所有交易对的统计数据"
)
async def get_strategy_symbols(
    strategy_id: str,
    type: Literal["daily", "weekly", "monthly", "yearly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取币种明细
    
    返回策略下所有交易对的统计数据
    """
    cache_key = f"symbols:{strategy_id}:{type}"
    
    cached_data = cache.get(cache_key)
    if cached_data:
        return SymbolsResponse(
            code=0,
            message="获取币种明细成功（缓存）",
            data=cached_data
        )
    
    symbols_data = await data_service.get_strategy_symbols(
        strategy_id=strategy_id,
        report_type=type
    )
    
    response_data = {
        "strategy_id": strategy_id,
        "symbols": symbols_data,
        "report_type": type,
        "updated_at": datetime.now(BEIJING_TZ).isoformat()
    }
    
    _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
    ttl = _ttl_map.get(type, settings.cache_ttl_daily)
    cache.set(cache_key, response_data, ttl_seconds=ttl)
    
    return SymbolsResponse(
        code=0,
        message="获取币种明细成功",
        data=response_data
    )


# ========================================
# 合约账户净资产接口
# ========================================

@router.get(
    "/account/equity",
    response_model=EquityResponse,
    summary="获取合约账户净资产",
    description="获取合约账户净资产（含未实现盈亏），实时快照，不随日/周/月切换变化"
)
async def get_account_equity(
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取合约账户净资产

    使用 Binance PM 账户的 accountEquity（账户权益，含未实现盈亏），
    作为合约账户净资产实时快照展示。缓存时间短（30秒），保证近似实时。
    """
    cache_key = "account:equity"

    cached_data = cache.get(cache_key)
    if cached_data:
        return EquityResponse(
            code=0,
            message="获取合约账户净资产成功（缓存）",
            data=EquityData(**cached_data)
        )

    equity_data = await data_service.get_account_equity()

    cache.set(cache_key, equity_data, ttl_seconds=settings.cache_ttl_account)

    return EquityResponse(
        code=0,
        message="获取合约账户净资产成功",
        data=EquityData(**equity_data)
    )


# ========================================
# 趋势数据接口
# ========================================

@router.get(
    "/trend",
    response_model=TrendResponse,
    summary="获取趋势数据",
    description="获取收益趋势数据"
)
async def get_trend(
    type: Literal["daily", "weekly", "monthly", "yearly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
    days: int = Query(7, ge=1, le=30, description="天数或周数"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取趋势数据
    
    返回收益趋势数据
    """
    cache_key = f"trend:{type}:{days}"
    
    cached_data = cache.get(cache_key)
    if cached_data:
        return TrendResponse(
            code=0,
            message="获取趋势数据成功（缓存）",
            data=cached_data
        )
    
    trend_data = await data_service.get_trend_data(
        report_type=type,
        days=days
    )
    
    response_data = {
        "trends": trend_data,
        "report_type": type,
        "updated_at": datetime.now(BEIJING_TZ).isoformat()
    }
    
    _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
    ttl = _ttl_map.get(type, settings.cache_ttl_daily)
    cache.set(cache_key, response_data, ttl_seconds=ttl)
    
    return TrendResponse(
        code=0,
        message="获取趋势数据成功",
        data=response_data
    )


# ========================================
# 账户收益率接口（基于净资产快照）
# ========================================

@router.get(
    "/account/returns",
    response_model=ReturnsResponse,
    summary="获取账户收益率",
    description="获取账户日/周/月收益率（基于净资产快照增量），期初缺失时 yield_unavailable 为 true"
)
async def get_account_returns(
    period: Literal["daily", "weekly", "monthly", "yearly"] = Query("daily", description="周期：daily、weekly 或 monthly"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取账户收益率

    基于净资产快照计算日/周/月收益率，非 income API 口径。
    缓存时间短（30 秒），保证接近实时。
    """
    cache_key = f"account:returns:{period}"

    cached_data = cache.get(cache_key)
    if cached_data:
        return ReturnsResponse(
            code=0,
            message="获取账户收益率成功（缓存）",
            data=ReturnsData(**cached_data)
        )

    returns_data = await data_service.get_account_returns(period=period)

    cache.set(cache_key, returns_data, ttl_seconds=settings.cache_ttl_account)

    return ReturnsResponse(
        code=0,
        message="获取账户收益率成功",
        data=ReturnsData(**returns_data)
    )


# ========================================
# AI 监控接口
# ========================================

# AI 监控结果相对低频，中长 TTL 缓存（10 分钟）
_AI_MONITOR_CACHE_TTL = 600


@router.get(
    "/ai-monitor",
    response_model=AiMonitorResponse,
    summary="获取 AI 监控数据",
    description="聚合调优执行记录、月度资金分配、最近优化建议"
)
async def get_ai_monitor(
    weeks: int = Query(8, ge=1, le=26, description="调优记录回溯周数"),
    limit: int = Query(10, ge=1, le=20, description="返回条数上限"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取 AI 监控数据

    返回调优执行记录（public.ai_tuner_runs）、月度资金分配（capital_allocation active）、
    最近优化建议（strategy_memory）三部分。
    """
    cache_key = f"ai-monitor:{weeks}:{limit}"

    cached_data = cache.get(cache_key)
    if cached_data:
        return AiMonitorResponse(
            code=0,
            message="获取 AI 监控数据成功（缓存）",
            data=AiMonitorData(**cached_data)
        )

    ai_monitor_data = await data_service.get_ai_monitor(weeks=weeks, limit=limit)

    cache.set(cache_key, ai_monitor_data, ttl_seconds=_AI_MONITOR_CACHE_TTL)

    return AiMonitorResponse(
        code=0,
        message="获取 AI 监控数据成功",
        data=AiMonitorData(**ai_monitor_data)
    )


# ========================================
# 占用比历史趋势接口（AI监控 · 月度资金分配配套）
# ========================================

@router.get(
    "/position-utilization-trend",
    summary="获取各策略占用比历史趋势",
    description="从持仓对账历史明细表按天聚合各策略占用比（取每天最后一个小时的值）"
)
async def get_position_utilization_trend(
    days: int = Query(30, ge=7, le=60, description="回溯天数"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """返回各策略占用比历史趋势（按天聚合），供 AI监控 · 占用比趋势图使用"""
    cache_key = f"position-utilization-trend:{days}"

    cached_data = cache.get(cache_key)
    if cached_data:
        return {"code": 0, "message": "获取占用比趋势成功（缓存）", "data": cached_data}

    trend_data = await data_service.get_position_utilization_trend(days=days)
    cache.set(cache_key, trend_data, ttl_seconds=600)  # 占用比对账每小时更新，缓存 10 分钟即可

    return {"code": 0, "message": "获取占用比趋势成功", "data": trend_data}


# ========================================
# 风控指标接口
# ========================================

@router.get(
    "/risk",
    response_model=RiskResponse,
    summary="获取风控指标",
    description="获取持仓占用、阈值逼近/超限、止损统计、连续亏损与回撤等风控指标"
)
async def get_risk(
    days: int = Query(7, ge=1, le=30, description="回撤/亏损统计天数"),
    data_service: DataService = Depends(get_data_service),
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取风控指标

    阈值全部来自 dashboard 风控配置中心（risk.yaml），前端按占用率进度条展示。
    缓存时间短（30 秒）。
    """
    cache_key = f"risk:{days}"

    cached_data = cache.get(cache_key)
    if cached_data:
        return RiskResponse(
            code=0,
            message="获取风控指标成功（缓存）",
            data=RiskData(**cached_data)
        )

    risk_data = await data_service.get_risk(days=days)

    cache.set(cache_key, risk_data, ttl_seconds=settings.cache_ttl_account)

    return RiskResponse(
        code=0,
        message="获取风控指标成功",
        data=RiskData(**risk_data)
    )
