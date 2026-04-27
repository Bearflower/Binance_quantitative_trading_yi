"""
数据库管理模块
负责 PostgreSQL 数据库的连接、表结构创建、CRUD 操作封装
"""

import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
import os

from utils.logger import get_logger

logger = get_logger()


class DatabaseManager:
    """数据库管理类"""

    def __init__(self, connection_string: Optional[str] = None):
        """
        初始化数据库连接
        
        Args:
            connection_string: PostgreSQL 连接字符串，格式：
                              postgresql://user:password@host:port/database
                              如果为 None，则从环境变量读取
        """
        # 优先使用环境变量
        self.host = os.getenv('DB_HOST', 'localhost')
        self.port = os.getenv('DB_PORT', '5432')
        self.database = os.getenv('DB_NAME', 'stockfilter')
        self.user = os.getenv('DB_USER', 'stockfilter_user')
        self.password = os.getenv('DB_PASSWORD', 'Stock@2024')
        self.schema = os.getenv('DB_SCHEMA', 'schema_stockfilter')
        
        if connection_string is None:
            connection_string = f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"
        
        self.connection_string = connection_string
        self.conn = None
        self._connect()
        self._create_tables()

    def _connect(self):
        """建立数据库连接"""
        try:
            # 使用参数连接，避免密码中的特殊字符问题
            self.conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
                options=f'-c search_path={self.schema}'
            )
            logger.info(f"PostgreSQL 数据库连接成功 ({self.host}:{self.port}/{self.database})")
        except Exception as e:
            logger.error(f"PostgreSQL 数据库连接失败：{e}")
            raise

    def _create_tables(self):
        """创建数据库表结构"""
        cursor = self.conn.cursor()
        
        # 创建股票列表表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                code VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100),
                symbol VARCHAR(30),
                list_date DATE,
                sector VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 创建 K 线数据表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS klines (
                id SERIAL PRIMARY KEY,
                code VARCHAR(20) NOT NULL,
                date DATE NOT NULL,
                open NUMERIC(10,2),
                high NUMERIC(10,2),
                low NUMERIC(10,2),
                close NUMERIC(10,2),
                volume BIGINT,
                amount NUMERIC(20,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code, date)
            )
        """)
        
        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_klines_code ON klines(code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_klines_date ON klines(date)")

        # 创建扫描结果表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scan_results (
                id SERIAL PRIMARY KEY,
                scan_date DATE NOT NULL,
                code VARCHAR(20) NOT NULL,
                name VARCHAR(100),
                score NUMERIC(5,2),
                surge_date DATE,
                support_level NUMERIC(10,2),
                current_close NUMERIC(10,2),
                drop_rate NUMERIC(10,4),
                min_vol_ratio NUMERIC(10,4),
                surge_price NUMERIC(10,2),
                surge_volume_ratio NUMERIC(10,4),
                surge_pct NUMERIC(10,4),
                low_after_surge NUMERIC(10,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scan_code ON scan_results(code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scan_date ON scan_results(scan_date)")

        # 创建持仓表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id SERIAL PRIMARY KEY,
                code VARCHAR(20) NOT NULL,
                name VARCHAR(100),
                entry_date DATE,
                entry_price NUMERIC(10,2),
                position_size NUMERIC(10,2),
                current_price NUMERIC(10,2),
                pnl NUMERIC(10,2),
                pnl_pct NUMERIC(10,4),
                status VARCHAR(20) DEFAULT 'open',
                exit_date DATE,
                exit_price NUMERIC(10,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 创建推送历史表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS push_history (
                id SERIAL PRIMARY KEY,
                code VARCHAR(20) NOT NULL,
                push_date DATE NOT NULL,
                push_type VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code, push_date)
            )
        """)

        self.conn.commit()
        cursor.close()
        logger.info("数据库表结构创建完成")

    def get_stock_list(self, filters: Optional[Dict] = None) -> Optional[pd.DataFrame]:
        """获取股票列表"""
        query = "SELECT code, name, symbol, list_date, sector FROM stocks WHERE 1=1"

        if filters:
            if filters.get('exclude_st'):
                query += " AND name NOT LIKE '%ST%'"
            if filters.get('exclude_beijing'):
                query += " AND code NOT LIKE '8%'"
            if filters.get('min_list_days'):
                days = filters.get('min_list_days')
                # list_date 为 NULL 的股票保留（新股或数据缺失），只过滤 list_date 存在且不足 N 天的
                # 使用字符串拼接构建 interval，不需要参数绑定
                query += f" AND (list_date IS NULL OR list_date <= CURRENT_DATE - INTERVAL '{days} days')"

        # 不使用 params 参数，因为查询中没有使用参数占位符
        df = pd.read_sql_query(query, self.conn)
        return df

    def save_stock_list(self, df: pd.DataFrame):
        """保存股票列表"""
        if df is None or df.empty:
            return
        
        cursor = self.conn.cursor()
        
        for _, row in df.iterrows():
            cursor.execute("""
                INSERT INTO stocks (code, name, symbol, list_date, sector)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    symbol = EXCLUDED.symbol,
                    list_date = EXCLUDED.list_date,
                    sector = EXCLUDED.sector,
                    updated_at = CURRENT_TIMESTAMP
            """, (row['code'], row['name'], row['symbol'], 
                  row.get('list_date'), row.get('sector')))
        
        self.conn.commit()
        cursor.close()
        logger.info(f"保存 {len(df)} 只股票到数据库")

    def get_kline_history(self, code: str, days: int = 120) -> Optional[pd.DataFrame]:
        """获取 K 线历史数据"""
        query = """
            SELECT date, open, high, low, close, volume, amount
            FROM klines
            WHERE code = %s
            ORDER BY date DESC
            LIMIT %s
        """
        df = pd.read_sql_query(query, self.conn, params=(code, days))
        if df.empty:
            return None
        df['date'] = pd.to_datetime(df['date'])
        df.sort_values('date', inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def get_latest_kline_date(self, code: str) -> Optional[str]:
        """获取指定股票最新 K 线数据的日期"""
        query = """
            SELECT MAX(date) as latest_date
            FROM klines
            WHERE code = %s
        """
        cursor = self.conn.cursor()
        cursor.execute(query, (code,))
        result = cursor.fetchone()
        cursor.close()
        
        if result and result[0]:
            return result[0].strftime('%Y-%m-%d') if hasattr(result[0], 'strftime') else str(result[0])
        return None

    def save_kline_history(self, code: str, df: pd.DataFrame):
        """保存 K 线历史数据"""
        if df is None or df.empty:
            return
        
        cursor = self.conn.cursor()
        
        for _, row in df.iterrows():
            # 处理 volume 字段：转换为整数，处理 NaN 和超大值
            volume = row['volume']
            if pd.isna(volume):
                volume = 0
            else:
                try:
                    volume = int(volume)
                    # 检查是否超出 bigint 范围 (-9223372036854775808 到 9223372036854775807)
                    if volume > 9223372036854775807:
                        volume = 9223372036854775807
                    elif volume < -9223372036854775808:
                        volume = -9223372036854775808
                except (ValueError, OverflowError):
                    volume = 0
            
            # 处理 amount 字段
            amount = row.get('amount', 0)
            if pd.isna(amount):
                amount = 0
            
            cursor.execute("""
                INSERT INTO klines (code, date, open, high, low, close, volume, amount)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (code, date) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    amount = EXCLUDED.amount
            """, (code, row['date'].strftime('%Y-%m-%d'), row['open'], row['high'], 
                  row['low'], row['close'], volume, amount))
        
        self.conn.commit()
        cursor.close()
        logger.debug(f"{code} 保存 {len(df)} 条 K 线数据")

    def save_scan_result(self, scan_date: str, results: List[Dict]):
        """保存扫描结果"""
        if not results:
            return
        
        cursor = self.conn.cursor()
        
        for result in results:
            cursor.execute("""
                INSERT INTO scan_results (
                    scan_date, code, name, score, surge_date, support_level,
                    current_close, drop_rate, min_vol_ratio, surge_price,
                    surge_volume_ratio, surge_pct, low_after_surge
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
            """, (scan_date, result.get('code'), result.get('name'), result.get('score'),
                  result.get('surge_date'), result.get('support_level'),
                  result.get('current_close'), result.get('drop_rate'),
                  result.get('min_vol_ratio'), result.get('surge_price'),
                  result.get('surge_volume_ratio'), result.get('surge_pct'),
                  result.get('low_after_surge')))
        
        self.conn.commit()
        cursor.close()
        logger.info(f"保存 {len(results)} 条扫描结果")

    def has_pushed_today(self, code: str, today: Optional[str] = None) -> bool:
        """检查某只股票今日是否已推送"""
        if today is None:
            today = datetime.now().strftime('%Y-%m-%d')
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM push_history
            WHERE code = %s AND push_date = %s
        """, (code, today))
        result = cursor.fetchone()
        cursor.close()
        return result[0] > 0

    def save_push_history(self, code: str, push_date: str, push_type: str = 'daily_scan'):
        """保存推送历史"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO push_history (code, push_date, push_type)
            VALUES (%s, %s, %s)
            ON CONFLICT (code, push_date) DO NOTHING
        """, (code, push_date, push_type))
        self.conn.commit()
        cursor.close()

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")
