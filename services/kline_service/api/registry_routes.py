"""
标的注册管理 API 路由

提供标的注册、取消注册、续期等功能，并动态管理采集任务。
"""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
from models.registered_symbol import (
    RegisterRequest, RenewRequest, UnregisterRequest,
    RegisteredSymbolConfig, RegisteredSymbolList, RegisterResponse
)
from core.registry import registry
from shared.core.config import settings
from shared.utils.logger import get_logger

logger = get_logger(__name__)

# 首次采集最小窗口（分钟），从 Settings 配置读取
_MIN_INITIAL_COLLECT_MINUTES = settings.MIN_INITIAL_COLLECT_MINUTES

router = APIRouter(prefix="/register", tags=["标的注册管理"])

# 全局引用（由 main.py 初始化）
_scheduler = None
_collector = None


def init_scheduler(scheduler):
    """
    初始化调度器引用
    
    Args:
        scheduler: TaskScheduler 实例
    """
    global _scheduler
    _scheduler = scheduler
    logger.info("注册管理 API 已关联调度器")


def init_collector(collector):
    """
    初始化采集器引用（用于注册后立即创建 K 线表）
    
    Args:
        collector: KlineCollector 实例
    """
    global _collector
    _collector = collector
    logger.info("注册管理 API 已关联采集器")


def get_scheduler():
    """
    获取调度器实例
    
    Returns:
        TaskScheduler 实例
        
    Raises:
        RuntimeError: 调度器未初始化
    """
    if _scheduler is None:
        raise RuntimeError("调度器未初始化，请先调用 init_scheduler()")
    return _scheduler


def get_collector():
    """
    获取采集器实例
    
    Returns:
        KlineCollector 实例
        
    Raises:
        RuntimeError: 采集器未初始化
    """
    if _collector is None:
        raise RuntimeError("采集器未初始化，请先调用 init_collector()")
    return _collector


def _get_collect_minutes(interval: str) -> int:
    """
    根据 K 线周期获取采集窗口（分钟数）
    
    Args:
        interval: K 线周期，如 '1h', '15m'
        
    Returns:
        采集窗口（分钟数）
    """
    INTERVAL_MINUTES = {
        '1m': 1, '5m': 5, '15m': 15, '30m': 30,
        '1h': 60, '2h': 120, '4h': 240,
        '6h': 360, '8h': 480, '12h': 720, '1d': 1440,
    }
    return INTERVAL_MINUTES.get(interval, 60)


def _add_collection_task(scheduler, symbol: str, interval: str) -> bool:
    """
    添加采集任务

    Args:
        scheduler: TaskScheduler 实例
        symbol: 交易对符号
        interval: 采集周期

    Returns:
        bool: 是否添加成功
    """
    try:
        scheduler.add_job(symbol, interval)
        logger.info(f"已添加采集任务：{symbol} {interval}")
        return True
    except Exception as e:
        logger.error(f"添加采集任务失败：{symbol} {interval} - {e}", exc_info=True)
        return False


def _remove_collection_task(scheduler, task_id: str) -> bool:
    """
    移除采集任务
    
    Args:
        scheduler: TaskScheduler 实例
        task_id: 任务 ID
        
    Returns:
        bool: 是否移除成功
    """
    try:
        scheduler.remove_task(task_id)
        logger.info(f"已移除采集任务：{task_id}")
        return True
    except Exception as e:
        logger.warning(f"移除采集任务失败：{task_id} - {e}", exc_info=True)
        return False


@router.post("", response_model=RegisterResponse, summary="注册新的标的")
async def register_symbol(request: RegisterRequest):
    """
    注册新的标的进行 K 线数据采集
    
    - **symbol**: 交易对符号，如 NEWCOINUSDT
    - **intervals**: 采集周期列表，如 ["1m", "5m", "15m", "1h"]
    - **duration_days**: 采集持续天数（1-30 天），默认 10 天
    - **priority**: 优先级（high, normal, low），默认 normal
    
    注册后，K 线服务将开始采集该标的的 K 线数据，直到过期或手动取消。
    同时会动态添加采集任务到调度器。
    """
    try:
        scheduler = get_scheduler()
        
        # 从请求头获取调用方标识（如果有）
        created_by = "api"  # 可以从请求头获取更详细的信息
        
        # 检查是否已存在活跃配置（用于判断是新增还是更新）
        existing_config = registry.get_symbol_config(request.symbol)
        is_new_registration = existing_config is None
        
        # 执行注册
        config = await registry.register(request, created_by=created_by)
        
        # 动态管理采集任务
        if is_new_registration:
            # 新注册：添加所有采集任务
            for interval in config.intervals:
                _add_collection_task(scheduler, config.symbol, interval)
        else:
            # 更新注册：先移除旧任务，再添加新任务
            old_intervals = set(existing_config.intervals)
            new_intervals = set(config.intervals)
            
            # 移除不再需要的任务
            removed_intervals = old_intervals - new_intervals
            for interval in removed_intervals:
                task_id = f"{config.symbol}_{interval}"
                _remove_collection_task(scheduler, task_id)
            
            # 添加新任务
            added_intervals = new_intervals - old_intervals
            for interval in added_intervals:
                _add_collection_task(scheduler, config.symbol, interval)
            
            # 对于已存在的任务，确保它们仍在运行
            common_intervals = old_intervals & new_intervals
            for interval in common_intervals:
                task_id = f"{config.symbol}_{interval}"
                if task_id not in scheduler.get_tasks():
                    _add_collection_task(scheduler, config.symbol, interval)
        
        # 注册后立即创建 K 线表并触发首次采集，避免策略查询时表不存在
        try:
            collector = get_collector()
            for interval in config.intervals:
                # 1. 确保 K 线表存在（即使没有数据也要创建）
                table_created = await collector.ensure_table(config.symbol, interval)
                # 2. 立即触发首次采集，确保至少 1000 分钟（~16.7h）的数据用于 ATR 计算
                # 策略的 ATR 周期为 14，1h 周期需要 15 根 K 线，即至少 900 分钟的数据
                minutes = max(_get_collect_minutes(interval), _MIN_INITIAL_COLLECT_MINUTES)
                stored = await collector.collect_recent(config.symbol, interval, minutes=minutes)
                if stored > 0:
                    logger.info(f"注册后首次采集成功：{config.symbol} {interval}，存储{stored}条数据")
                elif table_created:
                    logger.info(f"K 线表已创建，首次采集暂无数据（可能新币刚上线）：{config.symbol} {interval}")
        except Exception as e:
            logger.warning(f"注册后触发采集失败（不影响下次定时任务）：{config.symbol} - {e}")

        logger.info(f"API: 注册标的 {config.symbol}，过期时间：{config.expires_at}，采集周期：{config.intervals}")
        
        return RegisterResponse(code=0, message="success", data=config)
        
    except RuntimeError as e:
        logger.error(f"调度器未初始化：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"服务配置错误：{str(e)}")
    except Exception as e:
        logger.error(f"注册标的失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"注册失败：{str(e)}")


@router.delete("", summary="取消注册")
async def unregister_symbol(symbol: str = Query(..., description="交易对符号")):
    """
    取消标的注册，停止 K 线数据采集
    
    - **symbol**: 要取消的交易对符号
    
    取消注册后，会自动移除所有相关的采集任务。
    """
    try:
        scheduler = get_scheduler()
        
        # 获取配置信息（用于移除任务）
        config = registry.get_symbol_config(symbol)
        
        if config is None:
            raise HTTPException(status_code=404, detail=f"标的 {symbol} 未注册或已过期")
        
        # 移除所有采集任务
        removed_count = 0
        for interval in config.intervals:
            task_id = f"{symbol}_{interval}"
            if _remove_collection_task(scheduler, task_id):
                removed_count += 1
        
        # 执行取消注册
        success = await registry.unregister(symbol)
        
        if success:
            logger.info(f"API: 取消注册 {symbol}，已移除 {removed_count} 个采集任务")
            return {
                "code": 0,
                "message": "success",
                "data": {
                    "symbol": symbol,
                    "status": "cancelled",
                    "removed_tasks": removed_count
                }
            }
        else:
            raise HTTPException(status_code=404, detail=f"标的 {symbol} 未注册")
            
    except HTTPException:
        raise
    except RuntimeError as e:
        logger.error(f"调度器未初始化：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"服务配置错误：{str(e)}")
    except Exception as e:
        logger.error(f"取消注册失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"取消注册失败：{str(e)}")


@router.put("/renew", response_model=RegisterResponse, summary="续期")
async def renew_symbol(request: RenewRequest):
    """
    为已注册的标的续期
    
    - **symbol**: 交易对符号
    - **additional_days**: 续期天数（1-30 天）
    
    续期不会影响采集任务，只是延长过期时间。
    """
    try:
        config = await registry.renew(request.symbol, request.additional_days)
        
        if config:
            logger.info(f"API: 续期 {config.symbol}，新过期时间：{config.expires_at}")
            return RegisterResponse(code=0, message="success", data=config)
        else:
            raise HTTPException(status_code=404, detail=f"标的 {request.symbol} 未注册")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"续期失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"续期失败：{str(e)}")


@router.get("", response_model=RegisteredSymbolList, summary="查询已注册的标的")
async def get_registered_symbols(
    include_inactive: bool = Query(False, description="是否包含已过期/已取消的标的")
):
    """
    查询所有已注册的标的配置
    
    - **include_inactive**: 是否包含已过期/已取消的标的，默认 false
    """
    try:
        configs = registry.get_all_configs(include_inactive=include_inactive)
        
        return RegisteredSymbolList(
            code=0,
            message="success",
            data=configs,
            total=len(configs)
        )
        
    except Exception as e:
        logger.error(f"查询已注册标的失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"查询失败：{str(e)}")


@router.get("/{symbol}", response_model=RegisterResponse, summary="查询指定标的的配置")
async def get_symbol_config(symbol: str):
    """
    查询指定标的的注册配置
    
    - **symbol**: 交易对符号
    """
    try:
        config = registry.get_symbol_config(symbol)
        
        if config:
            return RegisterResponse(code=0, message="success", data=config)
        else:
            raise HTTPException(status_code=404, detail=f"标的 {symbol} 未注册或已过期")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询标的配置失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"查询失败：{str(e)}")


@router.get("/tasks/status", summary="查询采集任务状态")
async def get_tasks_status():
    """
    查询当前所有采集任务的状态
    
    返回：
    - 总任务数
    - 每个任务的详细信息（任务 ID、交易对、周期、下次运行时间等）
    """
    try:
        scheduler = get_scheduler()
        tasks = scheduler.get_tasks()
        
        task_list = []
        for task_id, task_info in tasks.items():
            next_run = scheduler.get_next_run_time(task_id)
            task_list.append({
                "task_id": task_id,
                "symbol": task_info["symbol"],
                "interval": task_info["interval"],
                "cron": task_info["cron"],
                "minutes": task_info["minutes"],
                "next_run_time": next_run.isoformat() if next_run else None
            })
        
        return {
            "code": 0,
            "message": "success",
            "data": {
                "total": len(task_list),
                "tasks": task_list
            }
        }
        
    except RuntimeError as e:
        logger.error(f"调度器未初始化：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"服务配置错误：{str(e)}")
    except Exception as e:
        logger.error(f"查询任务状态失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"查询失败：{str(e)}")


@router.get("/tasks/symbol/{symbol}", summary="查询指定标的的采集任务")
async def get_symbol_tasks(symbol: str):
    """
    查询指定标的的所有采集任务
    
    - **symbol**: 交易对符号
    
    返回该标的的所有采集任务状态。
    """
    try:
        scheduler = get_scheduler()
        tasks = scheduler.get_tasks()
        
        # 筛选该标的的任务
        symbol_tasks = {
            task_id: task_info
            for task_id, task_info in tasks.items()
            if task_info["symbol"] == symbol
        }
        
        task_list = []
        for task_id, task_info in symbol_tasks.items():
            next_run = scheduler.get_next_run_time(task_id)
            task_list.append({
                "task_id": task_id,
                "symbol": task_info["symbol"],
                "interval": task_info["interval"],
                "cron": task_info["cron"],
                "minutes": task_info["minutes"],
                "next_run_time": next_run.isoformat() if next_run else None
            })
        
        return {
            "code": 0,
            "message": "success",
            "data": {
                "symbol": symbol,
                "total": len(task_list),
                "tasks": task_list
            }
        }
        
    except RuntimeError as e:
        logger.error(f"调度器未初始化：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"服务配置错误：{str(e)}")
    except Exception as e:
        logger.error(f"查询标的任务失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"查询失败：{str(e)}")
