"""
Dashboard API 路由
定义所有 API 接口
"""
from typing import Literal

from fastapi import APIRouter, Query, HTTPException, Depends
from datetime import datetime

import structlog

from dashboard.backend.models.schemas import (
    HealthResponse,
    MetadataResponse,
    OverviewResponse,
    StrategiesResponse,
    StrategyDetail,
    SymbolsResponse,
    TrendResponse,
    ErrorResponse
)
from dashboard.backend.services.data_service import DataService
from dashboard.backend.core.cache import CacheService
from dashboard.backend.core.config import strategy_config, settings


logger = structlog.get_logger()

# 创建路由器
router = APIRouter()


# ========================================
# 依赖注入
# ========================================

def get_cache_service() -> CacheService:
    """获取缓存服务实例"""
    from dashboard.backend.core.cache import cache_service
    return cache_service


def get_data_service() -> DataService:
    """获取数据服务实例"""
    # 这里需要从应用状态获取数据服务
    # 在 main.py 中会设置应用状态
    from fastapi import Request
    # 临时返回，实际在 main.py 中通过 app.state 设置
    raise NotImplementedError("数据服务需要通过应用状态注入")


# ========================================
# 健康检查
# ========================================

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="健康检查",
    description="检查 API 服务和依赖服务的健康状态"
)
async def health_check():
    """
    健康检查接口

    返回服务状态、版本号和依赖服务状态。
    """
    return HealthResponse(
        status="ok",
        timestamp=datetime.now().isoformat(),
        version=settings.app_version,
        services={
            "database": "ok",
            "binance_api": "ok"
        }
    )


# ========================================
# 元数据
# ========================================

@router.get(
    "/metadata",
    response_model=MetadataResponse,
    summary="获取元数据",
    description="获取策略映射和时间范围等元数据"
)
async def get_metadata(
    cache: CacheService = Depends(get_cache_service)
):
    """
    获取元数据

    返回策略配置和可用的时间范围。
    """
    # 尝试从缓存获取
    cache_key = "metadata"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # 构建元数据
    from datetime import timedelta, timezone
    BEIJING_TZ = timezone(timedelta(hours=settings.timezone_offset))
    now = datetime.now(BEIJING_TZ)

    # 昨日
    yesterday = now.date() - timedelta(days=1)
    daily_range = {
        "start": yesterday.strftime("%Y-%m-%d"),
        "end": yesterday.strftime("%Y-%m-%d")
    }

    # 上周
    today = now.date()
    today_weekday = today.weekday()
    days_to_last_sunday = 0 if today_weekday == 6 else (today_weekday + 1)
    last_sunday = today - timedelta(days=days_to_last_sunday)
    last_monday = last_sunday - timedelta(days=6)
    weekly_range = {
        "start": last_monday.strftime("%Y-%m-%d"),
        "end": last_sunday.strftime("%Y-%m-%d")
    }

    metadata = MetadataResponse(
        strategies=strategy_config,
        time_range={
            "daily": daily_range,
            "weekly": weekly_range
        }
    )

    # 写入缓存
    cache.set(cache_key, metadata, settings.cache_ttl_metadata)

    return metadata


# ========================================
# 总览数据
# ========================================

@router.get(
    "/overview",
    response_model=OverviewResponse,
    summary="获取总览数据",
    description="获取所有策略的汇总数据"
)
async def get_overview(
    type: Literal["daily", "weekly", "monthly"] = Query(
        default="daily",
        description="数据类型：daily(日报)、weekly(周报) 或 monthly(月报)"
    ),
    cache: CacheService = Depends(get_cache_service),
    data_service: DataService = Depends(get_data_service)
):
    """
    获取总览数据

    返回所有策略的汇总统计信息。
    """
    # 尝试从缓存获取
    cache_key = f"overview:{type}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        # 获取数据
        if type == "daily":
            stats = await data_service.get_daily_stats()
        else:
            stats = await data_service.get_weekly_stats()

        # 格式化策略数据
        strategies = []
        total_pnl = 0
        total_orders = 0
        total_fills = 0
        total_closed = 0
        total_wins = 0

        for strategy_key, strategy_stats in stats.items():
            summary = data_service.format_strategy_summary(
                strategy_key,
                strategy_stats,
                strategy_config
            )
            strategies.append(summary)

            # 汇总
            total_pnl += float(summary.total_pnl)
            total_orders += summary.order_count
            total_fills += summary.fill_count
            total_closed += summary.closed_count
            total_wins += summary.win_count

        # 计算总胜率
        win_rate = (total_wins / total_closed * 100) if total_closed > 0 else 0.0

        # 构建响应
        response = OverviewResponse(
            total_pnl=f"{total_pnl:.2f}",
            total_orders=total_orders,
            total_fills=total_fills,
            total_closed=total_closed,
            win_rate=round(win_rate, 1),
            strategies=strategies,
            report_date=data_service.get_report_date(type),
            data_source="binance_api"
        )

        # 写入缓存
        _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
        ttl = _ttl_map.get(type, settings.cache_ttl_daily)
        cache.set(cache_key, response, ttl)

        return response

    except Exception as e:
        logger.error("获取总览数据失败", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATA_NOT_AVAILABLE",
                "message": f"数据不可用: {str(e)}",
                "details": {}
            }
        )


# ========================================
# 策略列表
# ========================================

@router.get(
    "/strategies",
    response_model=StrategiesResponse,
    summary="获取策略列表",
    description="获取所有策略的统计数据"
)
async def get_strategies(
    type: Literal["daily", "weekly", "monthly"] = Query(
        default="daily",
        description="数据类型：daily(日报)、weekly(周报) 或 monthly(月报)"
    ),
    cache: CacheService = Depends(get_cache_service),
    data_service: DataService = Depends(get_data_service)
):
    """
    获取策略列表

    返回所有策略的统计信息。
    """
    # 尝试从缓存获取
    cache_key = f"strategies:{type}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        # 获取数据
        if type == "daily":
            stats = await data_service.get_daily_stats()
        else:
            stats = await data_service.get_weekly_stats()

        # 格式化策略数据
        strategies = []
        for strategy_key, strategy_stats in stats.items():
            summary = data_service.format_strategy_summary(
                strategy_key,
                strategy_stats,
                strategy_config
            )
            strategies.append(summary)

        # 构建响应
        response = StrategiesResponse(
            strategies=strategies,
            report_date=data_service.get_report_date(type)
        )

        # 写入缓存
        _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
        ttl = _ttl_map.get(type, settings.cache_ttl_daily)
        cache.set(cache_key, response, ttl)

        return response

    except Exception as e:
        logger.error("获取策略列表失败", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATA_NOT_AVAILABLE",
                "message": f"数据不可用: {str(e)}",
                "details": {}
            }
        )


# ========================================
# 单个策略详情
# ========================================

@router.get(
    "/strategies/{strategy_id}",
    response_model=StrategyDetail,
    summary="获取策略详情",
    description="获取单个策略的详细数据"
)
async def get_strategy_detail(
    strategy_id: str,
    type: Literal["daily", "weekly", "monthly"] = Query(
        default="daily",
        description="数据类型：daily(日报)、weekly(周报) 或 monthly(月报)"
    ),
    cache: CacheService = Depends(get_cache_service),
    data_service: DataService = Depends(get_data_service)
):
    """
    获取策略详情

    返回单个策略的详细统计信息，包括币种明细和逐日分布。
    """
    # 检查策略是否存在
    if strategy_id not in strategy_config:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "STRATEGY_NOT_FOUND",
                "message": f"策略不存在: {strategy_id}",
                "details": {}
            }
        )

    # 尝试从缓存获取
    cache_key = f"strategy:{strategy_id}:{type}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        # 获取数据
        if type == "daily":
            stats = await data_service.get_daily_stats()
        else:
            stats = await data_service.get_weekly_stats()

        # 检查数据是否存在
        if strategy_id not in stats:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "STRATEGY_NOT_FOUND",
                    "message": f"策略数据不存在: {strategy_id}",
                    "details": {}
                }
            )

        # 格式化详情
        detail = data_service.format_strategy_detail(
            strategy_id,
            stats[strategy_id],
            strategy_config
        )

        # 写入缓存
        _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
        ttl = _ttl_map.get(type, settings.cache_ttl_daily)
        cache.set(cache_key, detail, ttl)

        return detail

    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取策略详情失败", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATA_NOT_AVAILABLE",
                "message": f"数据不可用: {str(e)}",
                "details": {}
            }
        )


# ========================================
# 币种明细
# ========================================

@router.get(
    "/strategies/{strategy_id}/symbols",
    response_model=SymbolsResponse,
    summary="获取币种明细",
    description="获取策略下各币种的统计数据"
)
async def get_strategy_symbols(
    strategy_id: str,
    type: Literal["daily", "weekly", "monthly"] = Query(
        default="daily",
        description="数据类型：daily(日报)、weekly(周报) 或 monthly(月报)"
    ),
    cache: CacheService = Depends(get_cache_service),
    data_service: DataService = Depends(get_data_service)
):
    """
    获取币种明细

    返回策略下各币种的统计信息。
    """
    # 检查策略是否存在
    if strategy_id not in strategy_config:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "STRATEGY_NOT_FOUND",
                "message": f"策略不存在: {strategy_id}",
                "details": {}
            }
        )

    # 尝试从缓存获取
    cache_key = f"symbols:{strategy_id}:{type}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        # 获取数据
        if type == "daily":
            stats = await data_service.get_daily_stats()
        else:
            stats = await data_service.get_weekly_stats()

        # 检查数据是否存在
        if strategy_id not in stats:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "STRATEGY_NOT_FOUND",
                    "message": f"策略数据不存在: {strategy_id}",
                    "details": {}
                }
            )

        # 格式化币种明细
        detail = data_service.format_strategy_detail(
            strategy_id,
            stats[strategy_id],
            strategy_config
        )

        response = SymbolsResponse(
            strategy_id=strategy_id,
            strategy_name=detail.name,
            symbols=detail.symbols
        )

        # 写入缓存
        _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
        ttl = _ttl_map.get(type, settings.cache_ttl_daily)
        cache.set(cache_key, response, ttl)

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取币种明细失败", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATA_NOT_AVAILABLE",
                "message": f"数据不可用: {str(e)}",
                "details": {}
            }
        )


# ========================================
# 趋势数据
# ========================================

@router.get(
    "/trend",
    response_model=TrendResponse,
    summary="获取趋势数据",
    description="获取各策略的历史趋势数据"
)
async def get_trend(
    type: Literal["daily", "weekly", "monthly"] = Query(
        default="daily",
        description="趋势类型：daily(日报)、weekly(周报) 或 monthly(月报)"
    ),
    days: int = Query(
        default=7,
        description="天数（日报默认7天，周报默认4周）"
    ),
    cache: CacheService = Depends(get_cache_service),
    data_service: DataService = Depends(get_data_service)
):
    """
    获取趋势数据

    返回各策略的历史趋势数据，用于图表展示。
    """
    # 尝试从缓存获取
    cache_key = f"trend:{type}:{days}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        # 获取趋势数据
        trend_data = await data_service.get_trend_data(type, days)

        # 构建日期列表
        from datetime import timedelta, timezone
        BEIJING_TZ = timezone(timedelta(hours=settings.timezone_offset))
        now = datetime.now(BEIJING_TZ)

        dates = []
        for i in range(days):
            date = now.date() - timedelta(days=days - i)
            dates.append(date.strftime("%Y-%m-%d"))

        response = TrendResponse(
            type=type,
            dates=dates,
            strategies=trend_data
        )

        # 写入缓存
        _ttl_map = {"daily": settings.cache_ttl_daily, "weekly": settings.cache_ttl_weekly, "monthly": settings.cache_ttl_monthly}
        ttl = _ttl_map.get(type, settings.cache_ttl_daily)
        cache.set(cache_key, response, ttl)

        return response

    except Exception as e:
        logger.error("获取趋势数据失败", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATA_NOT_AVAILABLE",
                "message": f"数据不可用: {str(e)}",
                "details": {}
            }
        )
