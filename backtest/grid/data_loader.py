"""
网格交易策略回测数据加载器
从CSV文件加载K线数据,支持多时间框架
"""
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import pandas as pd
import os
import structlog


logger = structlog.get_logger()


class DataLoader:
    """
    数据加载器

    职责:
    - 从CSV文件加载K线数据
    - 支持多时间框架数据加载
    - 数据预处理和验证
    - 时间范围过滤
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化数据加载器

        Args:
            config: 配置字典

        Raises:
            ValueError: 配置验证失败
        """
        if not isinstance(config, dict):
            raise ValueError(f"配置必须是字典类型,实际为 {type(config).__name__}")

        self.config = config

        # 回测配置
        backtest_config = config.get('backtest', {})
        self.data_dir = backtest_config.get('data_dir')
        if not self.data_dir:
            raise ValueError("配置缺失: backtest.data_dir")

        self.start_date = backtest_config.get('start_date')
        self.end_date = backtest_config.get('end_date')

        if not self.start_date or not self.end_date:
            raise ValueError("配置缺失: backtest.start_date 或 backtest.end_date")

        # K线配置
        kline_config = config.get('kline', {})
        self.interval = kline_config.get('interval', '1h')
        self.timeframes = kline_config.get('timeframes', ['1h'])
        self.limit = kline_config.get('limit', 500)

        # 交易对
        self.symbol = config.get('symbol')
        if not self.symbol:
            raise ValueError("配置缺失: symbol")

        logger.info(
            "数据加载器初始化完成",
            data_dir=self.data_dir,
            symbol=self.symbol,
            interval=self.interval,
            timeframes=self.timeframes,
            start_date=self.start_date,
            end_date=self.end_date
        )

    def load_klines(self, interval: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        加载K线数据

        Args:
            interval: K线周期(可选,默认使用主周期)

        Returns:
            K线数据列表

        Raises:
            ValueError: 数据加载失败
        """
        interval = interval or self.interval

        try:
            # 构建文件路径
            filename = f"{self.symbol.lower()}_{interval}.csv"
            filepath = os.path.join(self.data_dir, filename)

            # 检查文件是否存在
            if not os.path.exists(filepath):
                logger.warning(f"K线数据文件不存在: {filepath}")
                return []

            # 读取CSV文件
            df = pd.read_csv(filepath)

            # 验证必需的列
            required_columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            for col in required_columns:
                if col not in df.columns:
                    raise ValueError(f"K线数据缺少必需的列: {col}")

            # 转换时间戳
            df['timestamp'] = pd.to_datetime(df['timestamp'])

            # 按时间范围过滤
            start_datetime = pd.to_datetime(self.start_date)
            end_datetime = pd.to_datetime(self.end_date) + timedelta(days=1)  # 包含结束日期

            df = df[(df['timestamp'] >= start_datetime) & (df['timestamp'] <= end_datetime)]

            # 按时间排序
            df = df.sort_values('timestamp')

            # 转换为字典列表
            klines = []
            for _, row in df.iterrows():
                kline = {
                    'timestamp': row['timestamp'].isoformat(),
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': float(row['volume'])
                }
                klines.append(kline)

            logger.info(
                f"加载K线数据: {self.symbol} {interval}",
                count=len(klines),
                start_time=klines[0]['timestamp'] if klines else None,
                end_time=klines[-1]['timestamp'] if klines else None
            )

            return klines

        except Exception as e:
            logger.error(
                f"加载K线数据失败: {self.symbol} {interval}",
                error=str(e),
                exc_info=True
            )
            raise

    def load_multi_timeframe_data(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        加载多时间框架K线数据

        Returns:
            多时间框架数据字典 {'15m': [...], '1h': [...], '4h': [...]}

        Raises:
            ValueError: 数据加载失败
        """
        tf_data = {}

        for interval in self.timeframes:
            klines = self.load_klines(interval)
            if not klines:
                raise ValueError(f"无法加载 {interval} 时间框架数据")

            tf_data[interval] = klines

        logger.info(
            "多时间框架数据加载完成",
            symbol=self.symbol,
            timeframes=list(tf_data.keys()),
            counts={tf: len(data) for tf, data in tf_data.items()}
        )

        return tf_data

    def validate_klines(self, klines: List[Dict[str, Any]]) -> bool:
        """
        验证K线数据

        Args:
            klines: K线数据列表

        Returns:
            是否有效
        """
        if not klines:
            logger.error("K线数据为空")
            return False

        # 检查必要字段
        required_fields = ['timestamp', 'open', 'high', 'low', 'close', 'volume']

        for kline in klines:
            for field in required_fields:
                if field not in kline:
                    logger.error(f"K线数据缺少字段: {field}")
                    return False

                # 检查数值有效性
                if field in ['open', 'high', 'low', 'close', 'volume']:
                    if kline[field] <= 0:
                        logger.error(f"K线数据字段值无效: {field}={kline[field]}")
                        return False

        # 检查时间顺序
        for i in range(1, len(klines)):
            if klines[i]['timestamp'] <= klines[i-1]['timestamp']:
                logger.error("K线数据时间顺序错误")
                return False

        logger.debug("K线数据验证通过", count=len(klines))
        return True

    def get_klines_summary(self, klines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        获取K线数据摘要

        Args:
            klines: K线数据列表

        Returns:
            摘要信息字典
        """
        if not klines:
            return {
                'count': 0,
                'start_time': None,
                'end_time': None,
                'price_range': None
            }

        prices = [kline['close'] for kline in klines]

        return {
            'count': len(klines),
            'start_time': klines[0]['timestamp'],
            'end_time': klines[-1]['timestamp'],
            'price_range': {
                'min': min(prices),
                'max': max(prices),
                'mean': sum(prices) / len(prices)
            }
        }
