"""
PostgreSQL 数据库连接与管理模块

负责：
- 数据库连接管理
- 表结构创建
- 数据迁移
"""

import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime
from contextlib import asynccontextmanager

import asyncpg

from utils.logger import logger
from config.settings import settings


class DatabaseManager:
    """PostgreSQL 数据库管理器"""
    
    def __init__(self):
        """初始化数据库管理器"""
        self.pool: Optional[asyncpg.Pool] = None
        self.initialized = False
        
        logger.info("✅ 数据库管理器初始化完成")
    
    async def connect(self) -> bool:
        """
        连接到 PostgreSQL 数据库
        
        Returns:
            是否连接成功
        """
        try:
            # 从配置获取数据库连接信息
            db_config = self._parse_database_url(settings.database_url)
            
            logger.info(f"🔗 正在连接到 PostgreSQL: {db_config['host']}:{db_config['port']}/{db_config['database']}")
            
            # 创建连接池（asyncpg 不支持在 create_pool 时指定 schema）
            self.pool = await asyncpg.create_pool(
                host=db_config['host'],
                port=db_config['port'],
                user=db_config['user'],
                password=db_config['password'],
                database=db_config['database'],
                min_size=2,
                max_size=10,
                command_timeout=60
            )
            
            # 测试连接并设置 schema
            async with self.pool.acquire() as conn:
                await conn.fetchval('SELECT 1')
                # 设置 search_path 到指定的 schema（使用 identifier 转义防止 SQL 注入）
                schema = db_config.get('schema', 'public')
                # 验证 schema 名称只包含字母、数字和下划线
                if not all(c.isalnum() or c == '_' for c in schema):
                    raise ValueError(f"Invalid schema name: {schema}")
                await conn.execute(f'SET search_path TO "{schema}"')
                logger.info(f"✅ Schema 设置为：{schema}")
            
            logger.info("✅ PostgreSQL 连接成功")
            
            # 创建表结构
            await self.create_tables()
            
            self.initialized = True
            return True
            
        except Exception as e:
            logger.error(f"❌ 数据库连接失败：{e}")
            return False
    
    async def disconnect(self):
        """断开数据库连接"""
        if self.pool:
            await self.pool.close()
            logger.info("👋 PostgreSQL 连接已关闭")
    
    def _parse_database_url(self, url: str) -> Dict[str, str]:
        """
        解析数据库连接 URL
        
        支持格式:
        - postgresql://user:password@host:port/database?schema=schema_name
        - postgres://user:password@host:port/database
        """
        if url.startswith('postgresql://') or url.startswith('postgres://'):
            # 解析 URL 格式
            from urllib.parse import urlparse, parse_qs
            
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            
            return {
                'user': parsed.username,
                'password': parsed.password,
                'host': parsed.hostname,
                'port': parsed.port or 5432,
                'database': parsed.path.lstrip('/'),
                'schema': query.get('schema', ['public'])[0]
            }
        else:
            # 默认配置
            return {
                'host': 'localhost',
                'port': 5432,
                'user': 'short_selling_user',
                'password': 'ShortSell@2024',
                'database': 'short_selling_db',
                'schema': 'schema_short_selling'
            }
    
    async def create_tables(self):
        """创建数据库表结构"""
        logger.info("📋 创建数据库表结构...")
        
        async with self.pool.acquire() as conn:
            # 1. 新币信息表
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS new_listings (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) UNIQUE NOT NULL,
                    listing_time TIMESTAMP,
                    status VARCHAR(20) DEFAULT 'monitoring',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 2. 评分记录表
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS scores (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    contract_score REAL,
                    fundamental_score REAL,
                    technical_score REAL,
                    sentiment_score REAL,
                    total_score REAL,
                    veto_reason TEXT,
                    listing_hours REAL,
                    scoring_attempt INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 3. 信号记录表
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS signals (
                    id VARCHAR(50) PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    total_score REAL,
                    current_price REAL,
                    entry_min REAL,
                    entry_max REAL,
                    stop_loss REAL,
                    take_profit_1 REAL,
                    take_profit_2 REAL,
                    status VARCHAR(20) DEFAULT 'pending',
                    confirmed_at TIMESTAMP,
                    executed_at TIMESTAMP,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expire_at TIMESTAMP
                )
            ''')
            
            # 4. 交易记录表
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    order_id VARCHAR(50) UNIQUE,
                    symbol VARCHAR(20) NOT NULL,
                    entry_price REAL,
                    stop_loss REAL,
                    take_profit_1 REAL,
                    take_profit_2 REAL,
                    position_size REAL,
                    leverage INTEGER,
                    status VARCHAR(20) DEFAULT 'open',
                    exit_price REAL,
                    profit_loss REAL,
                    close_reason VARCHAR(20),
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建索引
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_scores_symbol ON scores(symbol);
                CREATE INDEX IF NOT EXISTS idx_scores_created ON scores(created_at);
                CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
                CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            ''')
            
            logger.info("✅ 数据库表结构创建完成")
    
    @asynccontextmanager
    async def get_connection(self):
        """获取数据库连接的上下文管理器"""
        if not self.pool:
            raise RuntimeError("数据库未连接")
        
        async with self.pool.acquire() as conn:
            # 设置 schema
            schema = self._parse_database_url(settings.database_url).get('schema', 'public')
            # 验证 schema 名称只包含字母、数字和下划线
            if not all(c.isalnum() or c == '_' for c in schema):
                raise ValueError(f"Invalid schema name: {schema}")
            await conn.execute(f'SET search_path TO "{schema}"')
            yield conn
    
    # ==================== 新币管理 ====================
    
    async def save_new_listing(self, symbol: str, listing_time: Optional[datetime] = None):
        """保存新币信息"""
        async with self.get_connection() as conn:
            await conn.execute('''
                INSERT INTO new_listings (symbol, listing_time, status)
                VALUES ($1, $2, 'monitoring')
                ON CONFLICT (symbol) DO NOTHING
            ''', symbol, listing_time)
    
    async def get_new_listings(self, hours: int = 72) -> List[Dict[str, Any]]:
        """获取指定时间范围内的新币"""
        async with self.get_connection() as conn:
            rows = await conn.fetch('''
                SELECT symbol, listing_time, status, created_at
                FROM new_listings
                WHERE listing_time >= NOW() - ($1 || ' hours')::INTERVAL
                ORDER BY listing_time DESC
            ''', str(hours))
            return [dict(row) for row in rows]
    
    # ==================== 评分管理 ====================
    
    async def save_score(self, symbol: str, score_data: Dict[str, Any]):
        """保存评分记录"""
        async with self.get_connection() as conn:
            await conn.execute('''
                INSERT INTO scores (
                    symbol, contract_score, fundamental_score, 
                    technical_score, sentiment_score, total_score,
                    veto_reason, listing_hours, scoring_attempt
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ''', 
                symbol,
                score_data.get('contract_score'),
                score_data.get('fundamental_score'),
                score_data.get('technical_score'),
                score_data.get('sentiment_score'),
                score_data.get('total_score'),
                score_data.get('veto_reason'),
                score_data.get('listing_hours'),
                score_data.get('scoring_attempt', 1)
            )
    
    async def get_scores(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        """获取币种的评分历史"""
        async with self.get_connection() as conn:
            rows = await conn.fetch('''
                SELECT * FROM scores
                WHERE symbol = $1
                ORDER BY created_at DESC
                LIMIT $2
            ''', symbol, limit)
            return [dict(row) for row in rows]
    
    # ==================== 信号管理 ====================
    
    async def save_signal(self, signal_data: Dict[str, Any]):
        """保存信号记录"""
        async with self.get_connection() as conn:
            await conn.execute('''
                INSERT INTO signals (
                    id, symbol, total_score, current_price,
                    entry_min, entry_max, stop_loss, take_profit_1, take_profit_2,
                    status, expire_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ''',
                signal_data.get('id'),
                signal_data.get('symbol'),
                signal_data.get('total_score'),
                signal_data.get('current_price'),
                signal_data.get('entry_min'),
                signal_data.get('entry_max'),
                signal_data.get('stop_loss'),
                signal_data.get('take_profit_1'),
                signal_data.get('take_profit_2'),
                signal_data.get('status', 'pending'),
                signal_data.get('expire_at'),
                signal_data.get('created_at')
            )
    
    async def update_signal_status(self, signal_id: str, status: str, 
                                   confirmed_at: Optional[datetime] = None,
                                   executed_at: Optional[datetime] = None,
                                   notes: str = ""):
        """更新信号状态"""
        async with self.get_connection() as conn:
            await conn.execute('''
                UPDATE signals
                SET status = $2, confirmed_at = $3, executed_at = $4, notes = $5
                WHERE id = $1
            ''', signal_id, status, confirmed_at, executed_at, notes)
    
    async def get_pending_signals(self) -> List[Dict[str, Any]]:
        """获取待确认信号"""
        async with self.get_connection() as conn:
            rows = await conn.fetch('''
                SELECT * FROM signals
                WHERE status = 'pending' AND expire_at > NOW()
                ORDER BY created_at DESC
            ''')
            return [dict(row) for row in rows]
    
    async def get_signal_by_id(self, signal_id: str) -> Optional[Dict[str, Any]]:
        """根据 ID 获取信号"""
        async with self.get_connection() as conn:
            row = await conn.fetchrow('''
                SELECT * FROM signals WHERE id = $1
            ''', signal_id)
            return dict(row) if row else None
    
    # ==================== 交易管理 ====================
    
    async def save_trade(self, trade_data: Dict[str, Any]):
        """保存交易记录"""
        async with self.get_connection() as conn:
            await conn.execute('''
                INSERT INTO trades (
                    order_id, symbol, entry_price, stop_loss,
                    take_profit_1, take_profit_2, position_size, leverage,
                    status, entry_time
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ''',
                trade_data.get('order_id'),
                trade_data.get('symbol'),
                trade_data.get('entry_price'),
                trade_data.get('stop_loss'),
                trade_data.get('take_profit_1'),
                trade_data.get('take_profit_2'),
                trade_data.get('position_size'),
                trade_data.get('leverage'),
                trade_data.get('status', 'open'),
                trade_data.get('entry_time')
            )
    
    async def update_trade(self, order_id: str, exit_price: float, 
                          profit_loss: float, close_reason: str):
        """更新交易记录（平仓）"""
        async with self.get_connection() as conn:
            await conn.execute('''
                UPDATE trades
                SET status = 'closed', exit_price = $2, profit_loss = $3, 
                    close_reason = $4, exit_time = NOW()
                WHERE order_id = $1
            ''', order_id, exit_price, profit_loss, close_reason)
    
    async def get_trades(self, symbol: Optional[str] = None, 
                        limit: int = 50) -> List[Dict[str, Any]]:
        """获取交易记录"""
        async with self.get_connection() as conn:
            if symbol:
                rows = await conn.fetch('''
                    SELECT * FROM trades
                    WHERE symbol = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                ''', symbol, limit)
            else:
                rows = await conn.fetch('''
                    SELECT * FROM trades
                    ORDER BY created_at DESC
                    LIMIT $1
                ''', limit)
            return [dict(row) for row in rows]
    
    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """获取未平仓位"""
        async with self.get_connection() as conn:
            rows = await conn.fetch('''
                SELECT * FROM trades
                WHERE status = 'open'
                ORDER BY entry_time DESC
            ''')
            return [dict(row) for row in rows]


# 全局数据库管理器实例
db_manager = DatabaseManager()
