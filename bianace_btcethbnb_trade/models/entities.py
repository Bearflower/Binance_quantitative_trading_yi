#!/usr/bin/env python3
"""
交易记录数据仓库

提供交易记录相关的数据库操作。

功能：
1. 交易记录的CRUD操作
2. 交易记录查询和统计
3. 交易状态更新
4. 平仓记录管理

版本: v1.0.0
创建时间: 2026-04-27
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any

from models.repository import BaseRepository

logger = logging.getLogger(__name__)


class TradeRepository(BaseRepository):
    """
    交易记录数据仓库

    管理交易记录相关的数据库操作。
    """

    def __init__(self):
        """初始化交易记录仓库"""
        super().__init__(table_name='trades', primary_key='order_id')

    def get_entity_name(self) -> str:
        """获取实体名称"""
        return "交易记录"

    # ==================== 交易记录查询 ====================

    def get_by_order_id(self, order_id: int) -> Optional[Dict[str, Any]]:
        """
        根据订单ID查询交易记录

        Args:
            order_id: 订单ID

        Returns:
            交易记录字典，如果不存在则返回None
        """
        return self.find_by_id(order_id)

    def get_by_symbol(
        self,
        symbol: str,
        limit: int = 50,
        status: str = None
    ) -> List[Dict[str, Any]]:
        """
        查询指定交易对的交易记录

        Args:
            symbol: 交易对
            limit: 返回记录数量
            status: 订单状态（可选）

        Returns:
            交易记录列表
        """
        where_clause = "symbol = %s"
        params = [symbol]

        if status:
            where_clause += " AND status = %s"
            params.append(status)

        query = f"""
            SELECT * FROM {self.table_name}
            WHERE {where_clause}
            ORDER BY create_time DESC
            LIMIT %s
        """
        params.append(limit)

        return self.find_many(query, tuple(params))

    def get_recent_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取最近的交易记录

        Args:
            limit: 返回记录数量

        Returns:
            交易记录列表
        """
        query = f"""
            SELECT * FROM {self.table_name}
            ORDER BY create_time DESC
            LIMIT %s
        """
        return self.find_many(query, (limit,))

    def get_trades_by_time_range(
        self,
        start_time: datetime,
        end_time: datetime,
        symbol: str = None
    ) -> List[Dict[str, Any]]:
        """
        查询指定时间范围内的交易记录

        Args:
            start_time: 开始时间
            end_time: 结束时间
            symbol: 交易对（可选）

        Returns:
            交易记录列表
        """
        where_clause = "create_time >= %s AND create_time <= %s"
        params = [start_time, end_time]

        if symbol:
            where_clause += " AND symbol = %s"
            params.append(symbol)

        query = f"""
            SELECT * FROM {self.table_name}
            WHERE {where_clause}
            ORDER BY create_time DESC
        """

        return self.find_many(query, tuple(params))

    # ==================== 交易记录保存和更新 ====================

    def save_trade(
        self,
        order_data: Dict[str, Any],
        tp_price: Decimal = None,
        sl_price: Decimal = None,
        transaction_id: int = None
    ) -> int:
        """
        保存交易记录

        Args:
            order_data: 订单数据
            tp_price: 止盈价
            sl_price: 止损价
            transaction_id: 关联的资金划转ID

        Returns:
            影响的行数
        """
        data = {
            'order_id': order_data.get('orderId'),
            'symbol': order_data.get('symbol'),
            'side': order_data.get('side'),
            'position_side': order_data.get('positionSide'),
            'type': order_data.get('type'),
            'quantity': str(Decimal(order_data.get('origQty', '0'))),
            'price': str(Decimal(order_data.get('price', '0'))) if order_data.get('price') else None,
            'avg_price': str(Decimal(order_data.get('avgPrice', '0'))) if order_data.get('avgPrice') else None,
            'executed_qty': str(Decimal(order_data.get('executedQty', '0'))),
            'cum_quote': str(Decimal(order_data.get('cumQuote', '0'))) if order_data.get('cumQuote') else None,
            'status': order_data.get('status'),
            'reduce_only': order_data.get('reduceOnly', False),
            'time_in_force': order_data.get('timeInForce'),
            'client_order_id': order_data.get('clientOrderId'),
            'tp_trigger_price': str(tp_price) if tp_price else None,
            'tp_price': str(tp_price) if tp_price else None,
            'sl_trigger_price': str(sl_price) if sl_price else None,
            'sl_price': str(sl_price) if sl_price else None,
            'create_time': order_data.get('updateTime', int(datetime.now().timestamp() * 1000)),
            'update_time': order_data.get('updateTime', int(datetime.now().timestamp() * 1000)),
            'transaction_id': transaction_id
        }

        # 使用UPSERT避免重复插入
        return self.upsert(
            data,
            conflict_columns=['order_id'],
            update_columns=['status', 'avg_price', 'executed_qty', 'update_time']
        )

    def update_trade_status(
        self,
        order_id: int,
        status: str,
        avg_price: Decimal = None,
        executed_qty: Decimal = None
    ) -> int:
        """
        更新交易状态

        Args:
            order_id: 订单ID
            status: 新状态
            avg_price: 平均成交价
            executed_qty: 已成交数量

        Returns:
            影响的行数
        """
        data = {
            'status': status,
            'update_time': int(datetime.now().timestamp() * 1000)
        }

        if avg_price:
            data['avg_price'] = str(avg_price)

        if executed_qty:
            data['executed_qty'] = str(executed_qty)

        return self.update(order_id, data)

    # ==================== 平仓记录管理 ====================

    def save_closed_position(self, close_data: Dict[str, Any]) -> int:
        """
        保存平仓记录

        Args:
            close_data: 平仓数据

        Returns:
            影响的行数
        """
        data = {
            'order_id': close_data.get('order_id'),
            'symbol': close_data.get('symbol'),
            'side': close_data.get('side'),
            'position_side': close_data.get('position_side'),
            'open_price': str(close_data.get('open_price', Decimal('0'))),
            'close_price': str(close_data.get('close_price', Decimal('0'))),
            'quantity': str(close_data.get('quantity', Decimal('0'))),
            'open_time': close_data.get('open_time'),
            'close_time': close_data.get('close_time'),
            'leverage': close_data.get('leverage', 20),
            'gross_pnl': str(close_data.get('gross_pnl', Decimal('0'))),
            'commission': str(close_data.get('commission', Decimal('0'))),
            'net_pnl': str(close_data.get('net_pnl', Decimal('0'))),
            'pnl_rate': str(close_data.get('pnl_rate', Decimal('0'))),
            'close_reason': close_data.get('close_reason'),
            'max_unrealized_profit': str(close_data.get('max_unrealized_profit')) if close_data.get('max_unrealized_profit') else None,
            'min_unrealized_profit': str(close_data.get('min_unrealized_profit')) if close_data.get('min_unrealized_profit') else None,
            'duration_seconds': close_data.get('duration_seconds'),
            'remark': close_data.get('remark')
        }

        return self.insert(data)

    def get_closed_positions(
        self,
        symbol: str = None,
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        查询平仓记录

        Args:
            symbol: 交易对（可选）
            start_time: 开始时间（可选）
            end_time: 结束时间（可选）
            limit: 返回记录数量

        Returns:
            平仓记录列表
        """
        where_clause = "1=1"
        params = []

        if symbol:
            where_clause += " AND symbol = %s"
            params.append(symbol)

        if start_time:
            where_clause += " AND close_time >= %s"
            params.append(int(start_time.timestamp() * 1000))

        if end_time:
            where_clause += " AND close_time <= %s"
            params.append(int(end_time.timestamp() * 1000))

        query = f"""
            SELECT * FROM closed_positions
            WHERE {where_clause}
            ORDER BY close_time DESC
            LIMIT %s
        """
        params.append(limit)

        return self.find_many(query, tuple(params))

    # ==================== 统计查询 ====================

    def get_daily_trade_count(self, date: datetime = None) -> int:
        """
        获取指定日期的交易数量

        Args:
            date: 日期（默认今天）

        Returns:
            交易数量
        """
        if date is None:
            date = datetime.now().date()

        where_clause = "DATE(created_at) = %s"
        return self.count(where_clause, (date,))

    def get_symbol_daily_trade_count(self, symbol: str, date: datetime = None) -> int:
        """
        获取指定交易对在指定日期的交易数量

        Args:
            symbol: 交易对
            date: 日期（默认今天）

        Returns:
            交易数量
        """
        if date is None:
            date = datetime.now().date()

        where_clause = "symbol = %s AND DATE(created_at) = %s"
        return self.count(where_clause, (symbol, date))

    def get_trade_statistics(self, days: int = 30) -> Dict[str, Any]:
        """
        获取交易统计数据

        Args:
            days: 统计天数

        Returns:
            统计数据字典
        """
        query = f"""
            SELECT
                COUNT(*) as total_trades,
                COUNT(CASE WHEN status = 'FILLED' THEN 1 END) as filled_trades
            FROM {self.table_name}
            WHERE created_at >= NOW() - INTERVAL '%s days'
        """

        result = self.find_one(query % days)
        return {
            'total_trades': result['total_trades'] or 0,
            'filled_trades': result['filled_trades'] or 0,
            'period_days': days
        }


class FrequencyRepository(BaseRepository):
    """
    频率控制数据仓库

    管理交易频率控制相关的数据库操作。
    """

    def __init__(self):
        """初始化频率控制仓库"""
        super().__init__(table_name='trade_records', primary_key='id')

    def get_entity_name(self) -> str:
        """获取实体名称"""
        return "频率控制记录"

    # ==================== 频率控制查询 ====================

    def get_daily_total_trades(self, date: datetime = None) -> int:
        """
        获取指定日期的总交易数

        Args:
            date: 日期（默认今天）

        Returns:
            交易数量
        """
        if date is None:
            date = datetime.now().date()

        where_clause = "DATE(open_time) = %s AND status = %s"
        return self.count(where_clause, (date, 'CLOSED'))

    def get_symbol_daily_trades(self, symbol: str, date: datetime = None) -> int:
        """
        获取指定交易对在指定日期的交易数

        Args:
            symbol: 交易对
            date: 日期（默认今天）

        Returns:
            交易数量
        """
        if date is None:
            date = datetime.now().date()

        where_clause = "symbol = %s AND DATE(open_time) = %s AND status = %s"
        return self.count(where_clause, (symbol, date, 'CLOSED'))

    def get_last_trade_time(self, symbol: str) -> Optional[datetime]:
        """
        获取指定交易对最后一次交易时间

        Args:
            symbol: 交易对

        Returns:
            最后交易时间，如果不存在则返回None
        """
        query = f"""
            SELECT open_time
            FROM {self.table_name}
            WHERE symbol = %s AND status = 'CLOSED'
            ORDER BY open_time DESC
            LIMIT 1
        """

        result = self.find_one(query, (symbol,))
        return result['open_time'] if result else None

    def get_consecutive_losses(self, limit: int = 10) -> int:
        """
        获取连续亏损次数

        Args:
            limit: 查询记录数量

        Returns:
            连续亏损次数
        """
        query = f"""
            SELECT pnl
            FROM {self.table_name}
            WHERE status = 'CLOSED'
            ORDER BY open_time DESC
            LIMIT %s
        """

        results = self.find_many(query, (limit,))

        if not results:
            return 0

        # 统计连续亏损次数
        consecutive = 0
        for record in results:
            pnl = Decimal(str(record['pnl'])) if record['pnl'] else Decimal('0')
            if pnl < 0:
                consecutive += 1
            else:
                break  # 遇到盈利，中断连续亏损

        return consecutive

    def get_daily_pnl(self, date: datetime = None) -> Decimal:
        """
        获取指定日期的盈亏

        Args:
            date: 日期（默认今天）

        Returns:
            盈亏金额
        """
        if date is None:
            date = datetime.now().date()

        where_clause = "DATE(open_time) = %s AND status = %s"
        return self.sum('pnl', where_clause, (date, 'CLOSED'))

    def record_trade(
        self,
        symbol: str,
        direction: str,
        open_time: datetime,
        pnl: Decimal = Decimal('0')
    ) -> int:
        """
        记录交易

        Args:
            symbol: 交易对
            direction: 方向
            open_time: 开仓时间
            pnl: 盈亏金额

        Returns:
            影响的行数
        """
        data = {
            'symbol': symbol,
            'direction': direction,
            'open_time': open_time,
            'pnl': pnl,
            'status': 'CLOSED'
        }

        return self.insert(data)


class PerformanceRepository(BaseRepository):
    """
    绩效统计数据仓库

    管理交易绩效统计相关的数据库操作。
    """

    def __init__(self):
        """初始化绩效统计仓库"""
        super().__init__(table_name='trade_statistics', primary_key='id')

    def get_entity_name(self) -> str:
        """获取实体名称"""
        return "绩效统计记录"

    # ==================== 绩效统计查询 ====================

    def get_weekly_statistics(self, weeks: int = 4) -> List[Dict[str, Any]]:
        """
        获取最近N周的统计数据

        Args:
            weeks: 周数

        Returns:
            统计数据列表
        """
        query = f"""
            SELECT * FROM {self.table_name}
            WHERE period_type = %s
            ORDER BY period_end DESC
            LIMIT %s
        """

        return self.find_many(query, ('WEEKLY', weeks))

    def get_monthly_statistics(self, months: int = 6) -> List[Dict[str, Any]]:
        """
        获取最近N月的统计数据

        Args:
            months: 月数

        Returns:
            统计数据列表
        """
        query = f"""
            SELECT * FROM {self.table_name}
            WHERE period_type = %s
            ORDER BY period_end DESC
            LIMIT %s
        """

        return self.find_many(query, ('MONTHLY', months))

    def save_statistics(self, stats_data: Dict[str, Any]) -> int:
        """
        保存统计数据

        Args:
            stats_data: 统计数据

        Returns:
            影响的行数
        """
        data = {
            'period_type': stats_data.get('period_type'),
            'period_start': stats_data.get('period_start'),
            'period_end': stats_data.get('period_end'),
            'symbol': stats_data.get('symbol', 'ALL'),
            'total_trades': stats_data.get('total_trades', 0),
            'winning_trades': stats_data.get('winning_trades', 0),
            'losing_trades': stats_data.get('losing_trades', 0),
            'total_net_pnl': str(stats_data.get('total_net_pnl', Decimal('0'))),
            'total_commission': str(stats_data.get('total_commission', Decimal('0'))),
            'avg_pnl_rate': str(stats_data.get('avg_pnl_rate', Decimal('0'))),
            'max_pnl_rate': str(stats_data.get('max_pnl_rate', Decimal('0'))),
            'min_pnl_rate': str(stats_data.get('min_pnl_rate', Decimal('0'))),
            'win_rate': str(stats_data.get('win_rate', Decimal('0'))),
            'profit_loss_ratio': str(stats_data.get('profit_loss_ratio', Decimal('0'))),
            'max_consecutive_wins': stats_data.get('max_consecutive_wins', 0),
            'max_consecutive_losses': stats_data.get('max_consecutive_losses', 0),
            'updated_at': datetime.now()
        }

        # 使用UPSERT避免重复
        return self.upsert(
            data,
            conflict_columns=['period_type', 'period_start', 'period_end', 'symbol'],
            update_columns=[
                'total_trades', 'winning_trades', 'losing_trades',
                'total_net_pnl', 'total_commission',
                'avg_pnl_rate', 'max_pnl_rate', 'min_pnl_rate',
                'win_rate', 'profit_loss_ratio',
                'max_consecutive_wins', 'max_consecutive_losses'
            ]
        )

    def get_performance_summary(self, days: int = 30) -> Dict[str, Any]:
        """
        获取绩效摘要

        Args:
            days: 统计天数

        Returns:
            绩效摘要字典
        """
        # 查询平仓记录统计
        query = """
            SELECT
                COUNT(*) as total_trades,
                COUNT(CASE WHEN net_pnl > 0 THEN 1 END) as winning_trades,
                COUNT(CASE WHEN net_pnl < 0 THEN 1 END) as losing_trades,
                COALESCE(SUM(net_pnl), 0) as total_pnl,
                COALESCE(AVG(pnl_rate), 0) as avg_pnl_rate,
                COALESCE(MAX(pnl_rate), 0) as max_pnl_rate,
                COALESCE(MIN(pnl_rate), 0) as min_pnl_rate
            FROM closed_positions
            WHERE close_time >= EXTRACT(EPOCH FROM (NOW() - INTERVAL '%s days')) * 1000
        """

        result = self.find_one(query % days)

        if not result:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'total_pnl': Decimal('0'),
                'win_rate': Decimal('0'),
                'avg_pnl_rate': Decimal('0'),
                'period_days': days
            }

        total_trades = result['total_trades'] or 0
        winning_trades = result['winning_trades'] or 0

        # 计算胜率
        win_rate = Decimal(str(winning_trades / total_trades)) if total_trades > 0 else Decimal('0')

        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': result['losing_trades'] or 0,
            'total_pnl': Decimal(str(result['total_pnl'])),
            'win_rate': win_rate,
            'avg_pnl_rate': Decimal(str(result['avg_pnl_rate'])),
            'max_pnl_rate': Decimal(str(result['max_pnl_rate'])),
            'min_pnl_rate': Decimal(str(result['min_pnl_rate'])),
            'period_days': days
        }


class MonitorRepository(BaseRepository):
    """
    监控数据仓库

    管理监控指标和告警历史相关的数据库操作。
    """

    def __init__(self):
        """初始化监控数据仓库"""
        super().__init__(table_name='monitor_metrics', primary_key='id')

    def get_entity_name(self) -> str:
        """获取实体名称"""
        return "监控指标记录"

    # ==================== 指标存储 ====================

    def save_metrics(self, metrics_data: Dict[str, Any]) -> int:
        """
        保存监控指标

        Args:
            metrics_data: 指标数据

        Returns:
            影响的行数
        """
        data = {
            'metric_type': metrics_data.get('metric_type'),
            'metric_name': metrics_data.get('metric_name'),
            'metric_value': str(metrics_data.get('metric_value', 0)),
            'unit': metrics_data.get('unit'),
            'tags': metrics_data.get('tags'),
            'timestamp': metrics_data.get('timestamp', int(datetime.now().timestamp() * 1000)),
            'created_at': datetime.now()
        }

        return self.insert(data)

    def save_system_metrics(self, system_metrics: Dict[str, Any]) -> int:
        """
        保存系统指标

        Args:
            system_metrics: 系统指标数据

        Returns:
            影响的行数
        """
        timestamp = int(datetime.now().timestamp() * 1000)
        records = []

        # CPU指标
        if 'cpu' in system_metrics:
            cpu = system_metrics['cpu']
            records.append({
                'metric_type': 'system',
                'metric_name': 'cpu_usage',
                'metric_value': cpu.get('percent', 0),
                'unit': '%',
                'timestamp': timestamp
            })

        # 内存指标
        if 'memory' in system_metrics:
            memory = system_metrics['memory']
            records.append({
                'metric_type': 'system',
                'metric_name': 'memory_usage',
                'metric_value': memory.get('percent', 0),
                'unit': '%',
                'timestamp': timestamp
            })

        # 磁盘指标
        if 'disk' in system_metrics:
            disk = system_metrics['disk']
            records.append({
                'metric_type': 'system',
                'metric_name': 'disk_usage',
                'metric_value': disk.get('percent', 0),
                'unit': '%',
                'timestamp': timestamp
            })

        # 批量插入
        count = 0
        for record in records:
            count += self.save_metrics(record)

        return count

    def save_app_metrics(self, app_metrics: Dict[str, Any]) -> int:
        """
        保存应用指标

        Args:
            app_metrics: 应用指标数据

        Returns:
            影响的行数
        """
        timestamp = int(datetime.now().timestamp() * 1000)
        records = []

        # API指标
        if 'api' in app_metrics:
            api = app_metrics['api']
            records.extend([
                {
                    'metric_type': 'application',
                    'metric_name': 'api_success_rate',
                    'metric_value': api.get('success_rate', 100),
                    'unit': '%',
                    'timestamp': timestamp
                },
                {
                    'metric_type': 'application',
                    'metric_name': 'api_response_time',
                    'metric_value': api.get('avg_response_time', 0),
                    'unit': 's',
                    'timestamp': timestamp
                },
                {
                    'metric_type': 'application',
                    'metric_name': 'api_error_rate',
                    'metric_value': api.get('error_rate', 0),
                    'unit': '%',
                    'timestamp': timestamp
                }
            ])

        # 批量插入
        count = 0
        for record in records:
            count += self.save_metrics(record)

        return count

    def save_business_metrics(self, business_metrics: Dict[str, Any]) -> int:
        """
        保存业务指标

        Args:
            business_metrics: 业务指标数据

        Returns:
            影响的行数
        """
        timestamp = int(datetime.now().timestamp() * 1000)
        records = []

        # 交易指标
        if 'trading' in business_metrics:
            trading = business_metrics['trading']
            records.extend([
                {
                    'metric_type': 'business',
                    'metric_name': 'trade_success_rate',
                    'metric_value': trading.get('trade_success_rate', 100),
                    'unit': '%',
                    'timestamp': timestamp
                },
                {
                    'metric_type': 'business',
                    'metric_name': 'win_rate',
                    'metric_value': trading.get('win_rate', 0),
                    'unit': '%',
                    'timestamp': timestamp
                },
                {
                    'metric_type': 'business',
                    'metric_name': 'total_pnl',
                    'metric_value': trading.get('total_pnl', 0),
                    'unit': 'U',
                    'timestamp': timestamp
                }
            ])

        # 账户指标
        if 'account' in business_metrics:
            account = business_metrics['account']
            records.append({
                'metric_type': 'business',
                'metric_name': 'capital_usage',
                'metric_value': account.get('capital_usage', 0),
                'unit': '%',
                'timestamp': timestamp
            })

        # 批量插入
        count = 0
        for record in records:
            count += self.save_metrics(record)

        return count

    # ==================== 指标查询 ====================

    def get_metrics_by_type(
        self,
        metric_type: str,
        metric_name: str = None,
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        查询指定类型的指标

        Args:
            metric_type: 指标类型（system/application/business）
            metric_name: 指标名称（可选）
            start_time: 开始时间（可选）
            end_time: 结束时间（可选）
            limit: 返回记录数

        Returns:
            指标记录列表
        """
        where_clause = "metric_type = %s"
        params = [metric_type]

        if metric_name:
            where_clause += " AND metric_name = %s"
            params.append(metric_name)

        if start_time:
            where_clause += " AND timestamp >= %s"
            params.append(int(start_time.timestamp() * 1000))

        if end_time:
            where_clause += " AND timestamp <= %s"
            params.append(int(end_time.timestamp() * 1000))

        query = f"""
            SELECT * FROM {self.table_name}
            WHERE {where_clause}
            ORDER BY timestamp DESC
            LIMIT %s
        """
        params.append(limit)

        return self.find_many(query, tuple(params))

    def get_latest_metrics(self, metric_type: str = None) -> Dict[str, Any]:
        """
        获取最新的指标数据

        Args:
            metric_type: 指标类型（可选）

        Returns:
            最新指标数据
        """
        where_clause = "1=1"
        params = []

        if metric_type:
            where_clause = "metric_type = %s"
            params = [metric_type]

        query = f"""
            SELECT DISTINCT ON (metric_name) *
            FROM {self.table_name}
            WHERE {where_clause}
            ORDER BY metric_name, timestamp DESC
        """

        results = self.find_many(query, tuple(params))

        # 转换为字典格式
        metrics = {}
        for result in results:
            metric_name = result['metric_name']
            metrics[metric_name] = {
                'value': float(result['metric_value']),
                'unit': result['unit'],
                'timestamp': result['timestamp']
            }

        return metrics

    def get_metrics_aggregation(
        self,
        metric_name: str,
        aggregation_type: str = 'avg',
        hours: int = 24
    ) -> Dict[str, Any]:
        """
        获取指标聚合数据

        Args:
            metric_name: 指标名称
            aggregation_type: 聚合类型（avg/max/min/sum）
            hours: 统计时长（小时）

        Returns:
            聚合数据
        """
        start_time = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)

        aggregation_map = {
            'avg': 'AVG',
            'max': 'MAX',
            'min': 'MIN',
            'sum': 'SUM'
        }

        agg_func = aggregation_map.get(aggregation_type, 'AVG')

        query = f"""
            SELECT
                metric_name,
                {agg_func}(metric_value) as aggregated_value,
                COUNT(*) as count,
                MIN(timestamp) as start_time,
                MAX(timestamp) as end_time
            FROM {self.table_name}
            WHERE metric_name = %s AND timestamp >= %s
            GROUP BY metric_name
        """

        result = self.find_one(query, (metric_name, start_time))

        if result:
            return {
                'metric_name': result['metric_name'],
                'aggregated_value': float(result['aggregated_value']),
                'count': result['count'],
                'start_time': result['start_time'],
                'end_time': result['end_time'],
                'aggregation_type': aggregation_type
            }

        return {}

    # ==================== 数据清理 ====================

    def cleanup_old_metrics(self, days: int = 30) -> int:
        """
        清理旧的指标数据

        Args:
            days: 保留天数

        Returns:
            删除的记录数
        """
        cutoff_time = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

        query = f"""
            DELETE FROM {self.table_name}
            WHERE timestamp < %s
        """

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, (cutoff_time,))
                    deleted_count = cursor.rowcount
                    conn.commit()

                    self.logger.info(f"清理了 {deleted_count} 条旧指标数据")
                    return deleted_count
        except Exception as e:
            self.logger.error(f"清理旧指标数据失败: {e}")
            return 0


class AlertHistoryRepository(BaseRepository):
    """
    告警历史数据仓库

    管理告警历史记录相关的数据库操作。
    """

    def __init__(self):
        """初始化告警历史仓库"""
        super().__init__(table_name='alert_history', primary_key='id')

    def get_entity_name(self) -> str:
        """获取实体名称"""
        return "告警历史记录"

    # ==================== 告警记录存储 ====================

    def save_alert(self, alert_data: Dict[str, Any]) -> int:
        """
        保存告警记录

        Args:
            alert_data: 告警数据

        Returns:
            影响的行数
        """
        data = {
            'rule_name': alert_data.get('rule_name'),
            'metric': alert_data.get('metric'),
            'metric_value': str(alert_data.get('value', 0)),
            'threshold': str(alert_data.get('threshold', 0)),
            'level': alert_data.get('level'),
            'message': alert_data.get('message'),
            'triggered_at': alert_data.get('triggered_at'),
            'acknowledged': False,
            'created_at': datetime.now()
        }

        return self.insert(data)

    def acknowledge_alert(self, alert_id: int, acknowledged_by: str = None) -> int:
        """
        确认告警

        Args:
            alert_id: 告警ID
            acknowledged_by: 确认人

        Returns:
            影响的行数
        """
        data = {
            'acknowledged': True,
            'acknowledged_at': datetime.now(),
            'acknowledged_by': acknowledged_by
        }

        return self.update(alert_id, data)

    # ==================== 告警查询 ====================

    def get_alerts_by_level(
        self,
        level: str,
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        查询指定级别的告警

        Args:
            level: 告警级别
            start_time: 开始时间（可选）
            end_time: 结束时间（可选）
            limit: 返回记录数

        Returns:
            告警记录列表
        """
        where_clause = "level = %s"
        params = [level]

        if start_time:
            where_clause += " AND triggered_at >= %s"
            params.append(start_time.isoformat())

        if end_time:
            where_clause += " AND triggered_at <= %s"
            params.append(end_time.isoformat())

        query = f"""
            SELECT * FROM {self.table_name}
            WHERE {where_clause}
            ORDER BY triggered_at DESC
            LIMIT %s
        """
        params.append(limit)

        return self.find_many(query, tuple(params))

    def get_unacknowledged_alerts(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取未确认的告警

        Args:
            limit: 返回记录数

        Returns:
            未确认告警列表
        """
        query = f"""
            SELECT * FROM {self.table_name}
            WHERE acknowledged = FALSE
            ORDER BY triggered_at DESC
            LIMIT %s
        """

        return self.find_many(query, (limit,))

    def get_alert_statistics(self, days: int = 7) -> Dict[str, Any]:
        """
        获取告警统计

        Args:
            days: 统计天数

        Returns:
            告警统计数据
        """
        start_time = (datetime.now() - timedelta(days=days)).isoformat()

        query = f"""
            SELECT
                level,
                COUNT(*) as count,
                COUNT(CASE WHEN acknowledged THEN 1 END) as acknowledged_count,
                COUNT(CASE WHEN NOT acknowledged THEN 1 END) as unacknowledged_count
            FROM {self.table_name}
            WHERE triggered_at >= %s
            GROUP BY level
        """

        results = self.find_many(query, (start_time,))

        stats = {
            'total': 0,
            'by_level': {}
        }

        for result in results:
            level = result['level']
            stats['total'] += result['count']
            stats['by_level'][level] = {
                'count': result['count'],
                'acknowledged': result['acknowledged_count'],
                'unacknowledged': result['unacknowledged_count']
            }

        return stats
