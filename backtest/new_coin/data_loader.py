"""
数据加载器
从CSV文件加载K线数据、从JSON文件加载交易对列表、从YAML文件加载配置
"""
from typing import Dict, List, Any
import json
import csv
import os
from datetime import datetime, timezone
import structlog


logger = structlog.get_logger()


class DataLoader:
    """数据加载器
    
    职责：
    - 从CSV文件加载K线数据
    - 从JSON文件加载交易对列表
    - 从YAML文件加载策略配置
    - 数据预处理和验证
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化数据加载器
        
        Args:
            config: 配置字典
        """
        self.config = config
        
        # 数据路径
        backtest_config = config.get('backtest', {})
        self.data_dir = backtest_config.get('data_dir', 'backtest/new_coin/data')
        self.klines_dir = os.path.join(self.data_dir, 'klines')
        self.coin_list_path = os.path.join(self.data_dir, 'coin_list.json')
        
        logger.info(
            "数据加载器初始化完成",
            data_dir=self.data_dir,
            klines_dir=self.klines_dir
        )
    
    def load_klines(self, symbol: str) -> List[Dict[str, Any]]:
        """
        加载K线数据
        
        Args:
            symbol: 交易对
            
        Returns:
            K线数据列表
        """
        try:
            # 构建文件路径
            interval = self.config.get('kline', {}).get('interval', '1h')
            filename = f"{symbol}_{interval}.csv"
            filepath = os.path.join(self.klines_dir, filename)
            
            # 检查文件是否存在
            if not os.path.exists(filepath):
                logger.warning(f"K线数据文件不存在: {filepath}")
                return []
            
            # 读取CSV文件，兼容多种格式：
            # 1. 原始币安格式：12列，毫秒时间戳，无表头
            # 2. 旧格式变种：8列，毫秒时间戳，有表头
            #    （open_time,open,high,low,close,volume,quote_asset_volume,close_time）
            # 3. 新格式：7列，时间戳字符串，有表头
            #    （open_time,open_price,high_price,low_price,close_price,volume,quote_volume）
            klines = []
            with open(filepath, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                rows = list(reader)

            # 空文件直接返回
            if not rows:
                logger.warning(f"K线数据文件为空: {filepath}")
                return []

            # 判断第一行是否为表头（第一列不是纯数字则视为表头）
            first_row = rows[0]
            has_header = bool(first_row) and not first_row[0].lstrip('-').isdigit()

            if has_header:
                # 有表头：建立列名到索引的映射，按列名解析以兼容不同列顺序
                header = [name.strip() for name in first_row]
                col_map = {name: idx for idx, name in enumerate(header)}
                data_rows = rows[1:]

                # 检测数据格式：根据第一个数据行的 open_time 列内容判断
                # 新格式：时间字符串（包含 "-" 日期分隔符）
                # 旧格式变种：毫秒时间戳（纯数字）
                is_new_format = False
                if data_rows and 'open_time' in col_map:
                    first_data = data_rows[0]
                    open_time_val = first_data[col_map['open_time']]
                    is_new_format = '-' in open_time_val

                for row in data_rows:
                    # 跳过空行
                    if not row:
                        continue
                    try:
                        if is_new_format:
                            # 新格式：open_time 为时间字符串，需转换为毫秒时间戳（UTC时区）
                            open_time_str = row[col_map['open_time']]
                            open_time_ms = int(
                                datetime.strptime(open_time_str, '%Y-%m-%d %H:%M:%S')
                                .replace(tzinfo=timezone.utc)
                                .timestamp() * 1000
                            )
                            kline = {
                                'open_time': open_time_ms,                   # 开盘时间（毫秒时间戳）
                                'open': float(row[col_map['open_price']]),   # 开盘价
                                'high': float(row[col_map['high_price']]),   # 最高价
                                'low': float(row[col_map['low_price']]),     # 最低价
                                'close': float(row[col_map['close_price']]), # 收盘价
                                'volume': float(row[col_map['volume']]),     # 成交量
                                'close_time': open_time_ms + 3599999,        # 收盘时间（开盘时间+1小时-1毫秒）
                                'quote_asset_volume': float(row[col_map['quote_volume']]) if 'quote_volume' in col_map else 0.0,  # 成交额
                                'trade_count': 0,                            # 成交笔数（新格式无此字段）
                                'taker_buy_volume': 0.0,                     # 主动买入成交量（新格式无此字段）
                                'taker_buy_quote_volume': 0.0                # 主动买入成交额（新格式无此字段）
                            }
                        else:
                            # 旧格式变种：open_time 为毫秒时间戳，按列名读取
                            open_time_ms = int(row[col_map['open_time']])
                            kline = {
                                'open_time': open_time_ms,                                          # 开盘时间（毫秒时间戳）
                                'open': float(row[col_map['open']]),                                # 开盘价
                                'high': float(row[col_map['high']]),                                # 最高价
                                'low': float(row[col_map['low']]),                                  # 最低价
                                'close': float(row[col_map['close']]),                              # 收盘价
                                'volume': float(row[col_map['volume']]),                            # 成交量
                                'close_time': int(row[col_map['close_time']]) if 'close_time' in col_map else open_time_ms + 3599999,  # 收盘时间
                                'quote_asset_volume': float(row[col_map['quote_asset_volume']]) if 'quote_asset_volume' in col_map else 0.0,  # 成交额
                                'trade_count': int(row[col_map['trade_count']]) if 'trade_count' in col_map else 0,      # 成交笔数
                                'taker_buy_volume': float(row[col_map['taker_buy_volume']]) if 'taker_buy_volume' in col_map else 0.0,  # 主动买入成交量
                                'taker_buy_quote_volume': float(row[col_map['taker_buy_quote_volume']]) if 'taker_buy_quote_volume' in col_map else 0.0  # 主动买入成交额
                            }
                        klines.append(kline)
                    except (ValueError, IndexError, KeyError) as e:
                        logger.warning(f"数据行解析失败: {row}, 错误: {e}")
                        continue
            else:
                # 无表头：原始币安格式（12列，按固定字段顺序解析）
                # 字段顺序: open_time, open, high, low, close, volume,
                #          close_time, quote_asset_volume, trade_count,
                #          taker_buy_volume, taker_buy_quote_volume, ignore
                for row in rows:
                    # 跳过空行和列数不足的行
                    if not row or len(row) < 11:
                        logger.warning(f"跳过无效数据行: {row}")
                        continue
                    try:
                        kline = {
                            'open_time': int(row[0]),            # 开盘时间（毫秒时间戳）
                            'open': float(row[1]),               # 开盘价
                            'high': float(row[2]),               # 最高价
                            'low': float(row[3]),                # 最低价
                            'close': float(row[4]),              # 收盘价
                            'volume': float(row[5]),             # 成交量
                            'close_time': int(row[6]),           # 收盘时间（毫秒时间戳）
                            'quote_asset_volume': float(row[7]), # 成交额
                            'trade_count': int(row[8]),          # 成交笔数
                            'taker_buy_volume': float(row[9]),   # 主动买入成交量
                            'taker_buy_quote_volume': float(row[10])  # 主动买入成交额
                        }
                        klines.append(kline)
                    except (ValueError, IndexError) as e:
                        logger.warning(f"数据行解析失败: {row}, 错误: {e}")
                        continue
            
            logger.info(
                f"加载K线数据: {symbol}",
                count=len(klines),
                start_time=klines[0]['open_time'] if klines else 0,
                end_time=klines[-1]['open_time'] if klines else 0
            )
            
            return klines
            
        except Exception as e:
            logger.error(f"加载K线数据失败: {symbol}, 错误: {e}")
            return []
    
    def load_coin_list(self) -> List[Dict[str, Any]]:
        """
        加载交易对列表
        
        Returns:
            交易对信息列表
        """
        try:
            # 检查文件是否存在
            if not os.path.exists(self.coin_list_path):
                logger.warning(f"交易对列表文件不存在: {self.coin_list_path}")
                return []
            
            # 读取JSON文件
            with open(self.coin_list_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 处理不同的JSON格式
            if isinstance(data, dict):
                # 如果是字典格式，提取contracts字段
                coin_list = data.get('contracts', [])
            elif isinstance(data, list):
                # 如果是列表格式，直接使用
                coin_list = data
            else:
                logger.error(f"交易对列表格式错误: {type(data)}")
                return []
            
            logger.info(f"加载交易对列表: {len(coin_list)} 个币种")
            
            return coin_list
            
        except Exception as e:
            logger.error(f"加载交易对列表失败: {e}")
            return []
    
    def validate_klines(self, klines: List[Dict[str, Any]]) -> bool:
        """
        验证K线数据
        
        Args:
            klines: K线数据列表
            
        Returns:
            是否有效
        """
        if not klines:
            return False
        
        # 检查必要字段
        required_fields = ['open_time', 'open', 'high', 'low', 'close', 'volume']
        
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
            if klines[i]['open_time'] <= klines[i-1]['open_time']:
                logger.error("K线数据时间顺序错误")
                return False
        
        return True
    
    def preprocess_klines(self, klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        预处理K线数据
        
        Args:
            klines: K线数据列表
            
        Returns:
            处理后的K线数据列表
        """
        # 按时间排序
        klines = sorted(klines, key=lambda x: x['open_time'])
        
        # 去重
        seen_times = set()
        unique_klines = []
        for kline in klines:
            if kline['open_time'] not in seen_times:
                seen_times.add(kline['open_time'])
                unique_klines.append(kline)
        
        return unique_klines
