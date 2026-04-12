"""
PostgreSQL 数据库管理 (异步版本)
使用 asyncpg 驱动支持高并发访问
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
import asyncpg

logger = logging.getLogger(__name__)


class DatabaseManager:
    """异步数据库管理器"""
    
    def __init__(self, db_url: str = None):
        """
        初始化数据库管理器
        
        Args:
            db_url: 数据库连接 URL
        """
        self.db_url = db_url or 'postgresql://grid_user:Grid@2024@postgres:5432/trading_platform?schema=schema_grid'
        self.pool: Optional[asyncpg.Pool] = None
    
    async def initialize(self) -> None:
        """初始化数据库连接池"""
        try:
            self.pool = await asyncpg.create_pool(
                dsn=self.db_url,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            logger.info(f"数据库连接池初始化完成：{self.db_url}")
        except Exception as e:
            logger.error(f"数据库初始化失败：{str(e)}")
            raise
    
    async def close(self) -> None:
        """关闭连接池"""
        if self.pool:
            await self.pool.close()
            logger.info("数据库连接池已关闭")
    
    async def execute(self, query: str, *args):
        """执行 SQL 语句"""
        async with self.pool.acquire() as conn:
            try:
                result = await conn.execute(query, *args)
                return result
            except Exception as e:
                logger.error(f"SQL 执行失败：{query}, 错误：{str(e)}")
                raise
    
    async def fetch(self, query: str, *args) -> List[Dict]:
        """查询多行数据"""
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(query, *args)
                return [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"SQL 查询失败：{query}, 错误：{str(e)}")
                raise
    
    async def fetchrow(self, query: str, *args) -> Optional[Dict]:
        """查询单行数据"""
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(query, *args)
                return dict(row) if row else None
            except Exception as e:
                logger.error(f"SQL 查询失败：{query}, 错误：{str(e)}")
                raise
    
    async def insert_trade(self, trade_data: Dict[str, Any]) -> int:
        """插入交易记录"""
        query = """
            INSERT INTO trades (
                trade_id, symbol, side, price, quantity,
                fee, fee_asset, timestamp, grid_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (trade_id) DO UPDATE SET
                price = EXCLUDED.price,
                quantity = EXCLUDED.quantity,
                fee = EXCLUDED.fee,
                timestamp = EXCLUDED.timestamp
            RETURNING id
        """
        
        result = await self.fetchrow(
            query,
            trade_data.get('trade_id'),
            trade_data.get('symbol'),
            trade_data.get('side'),
            trade_data.get('price'),
            trade_data.get('quantity'),
            trade_data.get('fee'),
            trade_data.get('fee_asset'),
            trade_data.get('timestamp'),
            trade_data.get('grid_id')
        )
        return result['id'] if result else 0
    
    async def insert_grid_history(self, grid_data: Dict[str, Any]) -> int:
        """插入网格历史记录"""
        query = """
            INSERT INTO grid_history (
                grid_id, symbol, upper_price, lower_price,
                grid_count, investment, state, market_state, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (grid_id) DO UPDATE SET
                state = EXCLUDED.state,
                market_state = EXCLUDED.market_state,
                pnl = EXCLUDED.pnl,
                terminated_at = EXCLUDED.terminated_at
            RETURNING id
        """
        
        result = await self.fetchrow(
            query,
            grid_data.get('grid_id'),
            grid_data.get('symbol'),
            grid_data.get('upper_price'),
            grid_data.get('lower_price'),
            grid_data.get('grid_count'),
            grid_data.get('investment'),
            grid_data.get('state'),
            grid_data.get('market_state'),
            grid_data.get('created_at')
        )
        return result['id'] if result else 0
    
    async def update_grid_terminated(self, grid_id: str, terminated_at: datetime, pnl: float) -> None:
        """更新网格终止信息"""
        query = """
            UPDATE grid_history
            SET terminated_at = $1, pnl = $2, state = 'TERMINATED'
            WHERE grid_id = $3
        """
        await self.execute(query, terminated_at, pnl, grid_id)
    
    async def insert_system_status(self, status_data: Dict[str, Any]) -> int:
        """插入系统状态记录"""
        query = """
            INSERT INTO system_status (
                timestamp, market_state, price, atr, adx,
                ema_fast, ema_slow, total_pnl, account_balance
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
        """
        
        result = await self.fetchrow(
            query,
            status_data.get('timestamp'),
            status_data.get('market_state'),
            status_data.get('price'),
            status_data.get('atr'),
            status_data.get('adx'),
            status_data.get('ema_fast'),
            status_data.get('ema_slow'),
            status_data.get('total_pnl'),
            status_data.get('account_balance')
        )
        return result['id'] if result else 0
    
    async def insert_risk_event(self, event_data: Dict[str, Any]) -> int:
        """插入风险事件记录"""
        query = """
            INSERT INTO risk_events (
                event_type, trigger_price, trigger_pnl,
                action_taken, timestamp, details
            ) VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """
        
        result = await self.fetchrow(
            query,
            event_data.get('event_type'),
            event_data.get('trigger_price'),
            event_data.get('trigger_pnl'),
            event_data.get('action_taken'),
            event_data.get('timestamp'),
            event_data.get('details')
        )
        return result['id'] if result else 0
    
    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict]:
        """获取最近的交易记录"""
        query = """
            SELECT * FROM trades
            WHERE symbol = $1
            ORDER BY timestamp DESC
            LIMIT $2
        """
        return await self.fetch(query, symbol, limit)
    
    async def get_grid_history(self, symbol: str, limit: int = 50) -> List[Dict]:
        """获取网格历史记录"""
        query = """
            SELECT * FROM grid_history
            WHERE symbol = $1
            ORDER BY created_at DESC
            LIMIT $2
        """
        return await self.fetch(query, symbol, limit)
    
    async def get_recent_system_status(self, limit: int = 100) -> List[Dict]:
        """获取最近的系统状态"""
        query = """
            SELECT * FROM system_status
            ORDER BY timestamp DESC
            LIMIT $1
        """
        return await self.fetch(query, limit)
    
    async def get_risk_events(self, event_type: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """获取风险事件记录"""
        if event_type:
            query = """
                SELECT * FROM risk_events
                WHERE event_type = $1
                ORDER BY timestamp DESC
                LIMIT $2
            """
            return await self.fetch(query, event_type, limit)
        else:
            query = """
                SELECT * FROM risk_events
                ORDER BY timestamp DESC
                LIMIT $1
            """
            return await self.fetch(query, limit)
    
    async def cleanup_old_data(self, days: int = 30) -> None:
        """清理旧数据"""
        # 清理交易记录
        await self.execute("""
            DELETE FROM trades
            WHERE created_at < NOW() - INTERVAL '%s days'
        """ % days)
        
        # 清理系统状态
        await self.execute("""
            DELETE FROM system_status
            WHERE timestamp < NOW() - INTERVAL '%s days'
        """ % days)
        
        logger.info(f"清理完成，保留{days}天数据")
    
    async def insert_parameter_adjustment(self, adjustment_data: Dict[str, Any]) -> int:
        """插入参数调整记录"""
        query = """
            INSERT INTO grid_parameter_adjustments (
                grid_id, timestamp, parameter_name,
                old_value, new_value, trigger_reason,
                market_state, atr_value, adjustment_type, details
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
        """
        
        result = await self.fetchrow(
            query,
            adjustment_data.get('grid_id'),
            adjustment_data.get('timestamp'),
            adjustment_data.get('parameter_name'),
            adjustment_data.get('old_value'),
            adjustment_data.get('new_value'),
            adjustment_data.get('trigger_reason'),
            adjustment_data.get('market_state'),
            adjustment_data.get('atr_value'),
            adjustment_data.get('adjustment_type', 'SWITCH'),
            adjustment_data.get('details', '')
        )
        return result['id'] if result else 0
    
    async def get_parameter_adjustments(self, grid_id: str, limit: int = 50) -> List[Dict]:
        """获取参数调整历史"""
        query = """
            SELECT * FROM grid_parameter_adjustments
            WHERE grid_id = $1
            ORDER BY timestamp DESC
            LIMIT $2
        """
        return await self.fetch(query, grid_id, limit)
    
    async def update_trailing_profit_state(self, trailing_data: Dict[str, Any]) -> int:
        """更新移动止盈状态"""
        # 检查是否已存在
        existing = await self.get_trailing_profit_state(trailing_data.get('grid_id'))
        
        if existing:
            # 更新
            query = """
                UPDATE trailing_profit_state
                SET peak_price = $1,
                    peak_pnl_percent = $2,
                    current_stop_price = $3,
                    last_updated = $4
                WHERE grid_id = $5
            """
            await self.execute(
                query,
                trailing_data.get('peak_price'),
                trailing_data.get('peak_pnl_percent'),
                trailing_data.get('current_stop_price'),
                datetime.now(),
                trailing_data.get('grid_id')
            )
            return existing['id']
        else:
            # 插入
            query = """
                INSERT INTO trailing_profit_state (
                    grid_id, activated_at, peak_price,
                    peak_pnl_percent, current_stop_price, last_updated
                ) VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """
            result = await self.fetchrow(
                query,
                trailing_data.get('grid_id'),
                trailing_data.get('activated_at'),
                trailing_data.get('peak_price'),
                trailing_data.get('peak_pnl_percent'),
                trailing_data.get('current_stop_price'),
                datetime.now()
            )
            return result['id'] if result else 0
    
    async def get_trailing_profit_state(self, grid_id: str) -> Optional[Dict]:
        """获取移动止盈状态"""
        query = """
            SELECT * FROM trailing_profit_state
            WHERE grid_id = $1
        """
        return await self.fetchrow(query, grid_id)
    
    async def delete_trailing_profit_state(self, grid_id: str) -> None:
        """删除移动止盈状态"""
        query = """
            DELETE FROM trailing_profit_state
            WHERE grid_id = $1
        """
        await self.execute(query, grid_id)


# 全局数据库实例
_db_manager: Optional[DatabaseManager] = None


def get_db_manager() -> DatabaseManager:
    """获取全局数据库管理器实例"""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager


async def init_db():
    """初始化数据库"""
    db = get_db_manager()
    await db.initialize()
    return db


async def close_db():
    """关闭数据库"""
    db = get_db_manager()
    await db.close()
