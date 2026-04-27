"""
标的注册管理 API 路由
"""

from fastapi import APIRouter, HTTPException, Query
from typing import List
from kline_data_service.models.registered_symbol import (
    RegisterRequest, RenewRequest, UnregisterRequest,
    RegisteredSymbolConfig, RegisteredSymbolList, RegisterResponse
)
from kline_data_service.core.registry import registry
from shared.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/register", tags=["标的注册管理"])


@router.post("", response_model=RegisterResponse, summary="注册新的标的")
async def register_symbol(request: RegisterRequest):
    """
    注册新的标的进行 K 线数据采集
    
    - **symbol**: 交易对符号，如 NEWCOINUSDT
    - **intervals**: 采集周期列表，如 ["1m", "5m", "15m", "1h"]
    - **duration_days**: 采集持续天数（1-30 天），默认 10 天
    - **priority**: 优先级（high, normal, low），默认 normal
    
    注册后，K 线服务将开始采集该标的的 K 线数据，直到过期或手动取消
    """
    try:
        # 从请求头获取调用方标识（如果有）
        created_by = "api"  # 可以从请求头获取更详细的信息
        
        config = await registry.register(request, created_by=created_by)
        
        logger.info(f"📝 API: 注册标的 {config.symbol}，过期时间：{config.expires_at}")
        
        return RegisterResponse(code=0, message="success", data=config)
        
    except Exception as e:
        logger.error(f"注册标的失败：{e}")
        raise HTTPException(status_code=500, detail=f"注册失败：{str(e)}")


@router.delete("", summary="取消注册")
async def unregister_symbol(symbol: str = Query(..., description="交易对符号")):
    """
    取消标的注册，停止 K 线数据采集
    
    - **symbol**: 要取消的交易对符号
    """
    try:
        success = await registry.unregister(symbol)
        
        if success:
            logger.info(f"📝 API: 取消注册 {symbol}")
            return {"code": 0, "message": "success", "data": {"symbol": symbol, "status": "cancelled"}}
        else:
            raise HTTPException(status_code=404, detail=f"标的 {symbol} 未注册")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"取消注册失败：{e}")
        raise HTTPException(status_code=500, detail=f"取消注册失败：{str(e)}")


@router.put("/renew", response_model=RegisterResponse, summary="续期")
async def renew_symbol(request: RenewRequest):
    """
    为已注册的标的续期
    
    - **symbol**: 交易对符号
    - **additional_days**: 续期天数（1-30 天）
    """
    try:
        config = await registry.renew(request.symbol, request.additional_days)
        
        if config:
            logger.info(f"📝 API: 续期 {config.symbol}，新过期时间：{config.expires_at}")
            return RegisterResponse(code=0, message="success", data=config)
        else:
            raise HTTPException(status_code=404, detail=f"标的 {request.symbol} 未注册")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"续期失败：{e}")
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
        logger.error(f"查询已注册标的失败：{e}")
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
        logger.error(f"查询标的配置失败：{e}")
        raise HTTPException(status_code=500, detail=f"查询失败：{str(e)}")
