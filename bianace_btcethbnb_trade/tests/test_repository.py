#!/usr/bin/env python3
"""
数据仓库单元测试

测试数据仓库基类和具体实现的功能。

版本: v1.0.0
创建时间: 2026-04-27
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch, MagicMock

from models.repository import BaseRepository
from models.entities import TradeRepository, FrequencyRepository, PerformanceRepository


class TestBaseRepository(unittest.TestCase):
    """测试数据仓库基类"""

    def setUp(self):
        """测试前准备"""
        # 创建一个具体的测试仓库
        self.repo = TradeRepository()

    @patch('models.repository.get_db_connection')
    def test_find_one(self, mock_get_db_connection):
        """测试查询单条记录"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock查询结果
        mock_cursor.fetchone.return_value = {
            'order_id': 123456,
            'symbol': 'BTCUSDT',
            'status': 'FILLED'
        }

        # 执行查询
        result = self.repo.find_one(
            "SELECT * FROM trades WHERE order_id = %s",
            (123456,)
        )

        # 验证结果
        self.assertIsNotNone(result)
        self.assertEqual(result['order_id'], 123456)
        self.assertEqual(result['symbol'], 'BTCUSDT')

    @patch('models.repository.get_db_connection')
    def test_find_many(self, mock_get_db_connection):
        """测试查询多条记录"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock查询结果
        mock_cursor.fetchall.return_value = [
            {'order_id': 1, 'symbol': 'BTCUSDT'},
            {'order_id': 2, 'symbol': 'ETHUSDT'}
        ]

        # 执行查询
        results = self.repo.find_many(
            "SELECT * FROM trades WHERE symbol = %s",
            ('BTCUSDT',)
        )

        # 验证结果
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['order_id'], 1)
        self.assertEqual(results[1]['order_id'], 2)

    @patch('models.repository.get_db_connection')
    def test_insert(self, mock_get_db_connection):
        """测试插入记录"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.rowcount = 1

        # 执行插入
        data = {
            'order_id': 123456,
            'symbol': 'BTCUSDT',
            'status': 'NEW'
        }
        result = self.repo.insert(data)

        # 验证结果
        self.assertEqual(result, 1)

    @patch('models.repository.get_db_connection')
    def test_update(self, mock_get_db_connection):
        """测试更新记录"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.rowcount = 1

        # 执行更新
        data = {'status': 'FILLED'}
        result = self.repo.update(123456, data)

        # 验证结果
        self.assertEqual(result, 1)

    @patch('models.repository.get_db_connection')
    def test_count(self, mock_get_db_connection):
        """测试统计记录数量"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock查询结果
        mock_cursor.fetchone.return_value = {'count': 10}

        # 执行统计
        result = self.repo.count("symbol = %s", ('BTCUSDT',))

        # 验证结果
        self.assertEqual(result, 10)


class TestTradeRepository(unittest.TestCase):
    """测试交易记录仓库"""

    def setUp(self):
        """测试前准备"""
        self.repo = TradeRepository()

    def test_get_entity_name(self):
        """测试获取实体名称"""
        self.assertEqual(self.repo.get_entity_name(), "交易记录")

    @patch('models.repository.get_db_connection')
    def test_get_by_order_id(self, mock_get_db_connection):
        """测试根据订单ID查询"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock查询结果
        mock_cursor.fetchone.return_value = {
            'order_id': 123456,
            'symbol': 'BTCUSDT',
            'status': 'FILLED'
        }

        # 执行查询
        result = self.repo.get_by_order_id(123456)

        # 验证结果
        self.assertIsNotNone(result)
        self.assertEqual(result['order_id'], 123456)

    @patch('models.repository.get_db_connection')
    def test_save_trade(self, mock_get_db_connection):
        """测试保存交易记录"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.rowcount = 1

        # 执行保存
        order_data = {
            'orderId': 123456,
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'positionSide': 'LONG',
            'type': 'LIMIT',
            'origQty': '0.001',
            'price': '50000',
            'status': 'NEW',
            'updateTime': int(datetime.now().timestamp() * 1000)
        }
        result = self.repo.save_trade(order_data)

        # 验证结果
        self.assertEqual(result, 1)


class TestFrequencyRepository(unittest.TestCase):
    """测试频率控制仓库"""

    def setUp(self):
        """测试前准备"""
        self.repo = FrequencyRepository()

    def test_get_entity_name(self):
        """测试获取实体名称"""
        self.assertEqual(self.repo.get_entity_name(), "频率控制记录")

    @patch('models.repository.get_db_connection')
    def test_get_daily_total_trades(self, mock_get_db_connection):
        """测试获取每日总交易数"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock查询结果
        mock_cursor.fetchone.return_value = {'count': 5}

        # 执行查询
        result = self.repo.get_daily_total_trades()

        # 验证结果
        self.assertEqual(result, 5)

    @patch('models.repository.get_db_connection')
    def test_get_consecutive_losses(self, mock_get_db_connection):
        """测试获取连续亏损次数"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock查询结果
        mock_cursor.fetchall.return_value = [
            {'pnl': Decimal('-10')},
            {'pnl': Decimal('-20')},
            {'pnl': Decimal('-15')},
            {'pnl': Decimal('30')},  # 盈利，中断连续亏损
            {'pnl': Decimal('-5')}
        ]

        # 执行查询
        result = self.repo.get_consecutive_losses()

        # 验证结果
        self.assertEqual(result, 3)  # 前3笔连续亏损


class TestPerformanceRepository(unittest.TestCase):
    """测试绩效统计仓库"""

    def setUp(self):
        """测试前准备"""
        self.repo = PerformanceRepository()

    def test_get_entity_name(self):
        """测试获取实体名称"""
        self.assertEqual(self.repo.get_entity_name(), "绩效统计记录")

    @patch('models.repository.get_db_connection')
    def test_get_weekly_statistics(self, mock_get_db_connection):
        """测试获取周统计"""
        # Mock数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock查询结果
        mock_cursor.fetchall.return_value = [
            {
                'period_type': 'WEEKLY',
                'total_trades': 10,
                'winning_trades': 6,
                'losing_trades': 4
            }
        ]

        # 执行查询
        results = self.repo.get_weekly_statistics(weeks=1)

        # 验证结果
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['total_trades'], 10)


if __name__ == '__main__':
    unittest.main()
