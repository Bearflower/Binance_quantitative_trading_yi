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
    ErrorResponse
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
    type: Literal["daily", "weekly", "monthly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
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
    type: Literal["daily", "weekly", "monthly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
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
    type: Literal["daily", "weekly", "monthly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
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
    type: Literal["daily", "weekly", "monthly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
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
# 趋势数据接口
# ========================================

@router.get(
    "/trend",
    response_model=TrendResponse,
    summary="获取趋势数据",
    description="获取收益趋势数据"
)
async def get_trend(
    type: Literal["daily", "weekly", "monthly"] = Query("daily", description="报告类型：daily、weekly 或 monthly"),
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
