"""K 线数据采集器"""

import asyncio
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import traceback

from shared.core.database import DatabaseManager
from shared.utils.logger import get_logger
from .binance_client import BinanceClient
from .registry import registry
from models.kline import KlineData

logger = get_logger(__name__)


class KlineCollector:
    """K 线数据采集器"""

    def __init__(
        self,
        binance_client: BinanceClient,
        db: DatabaseManager,
        symbols: Optional[List[str]] = None,
        intervals: Optional[List[str]] = None,
    ):
        """
        初始化采集器

        Args:
            binance_client: 币安 API 客户端
            db: 数据库连接
            symbols: 要采集的交易对列表
            intervals: 要采集的时间间隔列表
        """
        self.binance_client = binance_client
        self.db = db

        # 默认配置
        self.symbols = symbols or [
            "BTCUSDT",
            "ETHUSDT",
            "BNBUSDT",
        ]
        self.intervals = intervals or ["15m", "1h", "4h", "1d"]

        self.running = False
        self.stats = {
            "total_collected": 0,
            "total_stored": 0,
            "total_errors": 0,
            "last_collect_time": None,
        }

    async def collect_klines(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 500,
    ) -> List[KlineData]:
        """
        采集单个交易对的 K 线数据

        Args:
            symbol: 交易对
            interval: 时间间隔
            start_time: 开始时间（毫秒）
            end_time: 结束时间（毫秒）
            limit: 每次请求数量

        Returns:
            KlineData 列表
        """
        try:
            raw_data = await self.binance_client.get_klines(
                symbol=symbol,
                interval=interval,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
            )

            if not raw_data:
                logger.warning(f"未获取到 {symbol} {interval} 的 K 线数据")
                return []

            klines = []
            for data in raw_data:
                try:
                    kline = KlineData.from_binance_data(symbol, interval, data)
                    klines.append(kline)
                except Exception as e:
                    logger.error(f"解析 K 线数据失败：{e}")
                    self.stats["total_errors"] += 1
                    continue

            logger.info(
                f"采集 {symbol} {interval} 完成，共 {len(klines)} 条数据"
            )
            self.stats["total_collected"] += len(klines)
            self.stats["last_collect_time"] = datetime.now()

            return klines

        except Exception as e:
            logger.error(f"采集 {symbol} {interval} 失败：{e}")
            self.stats["total_errors"] += 1
            return []

    async def store_klines(self, klines: List[KlineData]) -> int:
        """
        存储 K 线数据到数据库

        Args:
            klines: KlineData 列表

        Returns:
            成功存储的数量
        """
        if not klines:
            return 0

        try:
            # 按 symbol 和 interval 分组
            grouped = {}
            for kline in klines:
                key = (kline.symbol, kline.interval)
                if key not in grouped:
                    grouped[key] = []
                grouped[key].append(kline.to_dict())

            # 批量插入
            total_stored = 0
            for (symbol, interval), data_list in grouped.items():
                table_name = f"kline_{symbol.lower()}_{interval}"
                stored = await self._batch_insert(table_name, data_list)
                total_stored += stored

            self.stats["total_stored"] += total_stored
            logger.info(f"存储 {total_stored} 条 K 线数据到数据库")

            return total_stored

        except Exception as e:
            logger.error(f"存储 K 线数据失败：{e}")
            self.stats["total_errors"] += 1
            return 0

    async def _batch_insert(
        self, table_name: str, data_list: List[Dict]
    ) -> int:
        """
        批量插入数据

        Args:
            table_name: 表名
            data_list: 数据列表

        Returns:
            插入数量
        """
        try:
            async with self.db.get_connection() as conn:
                # 检查表是否存在，不存在则创建
                await self._create_table_if_not_exists(
                    conn, table_name, data_list[0] if data_list else {}
                )

                # 逐条插入（使用命名参数）
                inserted = 0
                for data in data_list:
                    query = f"""
                        INSERT INTO {table_name} (
                            open_time, open_price, high_price, low_price, close_price,
                            volume, close_time, quote_volume, trade_count,
                            taker_buy_volume, taker_buy_quote_volume
                        ) VALUES (
                            :open_time, :open_price, :high_price, :low_price, :close_price,
                            :volume, :close_time, :quote_volume, :trade_count,
                            :taker_buy_volume, :taker_buy_quote_volume
                        )
                        ON CONFLICT (open_time) DO NOTHING
                    """
                    # 只提取 SQL 查询中需要的字段
                    values = {
                        'open_time': data['open_time'],
                        'open_price': data['open_price'],
                        'high_price': data['high_price'],
                        'low_price': data['low_price'],
                        'close_price': data['close_price'],
                        'volume': data['volume'],
                        'close_time': data['close_time'],
                        'quote_volume': data['quote_volume'],
                        'trade_count': data['trade_count'],
                        'taker_buy_volume': data['taker_buy_volume'],
                        'taker_buy_quote_volume': data['taker_buy_quote_volume']
                    }
                    await conn.execute(query, values)
                    inserted += 1
                
                logger.info(f"成功插入 {inserted} 条数据到 {table_name}")
                return inserted

        except Exception as e:
            logger.error(f"批量插入 {table_name} 失败：{e}")
            raise

    async def _create_table_if_not_exists(
        self, conn, table_name: str, sample_data: Dict
    ):
        """检查表是否存在，不存在则创建"""
        check_query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = :table_name
            )
        """
        exists = await conn.fetch_val(check_query, {"table_name": table_name})

        if not exists:
            logger.info(f"创建表：{table_name}")
            create_query = f"""
                CREATE TABLE {table_name} (
                    id SERIAL PRIMARY KEY,
                    open_time TIMESTAMP NOT NULL UNIQUE,
                    open_price DECIMAL(20, 8) NOT NULL,
                    high_price DECIMAL(20, 8) NOT NULL,
                    low_price DECIMAL(20, 8) NOT NULL,
                    close_price DECIMAL(20, 8) NOT NULL,
                    volume DECIMAL(30, 8) NOT NULL,
                    close_time TIMESTAMP NOT NULL,
                    quote_volume DECIMAL(30, 8) NOT NULL,
                    trade_count INTEGER NOT NULL,
                    taker_buy_volume DECIMAL(30, 8) NOT NULL,
                    taker_buy_quote_volume DECIMAL(30, 8) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            await conn.execute(create_query)

            # 创建索引
            await conn.execute(
                f"CREATE INDEX idx_{table_name}_open_time ON {table_name} (open_time)"
            )
            await conn.execute(
                f"CREATE INDEX idx_{table_name}_close_time ON {table_name} (close_time)"
            )

    async def collect_all(self) -> int:
        """
        采集所有配置的币种和周期

        Returns:
            采集的数据条数
        """
        total = 0

        for symbol in self.symbols:
            for interval in self.intervals:
                klines = await self.collect_klines(symbol, interval)
                if klines:
                    stored = await self.store_klines(klines)
                    total += stored

        logger.info(f"批量采集完成，共存储 {total} 条数据")
        return total

    async def ensure_table(self, symbol: str, interval: str) -> bool:
        """
        确保 K 线表存在（如果不存在则创建空表）
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            
        Returns:
            是否创建成功（或已存在）
        """
        table_name = f"kline_{symbol.lower()}_{interval}"
        try:
            async with self.db.get_connection() as conn:
                check_query = """
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = :table_name
                    )
                """
                exists = await conn.fetch_val(check_query, {"table_name": table_name})
                if not exists:
                    logger.info(f"预创建 K 线表：{table_name}")
                    await self._create_table_if_not_exists(conn, table_name, {})
                    logger.info(f"K 线表创建成功：{table_name}")
                return True
        except Exception as e:
            logger.error(f"确保 K 线表存在失败：{table_name} - {e}")
            return False

    async def collect_recent(
        self, symbol: str, interval: str, minutes: int = 5
    ) -> int:
        """
        采集最近 N 分钟的 K 线数据（采集已收盘的 K 线）
        
        逻辑：采集上一个完整周期的 K 线数据
        例如：
        - 15m 周期：在 03:15 时，采集 02:45-03:00 的 K 线
        - 1h 周期：在 03:05 时，采集 02:00-03:00 的 K 线

        Args:
            symbol: 交易对
            interval: 时间间隔
            minutes: 采集最近多少分钟（通常设置为周期长度）

        Returns:
            存储的数据条数
        """
        # 计算采集时间范围
        # 结束时间：当前时间往前推 1 分钟（确保 K 线已收盘）
        # 开始时间：结束时间往前推 minutes 分钟
        now = datetime.now()
        end_time = int((now - timedelta(minutes=1)).timestamp() * 1000)
        start_time = int((now - timedelta(minutes=minutes + 1)).timestamp() * 1000)

        klines = await self.collect_klines(
            symbol, interval, start_time=start_time, end_time=end_time
        )

        if klines:
            # 过滤掉未收盘的 K 线
            current_time = int(now.timestamp() * 1000)
            filtered_klines = [k for k in klines if k.close_time < current_time]
            
            if filtered_klines:
                stored = await self.store_klines(filtered_klines)
                logger.info(f"采集 {symbol} {interval}：获取{len(klines)}条，过滤后{len(filtered_klines)}条，存储{stored}条")
                return stored

        return 0

    async def validate_registered_symbols(self) -> int:
        """
        验证所有注册的标的在币安上是否有效，自动移除无效标的
        
        Returns:
            移除的无效标的数量
        """
        cleaned = 0
        try:
            # 获取币安所有有效交易对
            valid_symbols = await self.binance_client.get_all_symbols()
            if not valid_symbols:
                logger.warning("获取币安有效交易对列表失败，跳过验证")
                return 0

            valid_set = set(valid_symbols)
            active_symbols = registry.get_active_symbols()

            for config in active_symbols:
                if config.symbol not in valid_set:
                    logger.warning(
                        f"标的 {config.symbol} 在币安期货上不存在或已下架，自动取消注册"
                    )
                    await registry.unregister(config.symbol)
                    cleaned += 1

            if cleaned > 0:
                logger.info(f"🧹 自动清理了 {cleaned} 个无效的标的（已在币安下架）")
            return cleaned

        except Exception as e:
            logger.error(f"验证注册标的失败：{e}")
            return 0

    def get_stats(self) -> Dict:
        """获取采集统计信息"""
        return self.stats.copy()
