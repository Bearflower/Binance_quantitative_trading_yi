"""
已注册标的管理模块
"""

from typing import List, Optional, Dict
from datetime import datetime, timedelta
import json
from models.registered_symbol import (
    RegisteredSymbolConfig, RegisterRequest, RenewRequest,
    UnregisterRequest
)
from shared.core.database import db_manager
from shared.utils.logger import get_logger

logger = get_logger(__name__)


class SymbolRegistry:
    """标的注册管理器"""
    
    _instance: Optional["SymbolRegistry"] = None
    
    def __new__(cls) -> "SymbolRegistry":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化"""
        self._cache: Dict[str, RegisteredSymbolConfig] = {}
        self._initialized = False
    
    async def initialize(self) -> None:
        """初始化，从数据库加载配置"""
        if self._initialized:
            return
        
        await self._load_from_database()
        self._initialized = True
        logger.info(f"✅ 标的注册管理器已初始化，加载了 {len(self._cache)} 个配置")
    
    async def _load_from_database(self) -> None:
        """从数据库加载配置"""
        try:
            async with db_manager.get_connection() as conn:
                query = """
                    SELECT id, symbol, intervals, registered_at, expires_at, 
                           duration_days, priority, status, created_by, updated_at
                    FROM registered_symbols
                    WHERE status = 'active'
                """
                rows = await conn.fetch_all(query)
                
                for row in rows:
                    config = RegisteredSymbolConfig(
                        id=row['id'],
                        symbol=row['symbol'],
                        intervals=row['intervals'],
                        registered_at=row['registered_at'],
                        expires_at=row['expires_at'],
                        duration_days=row['duration_days'],
                        priority=row['priority'],
                        status=row['status'],
                        created_by=row['created_by'],
                        updated_at=row['updated_at']
                    )
                    self._cache[config.symbol] = config
                
                logger.info(f"从数据库加载了 {len(self._cache)} 个已注册标的配置")
                
        except Exception as e:
            logger.error(f"从数据库加载配置失败：{e}")
            raise
    
    async def register(self, request: RegisterRequest, created_by: str = "system") -> RegisteredSymbolConfig:
        """
        注册新的标的
        
        Args:
            request: 注册请求
            created_by: 注册方标识
            
        Returns:
            注册配置
        """
        # 检查是否已存在
        if request.symbol in self._cache:
            existing = self._cache[request.symbol]
            if existing.status == 'active':
                logger.warning(f"标的 {request.symbol} 已注册，将更新配置")
                # 更新现有配置
                return await self._update_registration(request, created_by)
            else:
                # 重新激活
                return await self._reactivate_registration(request, created_by)
        
        # 创建新配置
        config = RegisteredSymbolConfig.create(
            symbol=request.symbol,
            intervals=request.intervals,
            duration_days=request.duration_days,
            priority=request.priority,
            created_by=created_by
        )
        
        # 保存到数据库
        await self._save_to_database(config)
        
        # 更新缓存
        self._cache[config.symbol] = config
        
        logger.info(f"✅ 已注册标的：{config.symbol}，采集周期：{config.intervals}，过期时间：{config.expires_at}")
        
        return config
    
    async def _update_registration(self, request: RegisterRequest, created_by: str) -> RegisteredSymbolConfig:
        """更新现有注册"""
        config = self._cache[request.symbol]
        
        # 更新配置
        config.intervals = request.intervals
        config.priority = request.priority
        config.created_by = created_by
        config.updated_at = datetime.now()
        
        # 如果已过期，重新计算过期时间
        if config.is_expired():
            config.expires_at = datetime.now() + timedelta(days=request.duration_days)
            config.duration_days = request.duration_days
        else:
            # 如果未过期，延长过期时间（取较大值）
            new_expires = datetime.now() + timedelta(days=request.duration_days)
            if new_expires > config.expires_at:
                config.expires_at = new_expires
                config.duration_days = request.duration_days
        
        # 保存到数据库
        await self._save_to_database(config)
        
        logger.info(f"✅ 已更新标的注册：{config.symbol}，新过期时间：{config.expires_at}")
        
        return config
    
    async def _reactivate_registration(self, request: RegisterRequest, created_by: str) -> RegisteredSymbolConfig:
        """重新激活已取消/过期的注册"""
        config = self._cache[request.symbol]
        
        config.intervals = request.intervals
        config.duration_days = request.duration_days
        config.priority = request.priority
        config.created_by = created_by
        config.status = 'active'
        config.registered_at = datetime.now()
        config.expires_at = datetime.now() + timedelta(days=request.duration_days)
        config.updated_at = datetime.now()
        
        await self._save_to_database(config)
        
        logger.info(f"✅ 已重新激活标的：{config.symbol}")
        
        return config
    
    async def unregister(self, symbol: str) -> bool:
        """
        取消注册
        
        Args:
            symbol: 交易对符号
            
        Returns:
            是否成功
        """
        if symbol not in self._cache:
            logger.warning(f"标的 {symbol} 未注册，无法取消")
            return False
        
        config = self._cache[symbol]
        config.cancel()
        
        # 更新数据库
        async with db_manager.get_connection() as conn:
            query = """
                UPDATE registered_symbols 
                SET status = :status, updated_at = :updated_at
                WHERE symbol = :symbol
            """
            await conn.execute(query, {
                "status": config.status,
                "updated_at": config.updated_at,
                "symbol": symbol
            })
        
        logger.info(f"✅ 已取消标的注册：{symbol}")
        
        return True
    
    async def renew(self, symbol: str, additional_days: int) -> Optional[RegisteredSymbolConfig]:
        """
        续期
        
        Args:
            symbol: 交易对符号
            additional_days: 续期天数
            
        Returns:
            更新后的配置，如果不存在则返回 None
        """
        if symbol not in self._cache:
            logger.warning(f"标的 {symbol} 未注册，无法续期")
            return None
        
        config = self._cache[symbol]
        config.renew(additional_days)
        
        # 更新数据库
        async with db_manager.get_connection() as conn:
            query = """
                UPDATE registered_symbols 
                SET expires_at = :expires_at, duration_days = :duration_days, 
                    updated_at = :updated_at
                WHERE symbol = :symbol
            """
            await conn.execute(query, {
                "expires_at": config.expires_at,
                "duration_days": config.duration_days,
                "updated_at": config.updated_at,
                "symbol": symbol
            })
        
        logger.info(f"✅ 已续期标的：{symbol}，新过期时间：{config.expires_at}，剩余 {config.days_remaining()} 天")
        
        return config
    
    async def cleanup_expired(self) -> int:
        """
        清理过期的配置
        
        Returns:
            清理的数量
        """
        cleaned = 0
        
        for symbol, config in list(self._cache.items()):
            if config.is_expired() and config.status == 'active':
                config.expire()
                
                # 更新数据库
                async with db_manager.get_connection() as conn:
                    query = """
                        UPDATE registered_symbols 
                        SET status = :status, updated_at = :updated_at
                        WHERE symbol = :symbol
                    """
                    await conn.execute(query, {
                        "status": config.status,
                        "updated_at": config.updated_at,
                        "symbol": symbol
                    })
                
                logger.info(f"⏰ 标的 {symbol} 已过期，停止采集")
                cleaned += 1
        
        return cleaned
    
    def get_active_symbols(self) -> List[RegisteredSymbolConfig]:
        """获取所有活跃的配置"""
        return [
            config for config in self._cache.values()
            if config.status == 'active' and not config.is_expired()
        ]
    
    def get_symbol_config(self, symbol: str) -> Optional[RegisteredSymbolConfig]:
        """获取指定标的的配置"""
        config = self._cache.get(symbol)
        if config and config.status == 'active' and not config.is_expired():
            return config
        return None
    
    def get_all_configs(self, include_inactive: bool = False) -> List[RegisteredSymbolConfig]:
        """获取所有配置"""
        if include_inactive:
            return list(self._cache.values())
        return self.get_active_symbols()
    
    async def _save_to_database(self, config: RegisteredSymbolConfig) -> None:
        """保存到数据库"""
        async with db_manager.get_connection() as conn:
            # 检查是否存在
            check_query = """
                SELECT EXISTS (
                    SELECT 1 FROM registered_symbols WHERE symbol = :symbol
                )
            """
            exists = await conn.fetch_val(check_query, {"symbol": config.symbol})
            
            if exists:
                # 更新（不含 registered_at，该字段在创建后不再修改）
                query = """
                    UPDATE registered_symbols 
                    SET intervals = :intervals, expires_at = :expires_at,
                        duration_days = :duration_days, priority = :priority,
                        status = :status, created_by = :created_by,
                        updated_at = :updated_at
                    WHERE symbol = :symbol
                """
                params = {
                    "symbol": config.symbol,
                    "intervals": config.intervals,
                    "expires_at": config.expires_at,
                    "duration_days": config.duration_days,
                    "priority": config.priority,
                    "status": config.status,
                    "created_by": config.created_by,
                    "updated_at": config.updated_at
                }
            else:
                # 插入
                query = """
                    INSERT INTO registered_symbols (
                        symbol, intervals, registered_at, expires_at,
                        duration_days, priority, status, created_by, updated_at
                    ) VALUES (
                        :symbol, :intervals, :registered_at, :expires_at,
                        :duration_days, :priority, :status, :created_by, :updated_at
                    )
                """
                params = {
                    "symbol": config.symbol,
                    "intervals": config.intervals,
                    "registered_at": config.registered_at,
                    "expires_at": config.expires_at,
                    "duration_days": config.duration_days,
                    "priority": config.priority,
                    "status": config.status,
                    "created_by": config.created_by,
                    "updated_at": config.updated_at
                }
            
            await conn.execute(query, params)


# 全局实例
registry = SymbolRegistry()
