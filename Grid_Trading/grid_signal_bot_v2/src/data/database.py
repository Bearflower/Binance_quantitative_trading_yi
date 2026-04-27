"""
数据库连接管理模块
使用 PostgreSQL 存储信号推送历史和市场状态数据
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional, List, Dict, Any
from datetime import datetime
from decimal import Decimal

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


class DatabaseManager:
    """数据库管理器"""
    
    _instance: Optional["DatabaseManager"] = None
    _connection_pool: Optional[pool.SimpleConnectionPool] = None
    
    def __new__(cls) -> "DatabaseManager":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化数据库管理器"""
        if self._connection_pool is None:
            database_url = os.getenv(
                "DATABASE_URL",
                "postgresql://grid_user:password@localhost:5432/grid_trading"
            )
            
            try:
                self._connection_pool = pool.SimpleConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dsn=database_url,
                    cursor_factory=RealDictCursor
                )
                logger.info("数据库连接池初始化成功")
            except Exception as e:
                logger.error(f"数据库连接池初始化失败：{e}")
                raise
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接（上下文管理器）"""
        conn = None
        try:
            conn = self._connection_pool.getconn()
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"数据库操作失败：{e}")
            raise
        finally:
            if conn:
                self._connection_pool.putconn(conn)
    
    def execute_query(self, query: str, params: tuple = None) -> List[Dict[str, Any]]:
        """执行查询并返回结果"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params or ())
                if query.strip().upper().startswith('SELECT'):
                    return [dict(row) for row in cursor.fetchall()]
                conn.commit()
                return []
    
    def execute_one(self, query: str, params: tuple = None) -> Optional[Dict[str, Any]]:
        """执行查询并返回单行结果"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params or ())
                result = cursor.fetchone()
                if not query.strip().upper().startswith('SELECT'):
                    conn.commit()
                return dict(result) if result else None
    
    def save_signal(
        self,
        signal_time: datetime,
        market_state: str,
        symbol: str,
        grid_params: Dict[str, Any],
        is_pushed: bool = False
    ) -> int:
        """
        保存信号推送记录
        
        Args:
            signal_time: 信号时间
            market_state: 市场状态
            symbol: 交易对
            grid_params: 网格参数
            is_pushed: 是否已推送
            
        Returns:
            信号 ID
        """
        query = """
            INSERT INTO grid_signals (
                signal_time, market_state, symbol, grid_params, is_pushed
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """
        
        import json
        params = (
            signal_time,
            market_state,
            symbol,
            json.dumps(grid_params, default=str),
            is_pushed
        )
        
        result = self.execute_one(query, params)
        signal_id = result['id'] if result else None
        logger.info(f"信号已保存：ID={signal_id}, 状态={market_state}")
        return signal_id
    
    def get_recent_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取最近的信号记录"""
        query = """
            SELECT * FROM grid_signals
            ORDER BY signal_time DESC
            LIMIT %s
        """
        return self.execute_query(query, (limit,))
    
    def save_market_state(
        self,
        check_time: datetime,
        symbol: str,
        state: str,
        adx: Decimal,
        adx_4h: Optional[Decimal],
        trend_strength: Decimal,
        confidence: Decimal
    ) -> int:
        """
        保存市场状态记录
        
        Args:
            check_time: 检查时间
            symbol: 交易对
            state: 市场状态
            adx: 1H ADX 值
            adx_4h: 4H ADX 值
            trend_strength: 趋势强度系数
            confidence: 置信度
            
        Returns:
            记录 ID
        """
        query = """
            INSERT INTO market_states (
                check_time, symbol, state, adx, adx_4h,
                trend_strength, confidence
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        
        params = (
            check_time,
            symbol,
            state,
            adx,
            adx_4h,
            trend_strength,
            confidence
        )
        
        result = self.execute_one(query, params)
        state_id = result['id'] if result else None
        logger.info(f"市场状态已保存：ID={state_id}, 状态={state}")
        return state_id
    
    def get_recent_market_states(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        """获取最近的市场状态记录"""
        query = """
            SELECT * FROM market_states
            WHERE symbol = %s
            ORDER BY check_time DESC
            LIMIT %s
        """
        return self.execute_query(query, (symbol, limit))
    
    def save_grid_parameters(
        self,
        create_time: datetime,
        symbol: str,
        market_state: str,
        upper_price: Decimal,
        lower_price: Decimal,
        grid_count: int,
        grid_type: str,
        grid_direction: str,
        leverage: int,
        total_investment: Decimal,
        stop_upper_price: Optional[Decimal],
        stop_lower_price: Optional[Decimal],
        terminate_upper_price: Decimal,
        terminate_lower_price: Decimal,
        atr_value: Decimal
    ) -> int:
        """
        保存网格参数记录
        
        Returns:
            记录 ID
        """
        query = """
            INSERT INTO grid_parameters (
                create_time, symbol, market_state,
                upper_price, lower_price, grid_count, grid_type, grid_direction,
                leverage, total_investment,
                stop_upper_price, stop_lower_price,
                terminate_upper_price, terminate_lower_price,
                atr_value
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        
        params = (
            create_time, symbol, market_state,
            upper_price, lower_price, grid_count, grid_type, grid_direction,
            leverage, total_investment,
            stop_upper_price, stop_lower_price,
            terminate_upper_price, terminate_lower_price,
            atr_value
        )
        
        result = self.execute_one(query, params)
        param_id = result['id'] if result else None
        logger.info(f"网格参数已保存：ID={param_id}")
        return param_id
    
    def get_latest_grid_parameters(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取最新的网格参数"""
        query = """
            SELECT * FROM grid_parameters
            WHERE symbol = %s
            ORDER BY create_time DESC
            LIMIT 1
        """
        return self.execute_one(query, (symbol,))
    
    def close(self):
        """关闭连接池"""
        if self._connection_pool:
            self._connection_pool.closeall()
            logger.info("数据库连接池已关闭")


# 全局数据库管理器实例
_db_manager: Optional[DatabaseManager] = None


def get_db_manager() -> DatabaseManager:
    """获取全局数据库管理器实例"""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager
